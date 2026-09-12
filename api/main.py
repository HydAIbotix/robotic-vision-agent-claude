"""
Management API — FastAPI server powering the management frontend.

Start with:
    python api/main.py
    # or:
    uvicorn api.main:app --host 0.0.0.0 --port 8001 --reload

Endpoints:
    GET  /api/health            — liveness check
    GET  /api/runs              — list test runs
    POST /api/runs              — start a new test run (async)
    GET  /api/runs/{run_id}     — run details + step results
    WS   /api/runs/{run_id}/ws  — live event stream for a running suite
    GET  /api/test-cases        — list test cases from DB
    POST /api/test-cases/upload — upload Excel file
    GET  /api/config            — kiosk/robot configuration
    PUT  /api/config            — update configuration
    GET  /api/robots            — robot status (real backend only)
    POST /api/explore           — trigger app explorer
"""
import sys
import io
import os

# Windows default stdout/stderr use cp1252 which can't encode many Unicode chars
# (emoji, em-dash, etc.) that appear in Claude API responses and test output.
# Reconfigure to UTF-8 before any other code runs so all print() calls are safe.
for _s in ('stdout', 'stderr'):
    _stream = getattr(sys, _s)
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')
    elif hasattr(_stream, 'buffer'):
        setattr(sys, _s, io.TextIOWrapper(_stream.buffer, encoding='utf-8', errors='replace'))

import json
import threading
import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from vision_agent.config import settings
from ports import paths as tenant_paths   # tenant-aware blob roots (single-tenant == MVP paths)
from ports.tenancy import current_tenant, set_current_tenant
from ports.event_bus import get_event_bus   # realtime fan-out: in-memory (1 replica) | redis (N)
from api.database import get_db, init_db
from api import models

app = FastAPI(title="Robotic Kiosk Test Management API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class _TenantASGIMiddleware:
    """Bind the request's tenant (X-Tenant-Id header) for the duration of the call, so the DB
    tenant-filter and tenant-scoped blob paths use it. Pure-ASGI (not BaseHTTPMiddleware) so the
    contextvar set here reliably propagates into the endpoint — including sync endpoints dispatched
    to the threadpool, which anyio runs with a copy of this context. No-op unless MULTI_TENANT_ENABLED
    (single-tenant → the default tenant everywhere), so it never affects existing single-tenant use."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and settings.multi_tenant_enabled:
            from ports.tenancy import resolve_tenant_from_headers
            hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            set_current_tenant(resolve_tenant_from_headers(hdrs))   # JWT claim or X-Tenant-Id header
        await self.app(scope, receive, send)


app.add_middleware(_TenantASGIMiddleware)

# Live WebSocket connections keyed by run_id (LOCAL to this replica).
_ws_connections: dict[str, list[WebSocket]] = {}
# One event-bus subscription per run_id per replica (ref-counted by the sockets above). With the
# Redis event bus this is what makes realtime work across MULTIPLE replicas: the worker on any
# replica publishes an event, Redis fans it out to every replica, and each delivers to ITS local
# sockets. With the in-memory bus (single replica) it behaves exactly as the original in-process WS.
_ws_subs: dict[str, "callable"] = {}
_ws_lock = threading.Lock()


def _ws_channel(run_id: str) -> str:
    """Event-bus channel for a run's live events, namespaced by tenant so a run_id reused across
    tenants (per-tenant unique) never crosses streams. Single-tenant → 'ws:run:default:<run_id>'."""
    return f"ws:run:{current_tenant()}:{run_id}"

# Active run threads
_active_runs: dict[str, dict] = {}

# The running asyncio event loop — captured at startup so background threads
# can submit coroutines to it via asyncio.run_coroutine_threadsafe().
_main_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def startup():
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    init_db()
    print(f"  [API] Management API ready at http://{settings.api_host}:{settings.api_port}")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    # Report the ACTIVE cloud-agnostic backends so an operator can confirm, at a glance, which
    # adapters a given deployment resolved to (local box vs. Azure/GCP/EC2). Purely informational.
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "platform": {
            "vision_backend":    settings.vision_backend,      # anthropic | bedrock (Claude either way)
            "persistence":       settings.persistence_backend,  # sqlite | postgres
            "object_store":      settings.storage_backend,      # local | s3 | minio | gcs | azure
            "event_bus":         settings.event_bus_backend,    # memory | redis
            "task_queue":        settings.task_queue_backend,   # inline | redis
            "orchestrator":      settings.orchestrator_backend, # inprocess | temporal
            "tracing":           settings.tracing_backend,      # none | otel | langfuse
            "memory":            settings.memory_backend,       # none | chroma | pgvector
            "multi_tenant":      settings.multi_tenant_enabled,
            "deployment_mode":   settings.deployment_mode,      # docker | k8s
            "service_role":      settings.service_role,         # all | api | worker
        },
    }


# ── Test Runs ─────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    robot_id:   str = "R-01"
    kiosk_id:   str = ""          # derived from test cases if empty
    excel_path: str = ""          # reads from DB when empty
    filter_tc:  Optional[str] = None
    mode:       str = "playwright"   # playwright | real | demo
    credentials: dict = {
        "valid":   {"email": "tester@kiosk.local", "password": "Password123"},
        "invalid": {"email": "baduser", "password": "wrongpass"},
    }


@app.get("/api/runs")
def list_runs(limit: int = 50, db: Session = Depends(get_db)):
    runs = (
        db.query(models.TestRun)
        .order_by(models.TestRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_run_summary(r) for r in runs]


def _base_screens_dir() -> Path:
    # Tenant-scoped in multi-tenant mode; identical to the MVP path when single-tenant.
    return tenant_paths.screens_dir()


def _run_screens_dir(run_id: str) -> Path:
    return _base_screens_dir() / run_id


def _run_results_dir(run_id: str) -> Path:
    """Per-run results folder: results/<run_id>/ — holds results.json + run.log so every run's
    output is PRESERVED (unique per run_id, never overwritten by a later run). Tenant-scoped in
    multi-tenant mode; identical to the MVP path when single-tenant."""
    return tenant_paths.results_dir() / run_id


class _Tee:
    """Write to several streams at once (console + a per-run log file). Robust: a failing stream is
    skipped so logging never breaks a run. Used to capture the full console log of one run to
    results/<run_id>/run.log while still printing to the server console."""
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _render_run_log(run_id: str, run, test_results: list, robot_events: list) -> str:
    """Human-readable per-run action log rebuilt from the structured results — each test, each step
    (action + coordinates + result + observation), plus the robot API telemetry (exact camera pixels
    tapped, latency, HTTP status). Deterministic and thread-safe (does not rely on captured stdout)."""
    lines: list[str] = []
    lines.append(f"Run {run_id}")
    lines.append(f"  backend={getattr(run, 'mode', '')}  kiosk={getattr(run, 'kiosk_id', '')}  "
                 f"started={getattr(run, 'started_at', '')}  finished={datetime.utcnow().isoformat()}Z")
    lines.append("")
    for tr in test_results:
        lines.append(f"[{tr.get('outcome','?').upper()}] {tr.get('test_id','')}  {tr.get('summary','')}")
        for n, s in enumerate(tr.get("step_results") or [], 1):
            ok = "PASS" if s.get("success") else "FAIL"
            extra = []
            for k in ("method", "screen_id", "element_id", "expected_screen", "actual_screen",
                      "expected_text", "screenshot_before", "screenshot_after"):
                if s.get(k):
                    extra.append(f"{k}={s[k]}")
            lines.append(f"   {n:>2}. [{ok}] {s.get('step','')}" + (("  (" + ", ".join(extra) + ")") if extra else ""))
            if s.get("observation"):
                lines.append(f"        → {s['observation']}")
            if s.get("note"):
                lines.append(f"        note: {s['note']}")
        lines.append(f"   summary: {tr.get('vision_summary','')}")
        lines.append("")
    if robot_events:
        lines.append("── Robot API telemetry (endpoint, cmd, camera coords, status, latency) ──")
        for ev in robot_events:
            coords = ""
            if ev.get("u") is not None and ev.get("v") is not None:
                coords = f" @cam({ev['u']},{ev['v']})"
            lines.append(f"   {ev.get('request_time','')} {ev.get('event_type','')} {ev.get('endpoint','')}"
                         f"{(' (' + ev.get('cmd_id','') + ')') if ev.get('cmd_id') else ''}{coords}"
                         f" → {ev.get('http_status','?')} in {ev.get('latency_ms','?')}ms @ {ev.get('controller','')}")
    return "\n".join(lines) + "\n"


def _write_run_artifacts(run_id: str, run, test_results: list, robot_events: list) -> None:
    """Persist this run's results to results/<run_id>/ (results.json + run.log). Never raises —
    an artifact-write failure must not fail the run. Preserves EVERY run (unique per run_id)."""
    try:
        out_dir = _run_results_dir(run_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "run_id":       run_id,
            "backend":      getattr(run, "mode", ""),
            "kiosk_id":     getattr(run, "kiosk_id", ""),
            "started_at":   str(getattr(run, "started_at", "")),
            "finished_at":  datetime.utcnow().isoformat() + "Z",
            "total":        getattr(run, "total", 0),
            "passed":       getattr(run, "passed", 0),
            "failed":       getattr(run, "failed", 0),
            "status":       getattr(run, "status", ""),
            "test_results": test_results,
            "robot_events": robot_events,
        }
        (out_dir / "results.json").write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        (out_dir / "run.log").write_text(_render_run_log(run_id, run, test_results, robot_events),
                                         encoding="utf-8")
        print(f"  [RUN] Results preserved → {out_dir}")
    except Exception as exc:
        print(f"  [RUN] could not write per-run results artifacts: {exc}")


def _next_run_number() -> int:
    """Monotonic run counter persisted in results/.run_seq.  Reset deletes it → restarts at 1.
    Tenant-scoped so each tenant numbers its own runs independently (== global when single-tenant)."""
    p = tenant_paths.results_dir() / ".run_seq"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        n = int(p.read_text().strip()) if p.exists() else 0
    except Exception:
        n = 0
    n += 1
    try:
        p.write_text(str(n))
    except Exception:
        pass
    return n


@app.post("/api/runs", status_code=202)
def start_run(req: RunRequest, db: Session = Depends(get_db)):
    # Memorable, ordered run id: run-<N>-<HHMMSS>-<DDMM>  (N increments per run, resets on reset)
    now = datetime.now()
    run_id = f"run-{_next_run_number()}-{now.strftime('%H%M%S-%d%m')}"
    run = models.TestRun(
        run_id     = run_id,
        kiosk_id   = req.kiosk_id,
        robot_id   = req.robot_id,
        excel_path = req.excel_path,
        filter_tc  = req.filter_tc,
        mode       = req.mode,
        status     = "pending",
        created_at = datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    # Submit the run through the orchestration seam. Capture the tenant HERE (request context) — it
    # does not cross a thread/process boundary — and carry it in the job payload.
    #   inprocess + inline  → a same-process daemon thread (the MVP; single node)
    #   inprocess + redis   → a SERVICE_ROLE=worker process consumes it (API/worker split)
    #   temporal            → a durable Temporal workflow
    # All paths ultimately call _execute_run(run_id, req, tenant), so behaviour is identical.
    from ports.orchestration import submit_run
    payload = {"run_id": run_id, "req": req.model_dump(), "tenant": current_tenant()}
    _active_runs[run_id] = {"req": req}
    submit_run(payload, _run_job)

    return {"run_id": run_id, "status": "pending"}


def _run_job(payload: dict) -> None:
    """Shared run entry point for every orchestration/queue backend: reconstruct the request and
    execute the suite. Runs in whichever context the backend provides (thread / worker / activity)."""
    req = RunRequest(**payload["req"])
    _execute_run(payload["run_id"], req, payload.get("tenant", ""))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.query(models.TestRun).filter_by(run_id=run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        **_run_summary(run),
        "results": [
            {
                "test_id":       r.test_id,
                "summary":       r.summary,
                "outcome":       r.outcome,
                "step_results":  r.step_results,
                "vision_summary":r.vision_summary,
            }
            for r in run.results
        ],
    }


@app.websocket("/api/runs/{run_id}/ws")
async def run_ws(run_id: str, ws: WebSocket):
    await ws.accept()
    # Bind the client's tenant (from the handshake headers) so the channel matches the publisher's.
    # The HTTP tenant middleware doesn't see WebSocket scopes, so resolve it here. No-op single-tenant.
    if settings.multi_tenant_enabled:
        from ports.tenancy import resolve_tenant_from_headers
        set_current_tenant(resolve_tenant_from_headers({k.lower(): v for k, v in ws.headers.items()}))
    channel = _ws_channel(run_id)
    with _ws_lock:
        _ws_connections.setdefault(run_id, []).append(ws)
        # First local socket for this run_id → subscribe this replica to the run's bus channel.
        if run_id not in _ws_subs:
            _ws_subs[run_id] = get_event_bus().subscribe(
                channel, lambda data, rid=run_id: _deliver_local(rid, data))
    try:
        while True:
            await asyncio.sleep(30)  # keep alive
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            conns = _ws_connections.get(run_id, [])
            if ws in conns:
                conns.remove(ws)
            # Last local socket gone → drop this replica's subscription for the run.
            if not conns:
                _ws_connections.pop(run_id, None)
                unsub = _ws_subs.pop(run_id, None)
                if unsub:
                    try:
                        unsub()
                    except Exception:
                        pass


@app.get("/api/runs/{run_id}/defects")
def get_run_defects(run_id: str, db: Session = Depends(get_db)):
    defects = db.query(models.Defect).filter_by(run_id=run_id).all()
    return [
        {
            "id":                 d.id,
            "run_id":             d.run_id,
            "test_id":            d.test_id,
            "title":              d.title,
            "description":        d.description,
            "steps_to_reproduce": d.steps_to_reproduce,
            "root_cause":         d.root_cause,
            "probable_fix":       d.probable_fix,
            "severity":           d.severity,
            "priority":           d.priority,
            "jira_key":           d.jira_key,
            "jira_url":           d.jira_url,
            "status":             d.status,
            "evidence":           d.evidence_json or [],
            "created_at":         d.created_at.isoformat() if d.created_at else None,
        }
        for d in defects
    ]


# ── Auto-Repair (RAG + Claude self-healing) ─────────────────────────────────────
# The self-healing arm of defect intelligence. A repair job runs the retrieve → diagnose
# (Claude) → apply → lint → build → pr-prep pipeline in a background thread; stages stream
# into an in-memory job record the frontend polls. Pushing/opening the PR is a SEPARATE,
# explicitly-triggered endpoint (never automatic) since it is outward-facing.

_repair_jobs: dict[str, dict] = {}
_repair_lock = threading.Lock()
# repair_id → cancel Event. The /cancel endpoint sets it; run_repair polls it (cooperative cancel).
_repair_cancel: dict[str, threading.Event] = {}


def _repair_terminal(status: str) -> bool:
    return status in ("succeeded", "completed", "failed", "cancelled")


class RepairRequest(BaseModel):
    failure: str = ""                    # failed-test message / plain-English defect
    test_id: str = ""                    # e.g. "TC-RPS-001" (labels the branch/PR)
    run_id:  Optional[str] = None        # optional link back to the run that failed
    apply:   bool = True                 # False → dry run (retrieve + diagnose only)
    auto_pr: Optional[bool] = None       # None → settings.repair_auto_pr; True/False to override


def _repair_set(rid: str, **fields):
    # Positional param is `rid` (not `repair_id`) so callers may also pass repair_id=... in **fields
    # (it belongs in the job dict for the frontend) without a "multiple values" TypeError.
    with _repair_lock:
        _repair_jobs.setdefault(rid, {}).update(fields)


def _repair_stage(repair_id: str, update: dict):
    """Progress callback: merge one stage update into the job's stage map."""
    with _repair_lock:
        job = _repair_jobs.setdefault(repair_id, {})
        stages = job.setdefault("stages", {})
        stage = update.get("stage", "")
        if stage:
            stages[stage] = {**stages.get(stage, {}), **update}
        job["updated_at"] = datetime.utcnow().isoformat()


def _run_repair_job(repair_id: str, req: RepairRequest):
    from repair_agent.repair_failed_test import run_repair
    auto_pr = settings.repair_auto_pr if req.auto_pr is None else req.auto_pr
    cancel_event = threading.Event()
    with _repair_lock:
        _repair_cancel[repair_id] = cancel_event
    _repair_set(repair_id, status="running")
    try:
        result = run_repair(
            req.failure, test_id=req.test_id, apply=req.apply, auto_pr=auto_pr,
            branch_suffix=repair_id.split("-")[-1],
            progress_cb=lambda u: _repair_stage(repair_id, u),
            cancel_event=cancel_event,
        )
        with _repair_lock:
            job = _repair_jobs.setdefault(repair_id, {})
            job["result"] = result
            job["status"] = ("cancelled" if result.get("cancelled")
                             else "succeeded" if result.get("success") else "completed")
            if result.get("error"):
                job["error"] = result["error"]
            job["updated_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        print(f"  [REPAIR] job {repair_id} failed: {e}")
        _repair_set(repair_id, status="failed", error=str(e))
    finally:
        with _repair_lock:
            _repair_cancel.pop(repair_id, None)


@app.post("/api/repair", status_code=202)
def start_repair(req: RepairRequest):
    if not (req.failure or "").strip():
        raise HTTPException(status_code=400, detail="A 'failure' description is required.")
    repair_id = f"repair-{uuid.uuid4().hex[:8]}"
    _repair_set(
        repair_id,
        repair_id=repair_id, status="pending", stages={},
        failure=req.failure, test_id=req.test_id, run_id=req.run_id,
        created_at=datetime.utcnow().isoformat(),
    )
    threading.Thread(target=_run_repair_job, args=(repair_id, req), daemon=True).start()
    return {"repair_id": repair_id, "status": "pending"}


class RepairIndexState:
    building = False
    last_message = ""


# NOTE: the literal-path index routes MUST be declared before `GET /api/repair/{repair_id}`,
# otherwise FastAPI matches `/api/repair/index` as get_repair(repair_id="index") → 404.
@app.post("/api/repair/index", status_code=202)
def build_repair_index():
    """(Re)build the Chroma RAG index over the live kiosk app + docs, in the background."""
    if RepairIndexState.building:
        return {"status": "building", "message": "Index build already in progress."}

    def _build():
        from repair_agent.parse_code_and_store import build_codebase_index, CODEBASE_DIR, PERSIST_DIR
        RepairIndexState.building = True
        RepairIndexState.last_message = "Building index…"
        try:
            n = build_codebase_index()
            RepairIndexState.last_message = (
                f"Indexed {n} chunks from {CODEBASE_DIR}" if n
                else f"Indexed {CODEBASE_DIR} → {PERSIST_DIR}"
            )
        except Exception as e:
            RepairIndexState.last_message = f"Index build failed: {e}"
        finally:
            RepairIndexState.building = False

    threading.Thread(target=_build, daemon=True).start()
    return {"status": "building"}


@app.get("/api/repair/index")
def repair_index_status():
    from repair_agent.parse_code_and_store import PERSIST_DIR
    return {
        "building": RepairIndexState.building,
        "exists":   PERSIST_DIR.exists(),
        "message":  RepairIndexState.last_message,
        "persist_dir": str(PERSIST_DIR),
    }


@app.get("/api/repair")
def list_repairs():
    """Dashboard feed: every repair job this backend has run this session, newest first.
    Each entry is the full job (stages + result) so the UI can render live + historical
    pipelines without a per-job poll. In-memory only — cleared on backend restart."""
    with _repair_lock:
        jobs = [dict(j) for j in _repair_jobs.values()]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs


@app.get("/api/repair/{repair_id}")
def get_repair(repair_id: str):
    with _repair_lock:
        job = _repair_jobs.get(repair_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown repair job.")
        return dict(job)


class OpenPrRequest(BaseModel):
    confirm: bool = False


@app.post("/api/repair/{repair_id}/open-pr")
def open_repair_pr(repair_id: str, body: OpenPrRequest):
    """GATED outward-facing step — push the prepared branch and open the PR on GitHub."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to push and open the PR.")
    with _repair_lock:
        job = _repair_jobs.get(repair_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown repair job.")
    pr = ((job.get("result") or {}).get("stages") or {}).get("pr") or {}
    if not pr.get("prepared"):
        raise HTTPException(status_code=400, detail="No prepared PR branch/commit for this job.")

    from repair_agent.repair_failed_test import open_pull_request
    outcome = open_pull_request(pr["branch"], pr["base"], pr["title"], pr["body"])
    with _repair_lock:
        _repair_jobs[repair_id].setdefault("result", {}).setdefault("stages", {}).setdefault("pr", {})
        _repair_jobs[repair_id]["result"]["stages"]["pr"].update({"opened": outcome})
    return outcome


@app.post("/api/repair/{repair_id}/delete-pr")
def delete_repair_pr(repair_id: str):
    """Tear down a raised PR (delete its remote + local head branch) so repeated demo runs don't pile
    up branches/PRs on GitHub. Only touches the repair/* fix branch — never the base demo bug branch."""
    with _repair_lock:
        job = _repair_jobs.get(repair_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown repair job.")
    pr = ((job.get("result") or {}).get("stages") or {}).get("pr") or {}
    branch = pr.get("branch")
    if not branch:
        raise HTTPException(status_code=400, detail="No PR branch recorded for this job.")

    from repair_agent.repair_failed_test import delete_pull_request
    outcome = delete_pull_request(branch)
    with _repair_lock:
        prj = _repair_jobs[repair_id].setdefault("result", {}).setdefault("stages", {}).setdefault("pr", {})
        prj["deleted"] = outcome                       # record the deletion outcome
        if outcome.get("deleted"):
            prj.pop("opened", None)                     # the PR/branch is gone → drop the "View PR" link
    return outcome


@app.post("/api/repair/{repair_id}/cancel")
def cancel_repair(repair_id: str):
    """Cancel a running Auto-Repair job. Cooperative: sets the job's cancel Event; the pipeline stops
    at the next stage boundary and interrupts a slow DIAGNOSE call (so a stuck/slow model can't pin
    it). A job that has already finished is returned as-is."""
    with _repair_lock:
        job = _repair_jobs.get(repair_id)
        ev = _repair_cancel.get(repair_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown repair job.")
    status = job.get("status", "")
    if _repair_terminal(status) or ev is None:
        return {"status": status or "unknown", "cancelling": False,
                "message": "Repair already finished — nothing to cancel."}
    ev.set()
    _repair_set(repair_id, status="cancelling")
    return {"status": "cancelling", "cancelling": True}


# ── Test Cases ────────────────────────────────────────────────────────────────

@app.get("/api/test-cases")
def list_test_cases(kiosk_id: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.TestCase)
    if kiosk_id:
        q = q.filter_by(kiosk_id=kiosk_id)
    return [_tc_summary(t) for t in q.order_by(models.TestCase.test_id).all()]


def _infer_kiosk_id(test_id: str, steps_raw: str = "", db: Optional[Session] = None) -> str:
    """Derive the kiosk_id a test case belongs to — the single join key used across the whole
    lifecycle (exploration → device map → test cases → execution → results).

    Resolution order (first match wins):
      1. Device-map alias.  A test id like ``TC-VPS-001`` names its device via the ``VPS`` token;
         we match that token (or any configured alias appearing in the id/steps) to a
         DeviceConfig and return its linked kiosk_id.  This is the user-configured VPS→kiosk-1
         mapping and the primary, explicit path.
      2. A configured KioskConfig.kiosk_id appearing verbatim in the id/steps.
      3. Legacy K-01/K-02 text heuristic (kept so older test suites keep working).
    """
    import re
    text   = (test_id + " " + (steps_raw or "")).upper()
    tokens = {t for t in re.split(r"[-_\s]+", text) if t}

    if db is not None:
        try:
            # 1. Device alias → kiosk_id (e.g. VPS → kiosk-1).  Prefer a token-exact alias match
            #    (TC-VPS-001 → 'VPS'); fall back to an alias appearing anywhere in the text.
            devices = db.query(models.DeviceConfig).all()
            for d in devices:
                if d.alias and d.kiosk_id and d.alias.upper() in tokens:
                    return d.kiosk_id
            for d in devices:
                if d.alias and d.kiosk_id and d.alias.upper() in text:
                    return d.kiosk_id
            # 2. A configured kiosk id mentioned directly.
            for k in db.query(models.KioskConfig).all():
                if k.kiosk_id and k.kiosk_id.upper() in tokens:
                    return k.kiosk_id
        except Exception:
            pass

    # 3. Legacy heuristic.
    if "K-01" in text or "K1" in text or "KIOSK-1" in text or "KIOSK 1" in text:
        return "K-01"
    if "K-02" in text or "K2" in text or "KIOSK-2" in text or "KIOSK 2" in text:
        return "K-02"
    return "K-01"  # safe default


def _infer_kiosk_ids(test_id: str, steps_raw: str = "", db: Optional[Session] = None) -> list[str]:
    """ALL distinct kiosks a test references, in order of first appearance.

    Most tests touch ONE kiosk → returns a single id (same as _infer_kiosk_id). A CROSS-KIOSK
    E2E test names several devices in its steps (e.g. "load at VPS … buy at RPS …") → returns
    every referenced kiosk, so planning/execution can span both apps. Matches Device Map aliases
    and configured kiosk ids as whole words, ordered by where they first appear in the text.
    Falls back to [_infer_kiosk_id(...)] when nothing explicit is found.
    """
    import re
    text = (test_id + " " + (steps_raw or "")).upper()

    def _first_pos(needle: str) -> int:
        m = re.search(r"\b" + re.escape(needle.upper()) + r"\b", text)
        return m.start() if m else -1

    hits: list[tuple[int, str]] = []
    if db is not None:
        try:
            for d in db.query(models.DeviceConfig).all():
                if d.alias and d.kiosk_id:
                    p = _first_pos(d.alias)
                    if p != -1:
                        hits.append((p, d.kiosk_id))
            for k in db.query(models.KioskConfig).all():
                if k.kiosk_id:
                    p = _first_pos(k.kiosk_id)
                    if p != -1:
                        hits.append((p, k.kiosk_id))
        except Exception:
            pass

    ordered: list[str] = []
    for _, kid in sorted(hits, key=lambda t: t[0]):
        if kid not in ordered:
            ordered.append(kid)
    return ordered or [_infer_kiosk_id(test_id, steps_raw, db)]


def _scope_map_for_test(db: Optional[Session], app_map: Optional[dict],
                        test_id: str, steps_raw: str = "") -> tuple[Optional[dict], list[str]]:
    """Scope a multi-app app_map down to exactly the kiosk(s) a test references.

    Planning and execution must see ONLY the target app(s)' screens — a single-kiosk test must not
    be planned against another kiosk's screens (a VPS test once got RPS's login screen), and a
    cross-kiosk E2E test must see BOTH apps it touches (otherwise the second kiosk's whole flow
    collapses into one un-plannable vision_required step). This is the single choke point that
    guarantees correct scoping for BOTH plan generation (/tc-plan) and execution (parse_steps):
    resolve the test's kiosk_ids, then filter the map to the UNION of those app_ids.

    Returns (scoped_map, kiosk_ids). No-op (map unchanged) for a legacy single-app map or when the
    kiosks can't be matched to tagged apps — logged loudly when the map holds more than one app,
    since that mismatch could otherwise yield a wrong-app plan.
    """
    from app_map import store as app_map_store
    if not app_map:
        return app_map, []
    kiosk_ids = _infer_kiosk_ids(test_id, steps_raw, db)
    scoped    = app_map_store.scoped_to_apps(app_map, kiosk_ids)
    apps      = app_map.get("apps") or {}
    # Did the resolved kiosk(s) actually match tagged apps? (A 2-kiosk E2E test in a 2-kiosk env
    # legitimately spans the whole map — that is NOT a mismatch. Only warn when NONE matched.)
    tagged_app_ids = {(sc.get("app_id") or "") for sc in (app_map.get("screens") or {}).values()}
    matched = any(kid in tagged_app_ids for kid in kiosk_ids)
    if len(apps) > 1 and not matched:
        print(f"  [PLAN] ⚠ test '{test_id}' resolved to kiosk(s) {kiosk_ids}, which match no app in "
              f"the map (apps: {list(apps)}). Plan will see ALL apps' screens — check that the Device "
              f"Map alias→kiosk_id matches the Kiosk ID used during exploration.")
    else:
        print(f"  [PLAN] test '{test_id}' scoped to kiosk(s) {kiosk_ids} "
              f"({len(scoped.get('screens') or {})} screen(s))")
    return scoped, kiosk_ids


@app.post("/api/test-cases/upload")
def upload_test_cases(
    file: UploadFile = File(...),
    kiosk_id: Optional[str] = None,  # auto-detected from test content when omitted
    db: Session = Depends(get_db),
):
    import tempfile
    from test_runner.reader.excel_reader import read_test_cases

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(file.file.read())
        tmp_path = tmp.name

    try:
        cases = read_test_cases(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    saved = 0
    for tc in cases:
        tc_kiosk = kiosk_id or _infer_kiosk_id(tc["test_id"], tc.get("steps_raw", ""), db)
        existing = db.query(models.TestCase).filter_by(test_id=tc["test_id"]).first()
        if existing:
            for k, v in tc.items():
                setattr(existing, k, v)
            existing.kiosk_id = tc_kiosk
        else:
            db.add(models.TestCase(kiosk_id=tc_kiosk, **tc))
            saved += 1
    db.commit()
    return {"imported": len(cases), "new": saved}


# ── Configuration ─────────────────────────────────────────────────────────────

def _device_summary(d: models.DeviceConfig) -> dict:
    return {"alias": d.alias, "kiosk_id": d.kiosk_id or "",
            "position_name": (getattr(d, "position_name", "") or ""),
            "description": d.description or "",
            "pos_x": d.pos_x, "pos_y": d.pos_y, "pos_theta": d.pos_theta}


@app.get("/api/config")
def get_config(db: Session = Depends(get_db)):
    kiosks  = db.query(models.KioskConfig).all()
    devices = db.query(models.DeviceConfig).order_by(models.DeviceConfig.alias).all()
    return {
        "robot_backend":    settings.robot_backend,
        "robot_ip":         settings.robot_ip,
        "robot_port":       settings.robot_port,
        "agv_url":          settings.agv_url,
        "arm_url":          settings.arm_url,
        "agv_base":         settings.agv_api_base(),
        "arm_base":         settings.arm_api_base(),
        "robot_id":         settings.robot_id,
        "agv_home_target":  settings.agv_home_target,
        "exploration_mode": _runtime_exploration_mode,
        "card_service_url": settings.card_service_url,
        "viewport":         {"width": settings.viewport_width, "height": settings.viewport_height},
        "camera":           {"width": settings.robot_camera_width, "height": settings.robot_camera_height},
        "repair_llm":       {"backend": settings.repair_llm_backend,          # claude | local
                             "local_model": settings.repair_local_model,
                             "local_base_url": settings.repair_local_base_url},
        "kiosks":           [_kiosk_summary(k) for k in kiosks],
        "devices":          [_device_summary(d) for d in devices],
    }


class CardServiceRequest(BaseModel):
    card_service_url: str = ""


@app.patch("/api/config/card-service")
def set_card_service(req: CardServiceRequest):
    """Set the shared card service URL (blank = kiosk apps use per-browser localStorage).

    Applied live (playwright appends ?cardServiceUrl=… to the kiosk URL on the next
    navigation) and persisted to .env so it survives restarts.
    """
    url = (req.card_service_url or "").strip().rstrip("/")
    settings.card_service_url = url
    _persist_env({"CARD_SERVICE_URL": url})
    return {"status": "ok", "card_service_url": url}


class RepairLlmRequest(BaseModel):
    backend: str   # "claude" | "local"


@app.patch("/api/config/repair-llm")
def set_repair_llm(req: RepairLlmRequest):
    """Choose which model the Auto-Repair DIAGNOSE step tries FIRST: Claude (default) or the local
    Ollama model. The other stays as an automatic backup; the deterministic demo rule is the last
    resort. Applied LIVE (propose_patch reads settings.repair_llm_backend at call time, so the next
    repair uses the new choice with no restart) and persisted to .env so it survives restarts."""
    backend = (req.backend or "").strip().lower()
    if backend not in ("claude", "local"):
        raise HTTPException(400, "backend must be 'claude' or 'local'")
    settings.repair_llm_backend = backend
    _persist_env({"REPAIR_LLM_BACKEND": backend})
    return {"status": "ok", "repair_llm": {"backend": backend,
                                           "local_model": settings.repair_local_model,
                                           "local_base_url": settings.repair_local_base_url}}


# ── Robot connection config (backend / ip / port) — settable from the UI ──────────

def _persist_env(updates: dict[str, str]) -> None:
    """Update/insert KEY=VALUE lines in .env, preserving every other line (incl. the API key).

    Line-oriented: replaces a key's line if present, appends it otherwise.  Never rewrites or
    reorders unrelated lines, so secrets in .env are untouched.
    """
    env_path = Path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


class RobotConnRequest(BaseModel):
    robot_backend: str            # "demo" | "playwright" | "real"
    robot_ip:      Optional[str] = None
    robot_port:    Optional[int] = None
    agv_url:         Optional[str] = None   # mobile base (AGV) controller URL
    arm_url:         Optional[str] = None   # arm/camera/card controller URL
    agv_home_target: Optional[str] = None   # AGV map position name for a "go home" step (e.g. "home-Aug-14-G37")


@app.patch("/api/config/robot")
def set_robot_conn(req: RobotConnRequest):
    """Set the robot backend and (for 'real') its IP/port.

    IP/port take effect immediately (real_robot reads settings live on each call, so the Robot
    Setup health checks use the new values right away).  Switching the *backend* also needs an
    API restart to take effect for test RUNS, because the robot dispatcher binds the backend's
    functions at import time — so we report restart_required when the backend changed.
    """
    backend = req.robot_backend.strip().lower()
    if backend not in ("demo", "playwright", "real"):
        raise HTTPException(400, f"Invalid robot_backend '{req.robot_backend}'")

    backend_changed = backend != settings.robot_backend
    env_updates: dict[str, str] = {"ROBOT_BACKEND": backend}
    settings.robot_backend = backend

    if backend == "real":
        if req.robot_ip is not None and req.robot_ip.strip():
            settings.robot_ip = req.robot_ip.strip()
            env_updates["ROBOT_IP"] = settings.robot_ip
        if req.robot_port is not None:
            settings.robot_port = int(req.robot_port)
            env_updates["ROBOT_PORT"] = str(settings.robot_port)
        # AGV base and arm may be on separate IPs — persist each (blank clears → falls back).
        if req.agv_url is not None:
            settings.agv_url = req.agv_url.strip().rstrip("/")
            env_updates["AGV_URL"] = settings.agv_url
        if req.arm_url is not None:
            settings.arm_url = req.arm_url.strip().rstrip("/")
            env_updates["ARM_URL"] = settings.arm_url

    # AGV "home" position name — settable regardless of backend (a name mapping, used by the real
    # backend's move-to-home). Blank resets to the literal "home".
    if req.agv_home_target is not None:
        settings.agv_home_target = req.agv_home_target.strip() or "home"
        env_updates["AGV_HOME_TARGET"] = settings.agv_home_target

    try:
        _persist_env(env_updates)
        persisted = True
    except Exception as e:
        print(f"  [CONFIG] could not persist .env: {e}")
        persisted = False

    return {
        "status":           "ok",
        "robot_backend":    settings.robot_backend,
        "robot_ip":         settings.robot_ip,
        "robot_port":       settings.robot_port,
        "agv_url":          settings.agv_url,
        "arm_url":          settings.arm_url,
        "agv_base":         settings.agv_api_base(),
        "arm_base":         settings.arm_api_base(),
        "robot_url":        settings.arm_api_base(),
        "agv_home_target":  settings.agv_home_target,
        "persisted":        persisted,
        "restart_required": backend_changed,
    }


# ── Camera / coordinate-space config (Robot Setup page) ───────────────────────────

class CameraConfigRequest(BaseModel):
    viewport_width:  Optional[int] = None
    viewport_height: Optional[int] = None
    camera_width:    Optional[int] = None
    camera_height:   Optional[int] = None


@app.patch("/api/config/camera")
def set_camera_config(req: CameraConfigRequest):
    """Save the coordinate-space dimensions that keep real-robot taps accurate.

    • viewport_* — the Playwright EXPLORATION pixel space app_map coordinates are learned in. Set it
      to the kiosk's rectified-camera ASPECT RATIO so the explored layout matches what the arm
      photographs; real_robot._scale then absorbs any pure resolution difference per-axis.
    • camera_* — a pre-calibration SEED for the rectified /capture resolution. Every /capture measures
      the real dims and overrides it (see vision_agent/config.py calibration note), so this only
      affects the pre-first-capture display; it never changes a live tap.

    Values take effect live (settings are read on each robot call) and persist to .env. The response
    returns both aspect ratios + whether they match, so the UI can warn on a mismatch (the usual
    cause of inaccurate taps)."""
    def _pos(v: object) -> bool:
        return isinstance(v, int) and v > 0

    updates: dict[str, str] = {}
    if req.viewport_width is not None:
        if not _pos(req.viewport_width):
            raise HTTPException(400, "viewport_width must be a positive integer")
        settings.viewport_width = req.viewport_width
        updates["VIEWPORT_WIDTH"] = str(req.viewport_width)
    if req.viewport_height is not None:
        if not _pos(req.viewport_height):
            raise HTTPException(400, "viewport_height must be a positive integer")
        settings.viewport_height = req.viewport_height
        updates["VIEWPORT_HEIGHT"] = str(req.viewport_height)
    if req.camera_width is not None:
        if not _pos(req.camera_width):
            raise HTTPException(400, "camera_width must be a positive integer")
        settings.robot_camera_width = req.camera_width
        updates["ROBOT_CAMERA_WIDTH"] = str(req.camera_width)
    if req.camera_height is not None:
        if not _pos(req.camera_height):
            raise HTTPException(400, "camera_height must be a positive integer")
        settings.robot_camera_height = req.camera_height
        updates["ROBOT_CAMERA_HEIGHT"] = str(req.camera_height)

    persisted = True
    if updates:
        try:
            _persist_env(updates)
        except Exception as e:
            print(f"  [CONFIG] could not persist camera config to .env: {e}")
            persisted = False

    def _ar(w: int, h: int) -> float:
        return round(w / h, 4) if h else 0.0
    vp_ar  = _ar(settings.viewport_width, settings.viewport_height)
    cam_ar = _ar(settings.robot_camera_width, settings.robot_camera_height)
    return {
        "status":         "ok",
        "viewport":       {"width": settings.viewport_width,  "height": settings.viewport_height,  "aspect": vp_ar},
        "camera":         {"width": settings.robot_camera_width, "height": settings.robot_camera_height, "aspect": cam_ar},
        "aspect_matches": abs(vp_ar - cam_ar) < 0.02,   # within ~2% → no reflow risk from aspect
        "persisted":      persisted,
    }


class DeviceConfigRequest(BaseModel):
    alias:         str
    kiosk_id:      str = ""
    position_name: str = ""   # AGV map position name for /base/goto target (blank → falls back to kiosk_id)
    description:   str = ""
    pos_x:         float = 0.0
    pos_y:         float = 0.0
    pos_theta:     float = 0.0


@app.get("/api/config/devices")
def list_devices(db: Session = Depends(get_db)):
    return [_device_summary(d) for d in db.query(models.DeviceConfig).order_by(models.DeviceConfig.alias).all()]


@app.put("/api/config/device")
def upsert_device(req: DeviceConfigRequest, db: Session = Depends(get_db)):
    existing = db.query(models.DeviceConfig).filter_by(alias=req.alias).first()
    if existing:
        for k, v in req.model_dump().items():
            setattr(existing, k, v)
        existing.updated_at = datetime.utcnow()
    else:
        db.add(models.DeviceConfig(**req.model_dump()))
    db.commit()
    return {"status": "ok", "alias": req.alias}


@app.delete("/api/config/device/{alias}", status_code=204)
def delete_device(alias: str, db: Session = Depends(get_db)):
    d = db.query(models.DeviceConfig).filter_by(alias=alias).first()
    if d:
        db.delete(d)
        db.commit()


class KioskConfigRequest(BaseModel):
    kiosk_id:   str
    name:       str = ""
    url:        str = "http://localhost:5173"
    robot_id:   str = "R-01"
    screen_w_m: float = 0.4
    screen_h_m: float = 0.3
    tag_id:     int = 1
    position_x: float = 0.0
    position_y: float = 0.0
    position_th:float = 0.0

    @field_validator(
        "name", "url", "robot_id", "screen_w_m", "screen_h_m", "tag_id",
        "position_x", "position_y", "position_th", mode="before",
    )
    @classmethod
    def _null_to_default(cls, v, info):
        # A kiosk row created by /api/explore leaves robot_id/tag_id NULL (no column default). The
        # Studio loads that kiosk and PUTs those fields back as JSON null → a non-optional str/int/
        # float would 422. Coerce null -> the field's own default instead of rejecting the save.
        return cls.model_fields[info.field_name].default if v is None else v


@app.put("/api/config/kiosk")
def upsert_kiosk(req: KioskConfigRequest, db: Session = Depends(get_db)):
    existing = db.query(models.KioskConfig).filter_by(kiosk_id=req.kiosk_id).first()
    if existing:
        for k, v in req.model_dump().items():
            setattr(existing, k, v)
        existing.updated_at = datetime.utcnow()
    else:
        db.add(models.KioskConfig(**req.model_dump()))
    db.commit()
    return {"status": "ok", "kiosk_id": req.kiosk_id}


# ── Robot Status ──────────────────────────────────────────────────────────────

@app.get("/api/robots")
def robot_status():
    if settings.robot_backend != "real":
        return {"mode": settings.robot_backend, "robots": []}
    try:
        from vision_agent.robot.real_robot import get_status
        return {"mode": "real", "robots": [get_status()]}
    except Exception as e:
        return {"mode": "real", "robots": [], "error": str(e)}


@app.get("/api/robot/health")
def robot_health(capture: bool = True):
    """Robot / Kiosk / Camera readiness for the Robot Setup page.

    Real backend → probes the physical robot's REST API (and calibrates via the camera check).
    Playwright / demo → reports a simulated-healthy status so the page is still meaningful.
    """
    arm_url = settings.arm_api_base()
    agv_url = settings.agv_api_base()
    if settings.robot_backend != "real":
        return {
            "backend":   settings.robot_backend,
            "arm_url":   arm_url,
            "agv_url":   agv_url,
            "robot_url": arm_url,  # back-compat alias
            "robot_id":  settings.robot_id,
            "kiosk_id":  settings.default_kiosk_id,
            "simulated": True,
            "healthy":   True,
            "components": {
                "robot":  {"status": "ok", "detail": f"Simulated — no physical robot arm ('{settings.robot_backend}' backend)"},
                "base":   {"status": "ok", "detail": f"Simulated — no physical AGV base ('{settings.robot_backend}' backend)"},
                "camera": {"status": "ok", "detail": "Screenshots captured from the browser (Playwright)"},
            },
            "checked_at": datetime.utcnow().timestamp(),
        }
    try:
        from vision_agent.robot.real_robot import health_check
        return health_check(do_capture=capture)
    except Exception as e:
        return {
            "backend": "real", "arm_url": arm_url, "agv_url": agv_url, "robot_url": arm_url,
            "robot_id": settings.robot_id,
            "simulated": False, "healthy": False, "error": str(e),
            "components": {"robot": {"status": "error", "detail": str(e)}},
        }


# ── Robot API Tester (Robot Setup page) ──────────────────────────────────────
# Whitelist of the physical-robot endpoints the tester may call (relative to /api/v1).
_ROBOT_TEST_PATHS = {
    "/setup", "/base/goto", "/base/state", "/base/pose", "/base/abort",
    "/capture", "/arm/command", "/arm/state", "/arm/abort",
    "/screen/click", "/card/pick", "/card/tap", "/card/replace",
}


class RobotTestCall(BaseModel):
    method: str                       # "GET" | "POST"
    path:   str                       # e.g. "/arm/state" (relative to /api/v1)
    body:   Optional[dict] = None
    timeout: Optional[float] = None
    target: Optional[str] = None      # "agv" | "arm" — which controller to hit (auto by path if unset)


@app.post("/api/robot/test-call")
def robot_test_call(req: RobotTestCall):
    """Proxy a single Robot API call for the Robot Setup 'API Tester'.

    Routes to the AGV controller for /base/* (or when target='agv'), otherwise to the arm
    controller — each of which may be on a different IP. Returns the HTTP status + parsed
    response body. Runs server-side so the browser never needs the robot's address.
    """
    import requests as _rq
    import time as _t
    method = (req.method or "GET").upper()
    path   = req.path if req.path.startswith("/") else "/" + req.path
    if path not in _ROBOT_TEST_PATHS:
        raise HTTPException(400, f"Unknown robot endpoint: {path!r}")
    if method not in ("GET", "POST"):
        raise HTTPException(400, f"Unsupported method: {method!r}")

    # Which controller: explicit target wins; else /base/* → AGV, everything else → arm.
    target = (req.target or "").strip().lower()
    use_agv = target == "agv" or (target != "arm" and path.startswith("/base"))
    base    = settings.agv_api_base() if use_agv else settings.arm_api_base()
    url     = f"{base}{path}"
    timeout = req.timeout or (30.0 if path == "/capture" else 12.0)
    t0 = _t.time()
    try:
        if method == "GET":
            resp = _rq.get(url, timeout=timeout)
        else:
            resp = _rq.post(url, json=(req.body or {}), timeout=timeout)
    except Exception as e:
        return {"ok": False, "url": url, "method": method,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((_t.time() - t0) * 1000, 1)}
    elapsed = round((_t.time() - t0) * 1000, 1)
    try:
        body = resp.json()
    except Exception:
        body = {"_raw_text": resp.text[:5000]}
    return {"ok": resp.ok, "url": url, "method": method,
            "status_code": resp.status_code, "elapsed_ms": elapsed,
            "response_body": body}


# ── Camera Vision Test (dedicated page) ──────────────────────────────────────
# Diagnostics to judge whether REAL robot-camera frames are good enough for the automation:
#   1. call the arm /capture API with type raw|screen (per the Robot API spec) and show the frame;
#   2. run the SAME detection pipeline the App Explorer / validation uses on that frame —
#      aHash screen match (Tier-1, 0 LLM), OpenCV boundary detection (0 LLM), OCR (0 LLM), and
#      optionally the Claude-vision element analysis (Tier-3) — and report which tier the image
#      supports. The rest of the automation relies on how much we can extract from these frames.

def _vision_test_dir() -> Path:
    # Dedicated, persistent folder for Camera Vision Test frames (raw/screen captures, uploads, and
    # their enhanced variants). Kept at the PROJECT ROOT (sibling to screenshots/) — NOT under
    # screenshots/ — so an App Explorer "clear all" (which nukes top-level screenshot files) never
    # wipes captured camera frames. Every frame the /capture and /upload endpoints return is saved
    # here automatically for later inspection.
    d = Path(tenant_paths.app_map_path()).parent / "camera_captures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _coords_export_dir() -> Path:
    # Dedicated project-root folder for the Camera Vision Test coordinate exports (one .xlsx per run of
    # the tool). Sibling to camera_captures/ so an App Explorer "clear all" never touches it.
    d = Path(tenant_paths.app_map_path()).parent / "coordinate_exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _export_coords_to_excel(coords: dict, frame_filename: str) -> Optional[str]:
    """Write the element coordinate table to a NEW timestamped .xlsx under coordinate_exports/ every time
    the Camera Vision Test computes coordinates. Never overwrites (unique timestamp + counter). Returns
    the saved filename, or None on failure (a diagnostic export must never break the API response)."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        screen_id = (coords.get("screen_id") or "screen").replace("/", "_")
        stem = Path(frame_filename).stem[:40]
        # Millisecond-precision stamp; a counter suffix guarantees a NEW file even on same-ms calls.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        export_dir = _coords_export_dir()
        fname = f"coords_{screen_id}_{stem}_{stamp}.xlsx"
        _n = 1
        while (export_dir / fname).exists():
            fname = f"coords_{screen_id}_{stem}_{stamp}_{_n}.xlsx"
            _n += 1

        wb = Workbook()
        ws = wb.active
        ws.title = "Click Coordinates"
        cal = coords.get("calibration", {})
        # Header / context block
        meta = [
            ("Camera Vision Test — element click coordinates (u,v)", ""),
            ("Exported", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("Screen", f"{coords.get('screen_id','')}  (source: {coords.get('source','')})"),
            ("Frame", frame_filename),
            ("Camera frame (px)", f"{coords.get('camera_width','')} x {coords.get('camera_height','')}"),
            ("Exploration viewport (px)", f"{coords.get('viewport_width','')} x {coords.get('viewport_height','')}"),
            ("Vertical calibration", f"source={cal.get('source','')}  ay={cal.get('ay','')}  by={cal.get('by','')}"
                                     + (f"  knots={cal.get('knots')}" if cal.get("knots") else "")),
        ]
        r = 0
        for label, val in meta:
            r += 1
            ws.cell(row=r, column=1, value=label).font = Font(bold=True)
            ws.cell(row=r, column=2, value=val)
        r += 2   # blank spacer row before the table

        # Table header
        headers = ["Element ID", "Type", "Label",
                   "Viewport X", "Viewport Y", "Click U (camera)", "Click V (camera)",
                   "BBox camera [u1,v1,u2,v2]"]
        for c, h in enumerate(headers, start=1):
            ws.cell(row=r, column=c, value=h).font = Font(bold=True)
        for e in coords.get("elements", []):
            cv = e.get("center_viewport") or ["", ""]
            cc = e.get("center_camera") or ["", ""]
            bb = e.get("bbox_camera")
            ws.append([e.get("id", ""), e.get("type", ""), e.get("label", ""),
                       cv[0], cv[1], cc[0], cc[1],
                       (",".join(str(x) for x in bb) if bb else "")])
        # Column widths
        for col, w in zip("ABCDEFGH", (28, 10, 20, 11, 11, 16, 16, 26)):
            ws.column_dimensions[col].width = w

        wb.save(export_dir / fname)
        return fname
    except Exception as e:
        print(f"  [VISION-TEST] coordinate Excel export skipped: {type(e).__name__}: {e}")
        return None


def _img_dims(img_bytes: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image as _Img
        return _Img.open(io.BytesIO(img_bytes)).size
    except Exception:
        return None, None


def _bound_for_processing(img_bytes: bytes, max_dim: int = 1600) -> bytes:
    """Downscale a frame so its largest side is ≤ max_dim, for the EXPENSIVE 0-LLM steps
    (OpenCV edge detection, OCR, and the enhance pipeline). Returns the ORIGINAL bytes unchanged
    when it already fits (or on any failure) — so real robot-camera frames (~600px) and browser
    screenshots (~1400px) are byte-identical and nothing about their detection changes.

    Why: a phone photo comes in at ~4032×3024 (12 MP); the enhance pass then upscales it 3× to
    ~110 MP and runs fastNlMeansDenoising + triple-PSM OCR on THAT — minutes of work, which blew
    past the 90 s client timeout. RealSense frames never hit this. Capping the working resolution
    keeps the diagnostic responsive on huge uploads with zero loss for the tiny frames it's built for.
    (Tier-1 aHash is deliberately NOT bounded here — compute_hash resizes to 16×16 itself, so it
    already sees the full frame and its match semantics are preserved.)"""
    try:
        from PIL import Image as _Img
        img = _Img.open(io.BytesIO(img_bytes))
        w, h = img.size
        if max(w, h) <= max_dim:
            return img_bytes
        scale = max_dim / float(max(w, h))
        img = img.convert("RGB").resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                        _Img.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return img_bytes


# Common Tesseract-engine install locations, checked when it isn't on PATH (Windows installers put
# it under Program Files but don't add it to PATH — the usual cause of "tesseract is not installed").
_TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
    "/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract",
]


def _resolve_tesseract() -> str:
    """Return a runnable Tesseract-engine path, or "" if none found.

    Order: explicit settings.tesseract_cmd → on PATH (shutil.which) → common install locations.
    When found, points pytesseract at it so image_to_string works even when the binary isn't on PATH.
    """
    import shutil
    candidates = []
    if settings.tesseract_cmd:
        candidates.append(settings.tesseract_cmd)
    on_path = shutil.which("tesseract")
    if on_path:
        candidates.append(on_path)
    candidates.extend(_TESSERACT_CANDIDATES)
    for c in candidates:
        if c and Path(c).is_file():
            try:
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = c
            except Exception:
                pass
            return c
    return ""


def _run_ocr(img_bytes: bytes) -> dict:
    """Full-image OCR (0 LLM). Returns {available, text, error, engine}.

    Distinguishes the two failure modes the old code conflated: the pytesseract PACKAGE missing vs
    the Tesseract ENGINE binary missing — the latter is what actually happened (package installed,
    binary present at Program Files but not on PATH). Gives an actionable message in each case."""
    try:
        import pytesseract
        from PIL import Image as _Img
    except ImportError:
        return {"available": False, "text": "", "engine": "",
                "error": "pytesseract package not installed on the backend (pip install pytesseract)."}
    engine = _resolve_tesseract()
    if not engine:
        return {"available": False, "text": "", "engine": "",
                "error": ("Tesseract ENGINE not found. Install it (Windows: winget install "
                          "UB-Mannheim.TesseractOCR) or set tesseract_cmd in .env to tesseract.exe. "
                          "The pytesseract pip package is only a wrapper around this binary.")}
    try:
        img = _Img.open(io.BytesIO(img_bytes))
        # Try a few page-segmentation modes and keep the longest read. On a soft/low-res camera frame
        # the default fully-automatic mode (psm 3) often returns nothing, while a uniform-block (6) or
        # sparse-text (11) pass recovers partial text — this makes OCR a best-effort read, not all-or-none.
        best = ""
        for psm in (3, 6, 11):
            try:
                t = pytesseract.image_to_string(img, config=f"--psm {psm}").strip()
            except Exception:
                t = ""
            if len(t) > len(best):
                best = t
        return {"available": True, "text": best, "engine": engine, "error": ""}
    except Exception as e:
        return {"available": True, "text": "", "engine": engine, "error": f"{type(e).__name__}: {e}"}


def _enhance_for_ocr(img_bytes: bytes) -> Optional[bytes]:
    """Return a PNG of an OpenCV-preprocessed copy tuned to make cheap-tier reads (OCR / edge
    detection) work better on a soft, low-res, glare-y camera photo. Returns None if OpenCV/decoding
    fails (caller then just skips enhancement — no behaviour change).

    Pipeline (all local, OpenCV already a dependency): grayscale → 3× cubic upscale (recover small
    text) → CLAHE (local contrast, fights glare/uneven lighting + the blue color cast) → light
    denoise → unsharp mask (counter blur). Deliberately no hard threshold: binarizing this frame
    destroyed more text than it recovered in testing."""
    try:
        import cv2
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return None
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        # 3× upscale to recover small text on a low-res camera frame — but cap the RESULT so a
        # large source (e.g. a downscaled phone photo already ~1600px) can't blow up to a size that
        # makes the denoise step crawl. min(3.0, 2400/longest) keeps a 600px frame at exactly 3×
        # (unchanged behaviour) while bounding anything bigger to a 2400px working image.
        h0, w0 = gray.shape[:2]
        f = min(3.0, 2400.0 / max(h0, w0)) if max(h0, w0) else 3.0
        f = max(1.0, f)
        up = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        cl = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(up)
        den = cv2.fastNlMeansDenoising(cl, None, 7, 7, 21)
        blur = cv2.GaussianBlur(den, (0, 0), 3)
        sharp = cv2.addWeighted(den, 1.6, blur, -0.6, 0)
        ok, buf = cv2.imencode(".png", sharp)
        return buf.tobytes() if ok else None
    except Exception:
        return None


class VisionCaptureRequest(BaseModel):
    capture_type: str = "screen"   # "screen" (AprilTag-rectified) | "raw" (unrectified sensor frame)


@app.post("/api/vision-test/capture")
def vision_test_capture(req: VisionCaptureRequest):
    """Call the arm controller's /capture (spec `type` = raw|screen) and save the returned frame.

    Hits the arm controller directly (settings.arm_api_base) so the diagnostic works whenever the
    arm is reachable, regardless of the active robot_backend. Pure diagnostic — it does NOT alter
    real_robot's live calibration or the screen-localization latch."""
    import requests as _rq
    import time as _t
    import base64 as _b64
    ctype = (req.capture_type or "screen").lower()
    if ctype not in ("screen", "raw"):
        raise HTTPException(400, "capture_type must be 'screen' or 'raw'")
    base   = settings.arm_api_base()
    url    = f"{base}/capture"
    cmd_id = f"vision-test-{ctype}-{int(_t.time() * 1000)}"
    t0 = _t.time()
    try:
        resp = _rq.post(url, json={"cmd_id": cmd_id, "type": ctype}, timeout=30.0)
    except Exception as e:
        raise HTTPException(502, f"Capture failed calling {url}: {type(e).__name__}: {e}")
    elapsed = round((_t.time() - t0) * 1000, 1)
    if not resp.ok:
        raise HTTPException(502, f"Capture returned HTTP {resp.status_code} from {url}: {resp.text[:300]}")
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(502, "Capture response was not JSON")
    b64 = data.get("image_b64")
    if not b64:
        raise HTTPException(502, "Capture response had no 'image_b64'")
    try:
        img_bytes = _b64.b64decode(b64)
    except Exception as e:
        raise HTTPException(502, f"'image_b64' decode failed: {e}")

    fname = f"vision_test_{ctype}_{int(_t.time() * 1000)}.png"
    (_vision_test_dir() / fname).write_bytes(img_bytes)

    w, h = data.get("width"), data.get("height")
    if not (w and h):
        w, h = _img_dims(img_bytes)
    return {
        "status":       "ok",
        "capture_type": ctype,
        "filename":     fname,
        "image_url":    f"/api/vision-test/image/{fname}",
        "width":        w,
        "height":       h,
        "aspect":       round(w / h, 4) if (w and h) else None,
        "bytes":        len(img_bytes),
        "elapsed_ms":   elapsed,
        "controller":   base,
        "cmd_id":       cmd_id,
    }


@app.post("/api/vision-test/upload")
def vision_test_upload(file: UploadFile = File(...)):
    """Save an uploaded image (e.g. a frame already captured from the robot) so the SAME detection
    pipeline can run on it without a live robot — useful for offline assessment of camera frames."""
    import time as _t
    data = file.file.read()
    w, h = _img_dims(data)
    if not (w and h):
        raise HTTPException(400, "Uploaded file is not a readable image")
    ext   = (Path(file.filename or "").suffix or ".png").lower()
    if ext not in (".png", ".jpg", ".jpeg"):
        ext = ".png"
    fname = f"vision_test_upload_{int(_t.time() * 1000)}{ext}"
    (_vision_test_dir() / fname).write_bytes(data)
    return {
        "status":       "ok",
        "capture_type": "upload",
        "filename":     fname,
        "image_url":    f"/api/vision-test/image/{fname}",
        "width":        w,
        "height":       h,
        "aspect":       round(w / h, 4) if (w and h) else None,
        "bytes":        len(data),
        "elapsed_ms":   0.0,
        "controller":   "",
        "cmd_id":       "",
    }


@app.get("/api/vision-test/image/{filename}")
def vision_test_image(filename: str):
    from fastapi.responses import FileResponse
    path = _vision_test_dir() / Path(filename).name   # .name → no path traversal
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


# ── Reference-template library (camera-domain Tier-1 screen templates) ────────
# Build a per-screen library so real-robot screen identity is 0 LLM: navigate the robot to a screen,
# capture the current arm frame, and save it as "<screen_id>.png" in template_ref_dir. Template
# matching (vision_agent/vision/template_match.py) then compares live frames against these.

def _template_ref_dir() -> Path:
    d = Path(settings.template_ref_dir or "./reference_screens")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_screen_slug(s: str) -> str:
    """Normalise a screen_id to a safe filename stem (lowercase, alnum/_/- only). No path traversal."""
    import re as _re
    return _re.sub(r"[^a-z0-9_-]+", "_", (s or "").strip().lower()).strip("_-")


class SaveReferenceRequest(BaseModel):
    screen_id:    str
    capture_type: str = "screen"        # "screen" (AprilTag-rectified — normal) | "raw"
    filename:     Optional[str] = None  # save THIS already-captured vision-test frame; else capture fresh


@app.post("/api/vision-test/save-reference")
def vision_test_save_reference(req: SaveReferenceRequest):
    """Save a kiosk-screen frame as the reference template for `screen_id` (<screen_id>.png in
    template_ref_dir). If `filename` names an already-captured vision-test frame, that exact frame is
    saved (no re-capture); otherwise the CURRENT arm frame is captured fresh. Build the camera-domain
    library one screen at a time — navigate the robot to a screen, then call this. Overwrites an
    existing template for that screen. Returns the saved path + a self-match score (should be ~1.0)."""
    import requests as _rq
    import time as _t
    import base64 as _b64

    slug = _safe_screen_slug(req.screen_id)
    if not slug:
        raise HTTPException(400, "screen_id is required")
    ctype = (req.capture_type or "screen").lower()
    if ctype not in ("screen", "raw"):
        raise HTTPException(400, "capture_type must be 'screen' or 'raw'")

    data: dict = {}
    base = ""   # controller that served the frame ("" when saving an existing on-disk frame)
    if req.filename:
        # Save an already-captured vision-test frame (the one the operator is looking at).
        src = _vision_test_dir() / Path(req.filename).name   # .name → no traversal
        if not src.exists() or not src.is_file():
            raise HTTPException(404, f"Frame {req.filename!r} not found — capture or upload first")
        img_bytes = src.read_bytes()
    else:
        # Capture a fresh frame from the arm.
        base   = settings.arm_api_base()
        url    = f"{base}/capture"
        cmd_id = f"save-ref-{slug}-{int(_t.time() * 1000)}"
        try:
            resp = _rq.post(url, json={"cmd_id": cmd_id, "type": ctype}, timeout=30.0)
        except Exception as e:
            raise HTTPException(502, f"Capture failed calling {url}: {type(e).__name__}: {e}")
        if not resp.ok:
            raise HTTPException(502, f"Capture returned HTTP {resp.status_code} from {url}: {resp.text[:300]}")
        try:
            data = resp.json()
            img_bytes = _b64.b64decode(data["image_b64"])
        except Exception as e:
            raise HTTPException(502, f"Capture response missing/!decodable image_b64: {e}")

    dest = _template_ref_dir() / f"{slug}.png"
    dest.write_bytes(img_bytes)

    # Self-match sanity: the saved template vs itself should score ~1.0 (proves it's readable).
    self_score = None
    try:
        from vision_agent.vision.template_match import _to_bgr_from_bytes, template_match_score
        bgr = _to_bgr_from_bytes(img_bytes)
        self_score = round(template_match_score(bgr, bgr), 4)
    except Exception:
        pass

    w, h = data.get("width"), data.get("height")
    if not (w and h):
        w, h = _img_dims(img_bytes)
    return {
        "status":       "ok",
        "screen_id":    slug,
        "filename":     dest.name,
        "path":         str(dest),
        "image_url":    f"/api/vision-test/reference-image/{dest.name}",
        "width":        w,
        "height":       h,
        "bytes":        len(img_bytes),
        "capture_type": ctype,
        "self_score":   self_score,
        "controller":   base,
    }


@app.get("/api/vision-test/references")
def vision_test_references():
    """List the saved per-screen reference templates in template_ref_dir."""
    d = _template_ref_dir()
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    items = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.suffix.lower() not in exts:
            continue
        w, h = _img_dims(f.read_bytes())
        items.append({
            "screen_id": f.stem,
            "filename":  f.name,
            "image_url": f"/api/vision-test/reference-image/{f.name}",
            "width":     w,
            "height":    h,
            "bytes":     f.stat().st_size,
        })
    return {"status": "ok", "dir": str(d), "count": len(items), "references": items}


@app.get("/api/vision-test/reference-image/{filename}")
def vision_test_reference_image(filename: str):
    from fastapi.responses import FileResponse
    path = _template_ref_dir() / Path(filename).name   # .name → no path traversal
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


@app.delete("/api/vision-test/reference/{filename}")
def vision_test_delete_reference(filename: str):
    path = _template_ref_dir() / Path(filename).name   # .name → no path traversal
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    path.unlink()
    return {"status": "ok", "deleted": path.name}


class VisionAnalyzeRequest(BaseModel):
    filename:        str
    expected_screen: Optional[str] = None   # optional: also report the phash distance to this screen
    use_claude:      bool = False           # also run the Tier-3 Claude-vision element analysis
    screen_id:       Optional[str] = None   # optional: force which screen's element coords to convert


class ElementCoordsRequest(BaseModel):
    filename:  str
    screen_id: str                          # which app_map screen's elements to convert for this frame


# aHash distance thresholds — mirror validate_pipeline._match_by_phash so the verdict here matches
# what a real run's Tier-1 screen check would conclude.
_PHASH_MATCH_THRESHOLD    = 8
_PHASH_MISMATCH_THRESHOLD = 20


def _diag_scale_point(x: int, y: int, cam_w: float, cam_h: float,
                      ay: Optional[float] = None, by: Optional[float] = None,
                      vknots: Optional[list] = None) -> tuple[int, int]:
    """Mirror of vision_agent.robot.real_robot._scale (viewport→camera + per-axis calibration), for the
    Camera Vision Test diagnostic ONLY — computed here so the live tap path (real_robot.py) is never
    touched. Kept in LOCKSTEP with _scale.

    The vertical mapping prefers a per-pose PIECEWISE map (`vknots`: email/password/sign-in/footer
    knots) when provided — the same 4-anchor self-calibration the runtime derives, so the tool shows the
    exact (u,v) the robot would tap including the footer fix. Absent vknots it is the affine (ay, by),
    defaulting to settings.camera_calib_ay/by. cam_w/cam_h = the captured frame's measured size."""
    fx = settings.camera_calib_ax * (x / settings.viewport_width) + settings.camera_calib_bx
    if vknots:
        from vision_agent.vision.screen_calibrate import eval_vmap
        fy = eval_vmap(vknots, y / settings.viewport_height)
    else:
        ay = settings.camera_calib_ay if ay is None else ay
        by = settings.camera_calib_by if by is None else by
        fy = ay * (y / settings.viewport_height) + by
    return int(round(fx * cam_w)), int(round(fy * cam_h))


def _element_coords_for_screen(screen: dict, screen_id: str, source: str,
                               cam_w: int, cam_h: int, image_bytes: Optional[bytes] = None) -> dict:
    """Convert every app_map element CENTER (and bbox, if present) of `screen` from the exploration
    viewport to this camera frame, using the SAME math the runtime uses to build a Click-API tap.
    These (u,v) are the exact pixels sent to the robot for each element on this screen.

    When the screen is a login-type screen (email + password inputs) and auto_tap_calibration is on,
    the vertical affine is DERIVED from this frame's own input boxes (self-calibration) — matching what
    the real backend does per pose — instead of the static CAMERA_CALIB_AY/BY, so the shown coordinates
    land on element centres regardless of how the rectification cropped this particular frame."""
    # Per-pose vertical self-calibration (login screens only; falls back to config when not confident).
    # Prefers the 4-anchor PIECEWISE map (email/password/sign-in/footer) so footer-link coordinates land
    # on their labels; falls back to the 2-point affine, then to the static config calibration.
    ay = by = None
    vknots = None
    calib_source = "config"
    calib_extra: dict = {}
    if settings.auto_tap_calibration and image_bytes:
        try:
            from vision_agent.vision.screen_calibrate import find_login_anchor_fracs, derive_login_vmap
            anchors = find_login_anchor_fracs(screen, settings.viewport_height)
            if anchors:
                res = derive_login_vmap(image_bytes, anchors)
                if res:
                    ay, by = res["ay"], res["by"]
                    calib_extra = {"email_cam_frac": round(res["email_cam_frac"], 4),
                                   "password_cam_frac": round(res["password_cam_frac"], 4)}
                    if res["kind"] == "vmap4":
                        vknots = res["knots"]
                        calib_source = "auto-vmap4"
                        calib_extra.update({"signin_cam_frac": round(res["signin_cam_frac"], 4),
                                            "footer_cam_frac": round(res["footer_cam_frac"], 4)})
                    else:
                        calib_source = "auto"
        except Exception:
            pass
    eff_ay = settings.camera_calib_ay if ay is None else ay
    eff_by = settings.camera_calib_by if by is None else by

    elements = []
    for e in (screen.get("elements") or []):
        c = e.get("center")
        if not c or len(c) < 2:
            continue
        cx, cy = int(round(float(c[0]))), int(round(float(c[1])))
        u, v = _diag_scale_point(cx, cy, cam_w, cam_h, ay, by, vknots)
        bbox_camera = None
        bb = e.get("bbox")
        if bb and len(bb) >= 4:
            u1, v1 = _diag_scale_point(int(round(float(bb[0]))), int(round(float(bb[1]))), cam_w, cam_h, ay, by, vknots)
            u2, v2 = _diag_scale_point(int(round(float(bb[2]))), int(round(float(bb[3]))), cam_w, cam_h, ay, by, vknots)
            bbox_camera = [u1, v1, u2, v2]
        elements.append({
            "id":              e.get("id", ""),
            "type":            e.get("type", ""),
            "label":           e.get("label", ""),
            "center_viewport": [cx, cy],
            "center_camera":   [u, v],
            "bbox_camera":     bbox_camera,
        })
    note = ("These (u,v) are the exact camera pixels the live runtime sends to the Robotics Click API "
            "for each element on this screen: app_map viewport center → viewport→camera scale (frame "
            "size ÷ exploration viewport) → per-axis vertical calibration. ")
    if calib_source == "auto-vmap4":
        note += ("The vertical mapping was AUTO-DERIVED from this login frame using FOUR anchors it "
                 "detected — the email box, password box, Sign In button, and the footer link row — and "
                 "interpolated as a piecewise curve (the same self-calibration the robot does per pose). "
                 "This anchors the BOTTOM of the screen, so the footer links (Sign up / Forgot password / "
                 "Developer settings) land on their labels instead of extrapolating too high.")
    elif calib_source == "auto":
        note += ("The vertical calibration was AUTO-DERIVED from this login frame's email/password boxes "
                 "(2-anchor affine; the Sign In / footer anchors weren't confidently measurable on this "
                 "frame, so footer links use the affine extrapolation and may read slightly high).")
    else:
        note += ("The vertical calibration is the STATIC CAMERA_CALIB_AY/BY (this screen has no login "
                 "boxes to self-calibrate from, or auto-calibration is off) — accurate only when this "
                 "frame's pose matches the one those constants were derived for.")
    calib = {
        "ax": settings.camera_calib_ax, "bx": settings.camera_calib_bx,
        "ay": round(eff_ay, 4), "by": round(eff_by, 4),
        "source": calib_source, **calib_extra,
    }
    if vknots:
        calib["knots"] = [[round(m, 4), round(c, 4)] for m, c in vknots]
    return {
        "screen_id":         screen_id,
        "source":            source,
        "camera_width":      cam_w,
        "camera_height":     cam_h,
        "viewport_width":    settings.viewport_width,
        "viewport_height":   settings.viewport_height,
        "calibration":       calib,
        "elements": elements,
        "note": note,
    }


@app.post("/api/vision-test/analyze")
def vision_test_analyze(req: VisionAnalyzeRequest):
    """Run the REAL detection pipeline on a captured/uploaded frame and report tier suitability:

      • Tier-1 aHash screen match  — compute_hash + compare to every app_map screen_hash (0 LLM).
        Ranks all screens by Hamming distance; a best distance ≤ 8 means Tier-1 alone can identify
        the screen from this camera frame (no Claude needed).
      • OpenCV boundary detection  — detect_interactive_rects (0 LLM): how many interactive
        rectangles are found purely from edges (a proxy for element-boundary clarity).
      • OCR text                   — pytesseract full-image read (0 LLM), if installed.
      • Claude vision (optional)   — analyze_image_elements: the Tier-3 element extraction the App
        Explorer uses, so you can see labelled elements + coordinates the model reads from the frame.

    Everything runs on the frame the /capture (or upload) endpoint saved — same code the live
    system uses, no reimplementation."""
    from vision_agent.screen_cache import compute_hash
    from app_map import store as app_map_store

    path = _vision_test_dir() / Path(req.filename).name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"Frame {req.filename!r} not found — capture or upload first")
    img_bytes = path.read_bytes()
    img_w, img_h = _img_dims(img_bytes)

    # Bound the working copy for the EXPENSIVE steps (OpenCV / OCR / enhance / Claude) so a large
    # upload (e.g. a 12 MP phone photo) doesn't exceed the client timeout. Real camera frames and
    # browser screenshots are already under the cap → this returns them unchanged (no regression).
    # aHash below stays on the FULL-res original (compute_hash resizes to 16×16 itself).
    proc_bytes = _bound_for_processing(img_bytes)

    # ── 1. Tier-1 aHash screen match (0 LLM) ──────────────────────────────────
    try:
        current_hash = compute_hash(img_bytes)
    except Exception as e:
        raise HTTPException(500, f"Could not hash image: {e}")

    app_map = app_map_store.load(tenant_paths.app_map_path()) if Path(tenant_paths.app_map_path()).exists() else {"screens": {}}
    screens = app_map.get("screens") or {}

    def _hamming(a: str, b: str) -> int:
        return sum(c1 != c2 for c1, c2 in zip(a, b))

    ranking = []
    for sid, sc in screens.items():
        h = (sc or {}).get("screen_hash", "")
        if not h:
            continue
        ranking.append({
            "screen_id":  sid,
            "app_id":     sc.get("app_id", ""),
            "distance":   _hamming(current_hash, h),
            "is_dynamic": bool(sc.get("is_dynamic", False)),
        })
    ranking.sort(key=lambda r: r["distance"])

    best = ranking[0] if ranking else None
    if best is None:
        tier1_verdict = "no_reference"
        tier1_detail  = "No app_map screen hashes to compare against — run the App Explorer first."
    elif best["distance"] <= _PHASH_MATCH_THRESHOLD:
        tier1_verdict = "match"
        tier1_detail  = (f"Tier-1 identifies this as '{best['screen_id']}' "
                         f"(distance {best['distance']} ≤ {_PHASH_MATCH_THRESHOLD}) — no Claude needed.")
    elif best["distance"] > _PHASH_MISMATCH_THRESHOLD:
        tier1_verdict = "no_match"
        tier1_detail  = (f"Nearest screen '{best['screen_id']}' is {best['distance']} away "
                         f"(> {_PHASH_MISMATCH_THRESHOLD}) — aHash cannot identify the screen from this "
                         f"frame; Tier-2/3 (Claude vision) is required. NOTE: with the best distance in "
                         f"the mismatch band, the ranking ORDER below is not meaningful — aHash is a 16×16 "
                         f"global-brightness fingerprint, so it ranks by coarse light/dark layout, NOT by "
                         f"fields or their order. A camera photo compared against browser-captured "
                         f"references can rank an unrelated screen (even another kiosk's) nearest purely by "
                         f"a similar bright-panel-on-dark shape; '{best['screen_id']}' being #1 is not a real "
                         f"content match. See the recommendation for how to make Tier-1 viable.")
    else:
        tier1_verdict = "inconclusive"
        tier1_detail  = (f"Nearest screen '{best['screen_id']}' is {best['distance']} away "
                         f"(between {_PHASH_MATCH_THRESHOLD} and {_PHASH_MISMATCH_THRESHOLD}) — "
                         f"borderline; Claude-vision fallback would run.")

    expected_distance = None
    if req.expected_screen and req.expected_screen in screens and screens[req.expected_screen].get("screen_hash"):
        expected_distance = _hamming(current_hash, screens[req.expected_screen]["screen_hash"])

    # ── 1b. Tier-1 TEMPLATE MATCHING (0 LLM) — the robust screen identifier ────
    # Normalized cross-correlation (TM_CCOEFF_NORMED) against each screen's reference image. Unlike
    # aHash, it bridges the camera↔browser domain gap, so this is the primary real-robot Tier-1.
    template_match = None
    try:
        from vision_agent.vision.template_match import (
            build_references, identify_screen, settings_center_crop,
        )
        t_refs = build_references(app_map, settings.template_ref_dir)
        t_res  = identify_screen(
            proc_bytes, t_refs, req.expected_screen or "",
            settings.template_match_threshold, settings.template_match_margin,
            center_crop=settings_center_crop(),
        )
        template_match = {
            "method":          t_res.get("method"),
            "success":         t_res.get("success"),
            "best":            (t_res.get("ranking") or [None])[0],
            "score":           t_res.get("score"),
            "ranking":         (t_res.get("ranking") or [])[:10],
            "reference_count": len(t_refs),
            "reference_dir":   settings.template_ref_dir or "",
            "threshold":       settings.template_match_threshold,
            "margin":          settings.template_match_margin,
        }
    except Exception as e:
        template_match = {"error": f"{type(e).__name__}: {e}", "ranking": []}

    # ── 2. OpenCV boundary detection (0 LLM) ──────────────────────────────────
    opencv_rects: list[dict] = []
    opencv_error = ""
    try:
        from PIL import Image as _Img
        from vision_agent.vision.detector import detect_interactive_rects
        opencv_rects = detect_interactive_rects(_Img.open(io.BytesIO(proc_bytes)))
    except Exception as e:
        opencv_error = f"{type(e).__name__}: {e}"

    # ── 3. OCR text (0 LLM) — on the raw frame, same as a live read ───────────
    ocr = _run_ocr(proc_bytes)

    # ── 3b. Image-enhancement pass (0 LLM) — local OpenCV preprocessing to give the cheap tiers a
    #        better shot on a soft/low-res/glare-y camera photo. Re-runs OCR + OpenCV + aHash on the
    #        enhanced copy so the operator can see, side by side, whether preprocessing helps. Saved
    #        under camera_captures/ too so the enhanced frame can be displayed and inspected. ──────
    enhanced = None
    enh_bytes = _enhance_for_ocr(proc_bytes)
    if enh_bytes:
        enh_fname = Path(req.filename).stem + "_enhanced.png"
        (_vision_test_dir() / enh_fname).write_bytes(enh_bytes)
        enh_w, enh_h = _img_dims(enh_bytes)
        enh_ocr = _run_ocr(enh_bytes)
        enh_rects: list[dict] = []
        try:
            from PIL import Image as _Img
            from vision_agent.vision.detector import detect_interactive_rects
            enh_rects = detect_interactive_rects(_Img.open(io.BytesIO(enh_bytes)))
        except Exception:
            enh_rects = []
        try:
            enh_hash = compute_hash(enh_bytes)
            enh_best = min(
                ({"screen_id": sid, "distance": _hamming(enh_hash, (sc or {}).get("screen_hash", ""))}
                 for sid, sc in screens.items() if (sc or {}).get("screen_hash")),
                key=lambda r: r["distance"], default=None)
        except Exception:
            enh_best = None
        enhanced = {
            "applied":      True,
            "filename":     enh_fname,
            "image_url":    f"/api/vision-test/image/{enh_fname}",
            "width":        enh_w,
            "height":       enh_h,
            "pipeline":     "grayscale → 3× upscale → CLAHE → denoise → unsharp",
            "ocr":          enh_ocr,
            "opencv_count": len(enh_rects),
            "tier1_best":   enh_best,
        }

    # ── 4. Claude-vision element analysis (Tier-3, optional) ──────────────────
    claude = None
    if req.use_claude:
        try:
            from vision_agent.nodes.analyze import analyze_image_elements
            claude = analyze_image_elements(proc_bytes)
        except Exception as e:
            claude = {"error": f"{type(e).__name__}: {e}", "elements": []}

    # ── Overall recommendation ────────────────────────────────────────────────
    if tier1_verdict == "match":
        recommendation = ("Tier-1 (perceptual-hash) screen identification WORKS on this camera frame "
                          "— steady-state validation needs 0 LLM calls for screen identity.")
    elif tier1_verdict == "no_reference":
        recommendation = ("No reference hashes yet — explore the app first, then re-capture to test "
                          "Tier-1. Element identity meanwhile relies on Claude vision (Tier-3).")
    else:
        recommendation = ("Tier-1 aHash does NOT reliably match this camera frame against the "
                          "browser-captured references — expected, since a camera photo differs from a "
                          "clean screenshot (glare, blur, perspective, color). To make Tier-1 viable on "
                          "the real robot, the reference hashes must be CAMERA-domain: after Playwright "
                          "exploration, do a one-time per-screen camera-reference pass so aHash compares "
                          "camera↔camera (not camera↔browser). Meanwhile screen validation falls through "
                          "to Claude vision (Tier-3), which reads the frame reliably. Element COORDINATES "
                          "still come from the app_map (learned in Playwright), so taps are unaffected; "
                          "only screen/text VALIDATION pays an LLM call. The enhanced-frame results below "
                          "show whether local preprocessing recovers OCR/edges on this capture.")

    # Lead the recommendation with the TEMPLATE-MATCH verdict (the primary real-robot Tier-1),
    # then keep the aHash guidance below it for continuity.
    _tb = template_match.get("best") if isinstance(template_match, dict) else None
    _ts = template_match.get("success") if isinstance(template_match, dict) else None
    if _ts is True and _tb:
        template_reco = (f"Tier-1 TEMPLATE MATCHING identifies this as '{_tb['screen_id']}' "
                         f"(score {_tb['score']} ≥ {settings.template_match_threshold}) — 0-LLM screen "
                         f"identity. This is the primary real-robot screen identifier; it is robust to "
                         f"the camera↔browser gap (normalized cross-correlation), unlike aHash below.")
    elif _tb:
        template_reco = (f"Tier-1 template matching ranks '{_tb['screen_id']}' highest (score {_tb['score']}), "
                         f"but under the {settings.template_match_threshold} match floor (margin "
                         f"{settings.template_match_margin}). Add a cleaner per-screen reference template "
                         f"(drop a file named after the screen into template_ref_dir) to push it over — "
                         f"template matching still discriminates far better than the aHash below.")
    else:
        template_reco = ("No template references available. Set template_ref_dir to a folder of per-screen "
                         "templates (each filename containing its screen_id, e.g. Login_page.png → 'login'), "
                         "or explore the app so reference_screenshot images exist.")
    recommendation = template_reco + "  " + recommendation

    # ── 5. Element camera coordinates (the exact (u,v) sent to the Robotics Click API) ────────────
    # For the identified/selected screen, convert each app_map element CENTER from the exploration
    # viewport to THIS camera frame with the same math the runtime uses, so the operator can see the
    # precise pixel the robot would tap for every UI element on the screen. Resolution order:
    #   explicit request screen_id → expected_screen → template-match best → aHash best.
    element_coords = None
    if img_w and img_h and screens:
        coord_sid, coord_src = "", ""
        if req.screen_id and req.screen_id in screens:
            coord_sid, coord_src = req.screen_id, "requested"
        elif req.expected_screen and req.expected_screen in screens:
            coord_sid, coord_src = req.expected_screen, "expected"
        elif _tb and _tb.get("screen_id") in screens:
            coord_sid, coord_src = _tb["screen_id"], "template_match"
        elif best and best.get("screen_id") in screens:
            coord_sid, coord_src = best["screen_id"], "ahash"
        if coord_sid:
            element_coords = _element_coords_for_screen(screens[coord_sid], coord_sid, coord_src,
                                                        img_w, img_h, image_bytes=img_bytes)
            # Auto-save the coordinate table to a NEW timestamped Excel file each run of the tool.
            element_coords["excel_export"] = _export_coords_to_excel(element_coords, req.filename)

    return {
        "status":       "ok",
        "filename":     req.filename,
        "width":        img_w,
        "height":       img_h,
        "current_hash": current_hash,
        "template_match": template_match,
        "element_coords": element_coords,
        "tier1": {
            "verdict":           tier1_verdict,
            "detail":            tier1_detail,
            "best":              best,
            "ranking":           ranking[:10],
            "expected_screen":   req.expected_screen or "",
            "expected_distance": expected_distance,
            "match_threshold":   _PHASH_MATCH_THRESHOLD,
            "mismatch_threshold": _PHASH_MISMATCH_THRESHOLD,
        },
        "opencv": {
            "count": len(opencv_rects),
            "rects": opencv_rects,
            "error": opencv_error,
        },
        "ocr":            ocr,
        "enhanced":       enhanced,
        "claude":         claude,
        "recommendation": recommendation,
    }


@app.post("/api/vision-test/element-coords")
def vision_test_element_coords(req: ElementCoordsRequest):
    """Convert a chosen app_map screen's element centers to camera pixels for a captured frame.

    A fast, 0-LLM companion to /vision-test/analyze: given the frame + a screen_id, returns the exact
    (u,v) the Robotics Click API would receive for each UI element on that screen (same conversion the
    live runtime uses). Lets the operator switch which screen's coordinates to inspect without re-running
    the whole detection pipeline."""
    from app_map import store as app_map_store

    path = _vision_test_dir() / Path(req.filename).name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"Frame {req.filename!r} not found — capture or upload first")
    img_bytes = path.read_bytes()
    img_w, img_h = _img_dims(img_bytes)
    if not img_w or not img_h:
        raise HTTPException(400, "Could not read the frame dimensions")

    app_map = app_map_store.load(tenant_paths.app_map_path()) if Path(tenant_paths.app_map_path()).exists() else {"screens": {}}
    screens = app_map.get("screens") or {}
    if req.screen_id not in screens:
        raise HTTPException(404, f"Screen {req.screen_id!r} is not in the app map")

    coords = _element_coords_for_screen(screens[req.screen_id], req.screen_id, "requested",
                                        img_w, img_h, image_bytes=img_bytes)
    # Auto-save the coordinate table to a NEW timestamped Excel file each time the tool runs.
    coords["excel_export"] = _export_coords_to_excel(coords, req.filename)
    return {
        "status": "ok",
        "filename": req.filename,
        "width": img_w,
        "height": img_h,
        "element_coords": coords,
    }


@app.get("/api/vision-test/coords-export/{filename}")
def vision_test_coords_export(filename: str):
    """Download a saved Camera Vision Test coordinate Excel export from coordinate_exports/."""
    from fastapi.responses import FileResponse
    path = _coords_export_dir() / Path(filename).name   # .name → no path traversal
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"Export {filename!r} not found")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


# ── TC Plan (Claude-powered, cached) ─────────────────────────────────────────

_TC_PLAN_PROMPT = """\
You are a test automation planner for a physical kiosk touchscreen testing system.

DEVICE MAP (a single robot moves to each device before interacting with its touchscreen):
{device_map}

A robotic arm physically moves to the device's position, then taps coordinates on its touchscreen.
Playwright (headless browser) is used as a substitute during development — same tap coordinates.

CHANNEL DEFINITIONS (follow these exactly):
- "robot": ANY interaction with a physical device touchscreen from the device map above.
  Includes tapping buttons, typing on on-screen keyboards, navigating menus.
  Even if the word "click", "enter", "select", or "navigate" is used — if the target is
  a physical device screen, the channel is "robot", not "web".
- "web": ONLY interactions with external web applications that are NOT a physical device
  (e.g. a CRM system, backend admin portal, browser-based management dashboard).
- "db": Direct database queries, SQL checks, or backend record verification.
- "validation": Assertions, verifications, checking expected outcomes, confirming state.

ROBOT ACTIONS — map each raw step to the RIGHT action (the runtime already knows the robot APIs):
- Touchscreen interactions on a device (channel "robot"): tap a button/element → action "tap";
  enter/type text into a field → action "type" (with px/py + value); screen-content assertions →
  action "verify".
- AGV / MOBILE BASE movement (channel "robot") — the robot drives itself between devices:
    • "Move the AGV/base/robot to <device>" (or "go to", "navigate to", "drive to") →
        {{"action":"move","channel":"robot","target":"<device alias, e.g. VPS>","description":"..."}}
      Use the DEVICE ALIAS from the Device Map as "target"; the runtime resolves it to that device's
      kiosk_id and calls the AGV goto API with that kiosk_id. Reserved target "home" returns to base.
    • "Check the state/status of the AGV|arm" →
        {{"action":"check_state","channel":"validation","target":"agv"|"arm","expected_state":"idle"}}
      Reads the base/arm status API; omit expected_state to just record it, or set it (e.g. "idle") to assert.
    • "Wait N seconds" → {{"action":"wait","channel":"robot","seconds":<N>,"description":"..."}}
  These base/wait/state steps involve NO screen and NO screenshot — do NOT add px/py/element_id/
  screen_id to them, and do NOT add a screen "verify" for a pure AGV movement.

KIOSK APP MAP (screens and elements with exact pixel coordinates):
{element_inventory}

THE APP MAP ABOVE IS THE COMPLETE AND ONLY SOURCE OF TRUTH FOR THIS APP — READ IT FIRST:
- Use ONLY screen_ids and element_ids that appear in it, verbatim. Never invent a screen, element,
  id, or coordinate that is not listed.
- Do NOT assume any flow the map does not show. If there is NO login / sign-in screen in the map,
  this app has no login — do NOT add email / password / sign-in steps. If there is no cart screen,
  there is no cart step. Plan strictly what the map supports for the raw steps below. Different apps
  differ: some start at a login screen, others open directly onto a menu/home — always match THIS map.
- If a raw step genuinely needs a screen or element that is ABSENT from the map, emit a single
  {{"action": "vision_required", "description": "<remaining goal>"}} step and stop — never fabricate ids.

CROSS-KIOSK / MULTI-APP TESTS (important):
- The app map may contain screens from MORE THAN ONE kiosk (each screen belongs to a device in the
  Device Map). An end-to-end test can move between devices — e.g. "load a card at VPS, then buy at
  RPS, then return to VPS". Plan the WHOLE journey: emit steps for EACH device in the order the raw
  steps describe, and set every step's "device" to the alias whose app that screen belongs to.
- When the flow moves to another device, the robot/browser switches to that device automatically
  based on the "device" tag — you do NOT emit a navigation step for the move itself; just tag the
  next screen's steps with the new device. Continue emitting real screen_id/element_id/px/py from
  that device's screens in the map.
- Only fall back to vision_required for a device/flow whose screens are genuinely NOT in the map.

TEST CASE TO PLAN:
ID: {test_id}
Summary: {summary}
Description: {description}

Steps (raw):
{steps_raw}

Expected Results (raw):
{expected_results_raw}

PREREQUISITE ANALYSIS — apply this to every app, not just kiosks:
Raw test steps are written by humans as high-level summaries. They are often abbreviated and skip
implicit prerequisites. Before translating each step, ask: "Would this step actually succeed if a
robot ran it right now, given only the steps that came before?"

Rules:
1. If a step navigates to a screen that REQUIRES prior state (e.g. cart, checkout, payment,
   confirmation), verify that state was established by an earlier step. If not, INSERT the
   missing prerequisite steps using elements from the app map above.
   Examples:
   - "Go to cart" / "Checkout" / "Proceed to payment" → items must be in the cart first.
     If no add-to-cart step exists before this, INSERT one (or more, if the app requires
     selecting a quantity/product first) using the relevant element from the app map.
   - "Submit order" / "Place order" → cart must be non-empty AND checkout must be initiated.
   - "Pay" / "Enter card" → must be on the payment screen, which requires a non-empty cart.
2. Any element that has a disabled or inactive state when a condition is not met (a "Checkout"
   button disabled when cart is empty, a "Submit" button disabled when form is incomplete)
   must be preceded by the steps that satisfy that condition.
3. Form submissions: all required fields must be filled before tapping submit.
4. Do NOT blindly translate raw steps word-for-word. Produce the COMPLETE executable sequence.
   Infer and insert missing steps from the app map whenever the raw steps skip prerequisites.

YOUR TASKS:
1. Apply the prerequisite analysis above, inserting any missing steps before translating.
2. Parse each raw step into one or more machine-executable sub-steps.
3. Assign the correct channel per step (use the definitions above strictly).
4. For every step: set "device" to the alias of the target device from the device map above
   (e.g. "TVM", "MPOS"). For web/db/validation steps not tied to a physical device, omit "device".
5. For "robot" tap steps: look up the screen_id and element from the app map; use the exact px/py.
6. For "robot" type steps: ALWAYS include a non-empty "value" — the exact text to enter, taken
   verbatim from the raw step (an amount like "4000", a name, a code). NEVER emit a type step with a
   missing or empty value (that types nothing and the step silently does nothing). Use credential
   placeholders {{valid_email}}, {{valid_password}} for login fields — but ONLY when the app map
   actually contains a login/sign-in screen with such fields. If the app has no login screen, there
   are no login steps and no credential placeholders.
6b. RUNTIME-GENERATED VALUES (e.g. "enter the SAME card number issued at VPS"): the value does not
   exist until an earlier step produces it, so it cannot be hard-coded. Emit a "capture" step right
   after the value first appears on screen, then reference it in the later type step's value as
   {{{{captured.NAME}}}}:
     {{"action": "capture", "channel": "robot", "device": "<alias>", "screen_id": "<screen>",
       "element_id": "<the element that DISPLAYS the value, from the app map>",
       "capture_as": "card_number", "description": "Capture the issued card number"}}
     ...later...
     {{"action": "type", "channel": "robot", "device": "<other alias>", "screen_id": "<screen>",
       "element_id": "<input>", "px": <int>, "py": <int>, "value": "{{{{captured.card_number}}}}"}}
   Only use a capture step when the displaying element exists in the app map; if it does not, emit
   vision_required for that portion instead of guessing.
6c. CONSUMING A CAPTURED / SPECIFIC VALUE — CRITICAL, applies to EVERY reuse, EACH time it occurs:
   When a step says to USE / PAY WITH / RE-ENTER the SAME value produced earlier (the same card,
   code, id, reference) you MUST actually ENTER that specific value — a "type" step with
   {{{{captured.NAME}}}} into the field that receives it, THEN the completion tap. This is REQUIRED
   even when the screen has a plausibly-related completion button already in the map
   (e.g. "Use Mock Card", "Apply", "Start Card Reader Session", "Confirm"). Those buttons complete
   with a GENERIC or blank value, NOT the specific captured one — tapping such a button WITHOUT first
   entering {{{{captured.NAME}}}} does NOT satisfy "use the SAME card" and will make later checks
   (e.g. a balance that must reflect this exact card) wrong. NEVER substitute a charted completion
   button for entering the specific captured value.
   - If the INPUT that must receive the captured value is NOT charted on that screen (the screen only
     has completion / card-reader buttons, no input element), emit exactly TWO steps IN THIS ORDER —
     TAP the completion button FIRST (it starts the mock-card flow / reveals the field), THEN enter
     the captured value and complete:
       1. {{"action": "tap", "channel": "robot", "device": "<alias>", "screen_id": "<screen>",
            "element_id": "<the charted completion button, e.g. use_mock_card_button>", "px": <int>,
            "py": <int>, "description": "Start the mock-card payment (reveals the card field)"}}
       2. {{"action": "vision_required", "device": "<alias>", "screen_id": "<screen>",
            "description": "Enter {{{{captured.NAME}}}} into the card field that appears and
            complete/confirm the payment with that SAME captured value"}}
     This ONE canonical shape (deterministic method tap FIRST, then live vision enters the value and
     confirms) matches the proven-working TC-E2E-001 order and makes similar tests behave identically.
     Do NOT emit a LONE vision_required that both selects the method AND enters — live vision then has
     to choose the method button itself and can tap the WRONG or a SECOND button (e.g. both "Use Mock
     Card" and "Start Card Reader Session"), silently failing the payment. Do NOT reverse the order
     (entering the card BEFORE tapping the completion button does not start the mock-card flow).
   - MULTIPLE reuses (e.g. buy two items, paying for EACH with the same captured card): repeat BOTH
     steps (tap the completion button, then enter {{{{captured.NAME}}}} and confirm) for every payment.
     The captured value must be entered again each time; one button-tap can never stand in for it.
   - After entering the captured value and completing a payment, add a "verify" of the RESULT (the
     order-confirmation / result / updated screen) before moving on, so a payment that silently did
     not complete is caught immediately instead of desyncing the next step. Set that verify's
     "expected_screen" to the RESULT screen the tester will actually see once the order completes (the
     order-result / confirmation screen), NOT "payment" (the payment was already made). If the exact
     result screen isn't in the app map (a successful mock-card completion often advances to an
     uncharted screen), pick the closest charted screen AND write a precise "description" of the
     outcome — the runtime validates the described OUTCOME and tolerates a stale expected_screen.
7. Identify required_config — data the tester MUST provide before the test:
   - Include email + password ONLY if the app map has a login/sign-in screen; otherwise omit them.
   - Include card_number ONLY if a specific pre-existing card is needed by the steps.
   - EXCLUDE: amount / balance (card balance is managed by the system automatically).
   - EXCLUDE: anything generated at runtime (card numbers created by the reader, transaction IDs, etc.).
8. Set credential_scenario: "valid" or "invalid" based on whether the test uses correct credentials.
9. VALUE CHECKS — when a verify step asserts a SPECIFIC on-screen value (amount, count,
   order/confirmation id, status text), it MUST include "expected_value" (the exact string,
   e.g. "$856.67") AND "value_element_id" — the id of the element on "expected_screen" that
   DISPLAYS that value, chosen from that screen's app-map elements by matching the field the step
   refers to (e.g. an order total → the element whose label/note identifies it as the total).
   Only set "value_element_id" when such an element exists for that screen; otherwise omit it
   (validation falls back to vision). Example:
   {{"action": "verify", "channel": "validation", "expected_screen": "order_result",
     "description": "Order result shows total of $856.67", "expected_value": "$856.67",
     "value_element_id": "order_total"}}

Return ONLY valid JSON — no markdown fences, no extra text.
The skeleton below shows ONLY the FORMAT and field names. Do NOT copy its screen_ids, element_ids,
channels, or steps — build every step from the ACTUAL app map and raw steps above. Placeholders in
angle brackets (<...>) must be replaced with real values taken from the app map; required_config is
[] unless the app genuinely needs pre-provided data (see task 7):
{{
  "test_id": "{test_id}",
  "credential_scenario": "valid",
  "required_config": [],
  "steps": [
    {{
      "action": "verify",
      "channel": "validation",
      "description": "<the first screen this test should see is visible>",
      "expected_screen": "<a screen_id copied verbatim from the app map>"
    }},
    {{
      "action": "tap",
      "channel": "robot",
      "device": "<device alias from the device map, or omit if none applies>",
      "description": "<what this tap does>",
      "screen_id": "<screen_id from the app map>",
      "element_id": "<element_id that exists on that screen in the app map>",
      "px": 0,
      "py": 0
    }},
    {{
      "action": "verify",
      "channel": "validation",
      "description": "<expected outcome of the steps>",
      "expected_screen": "<screen_id from the app map>"
    }}
  ]
}}
"""


class TcPlanRequest(BaseModel):
    test_id: str
    summary: str
    description: str = ""
    steps_raw: str
    expected_results_raw: str = ""
    force: bool = False   # True → regenerate even if cached


@app.post("/api/tc-plan")
def get_tc_plan(req: TcPlanRequest, db: Session = Depends(get_db)):
    """Generate (or return cached) Claude-powered structured plan for a test case."""
    from test_runner import plan_cache as _plan_cache
    from app_map import store as app_map_store

    # Load app map once — needed for both cache key and element inventory
    app_map = None
    if Path(tenant_paths.app_map_path()).exists():
        app_map = app_map_store.load(tenant_paths.app_map_path())
    # Scope to THIS test's kiosk(s) so Claude only ever sees the target app(s)' screens: a VPS test
    # is never planned against RPS's login screen, and a cross-kiosk E2E test sees BOTH apps it
    # touches.  Same choke point the runner uses — so the cache key (version_hash of the scoped map)
    # matches between UI plan generation and execution.
    app_map, _kiosk_ids = _scope_map_for_test(db, app_map, req.test_id, req.steps_raw or "")
    map_version = app_map_store.version_hash(app_map)

    # Return cached plan unless force=True.
    # Uses the same plan_cache module as the test runner so UI and runner share one file.
    if not req.force:
        cached = _plan_cache.load(req.test_id, req.steps_raw or "", map_version, req.expected_results_raw or "")
        if cached and _plan_cache.is_valid(cached, app_map):
            return cached

    # Build device map context from DB (one-time config, embedded in plan at generation time)
    devices = db.query(models.DeviceConfig).order_by(models.DeviceConfig.alias).all()
    if devices:
        device_map = "\n".join(
            f"- {d.alias} ({d.description}): physical position "
            f"(x={d.pos_x:.2f}m, y={d.pos_y:.2f}m, θ={d.pos_theta:.1f}°)"
            for d in devices
        )
    else:
        device_map = (
            "No devices configured yet — infer the device name from the test steps verbatim "
            "and tag each robot step with the device name you identify (e.g. 'TVM', 'MPOS')."
        )

    # Build element inventory for the prompt
    inventory = ""
    if app_map:
        inventory = app_map_store.element_inventory_for_prompt(app_map)
    if not inventory:
        inventory = "App map not yet available — classify channels from context only; omit px/py/element_id."

    # Call Claude
    try:
        from langchain_core.messages import HumanMessage
        from vision_agent.llm import get_llm
        prompt = _TC_PLAN_PROMPT.format(
            device_map=device_map,
            test_id=req.test_id,
            summary=req.summary,
            description=req.description or req.summary,
            steps_raw=req.steps_raw,
            expected_results_raw=req.expected_results_raw,
            element_inventory=inventory,
        )
        llm = get_llm()
        raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
        # Strip markdown fences if Claude wraps the JSON
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"Claude returned invalid JSON: {e}")
    except Exception as e:
        raise HTTPException(500, f"Plan generation failed: {e}")

    # Normalise required_config so a malformed entry can never break the studio (it crashed the
    # Test Intake page reading `label.toLowerCase()` on a Claude entry that omitted `label`).
    # Drop entries without a key; default label from the key and type to "text".
    _norm_cfg = []
    for c in (plan.get("required_config") or []):
        if not isinstance(c, dict):
            continue
        key = (c.get("key") or "").strip()
        if not key:
            continue
        _norm_cfg.append({
            "key":   key,
            "label": (c.get("label") or key.replace("_", " ").title()),
            "type":  (c.get("type") or "text"),
        })
    plan["required_config"] = _norm_cfg

    # Deterministic net (shared with the runner): a completion tap meant to REUSE a value captured
    # earlier (e.g. "pay with the SAME issued card") that never enters the value AND whose screen has
    # no charted input for it → rewrite to vision_required so live vision enters {{captured.NAME}} and
    # completes. Fixes plans that bound a plausible-but-wrong charted button (e.g. "Use Mock Card")
    # instead of entering the specific captured value. Idempotent; no-op on correctly-planned reuse.
    try:
        from test_runner.plan_normalize import normalize_captured_reuse
        plan, _reuse_notes = normalize_captured_reuse(plan, app_map)
        for _n in _reuse_notes:
            print(f"  [TC-PLAN] captured-value reuse fix: {_n}")
    except Exception as _exc:
        print(f"  [TC-PLAN] captured-value normalisation skipped: {_exc}")

    # Stamp and save using the shared plan_cache (same file the test runner reads)
    plan["generated_at"] = datetime.utcnow().isoformat() + "Z"
    _plan_cache.invalidate_all_for(req.test_id)  # remove any stale hash-based files
    _plan_cache.save(plan, req.test_id, req.steps_raw or "", map_version, req.expected_results_raw or "")
    return plan


@app.delete("/api/tc-plan/{test_id}", status_code=204)
def delete_tc_plan(test_id: str):
    """Remove all cached plans for this test_id so the next request regenerates via Claude."""
    from test_runner import plan_cache as _plan_cache
    _plan_cache.invalidate_all_for(test_id)


# ── Exploration mode (runtime override — survives until server restart) ────────

# Starts from .env / settings; overridable at runtime via PATCH /api/explore-config.
_runtime_exploration_mode: str = settings.exploration_mode


class ExploreConfigPatch(BaseModel):
    mode: str  # "claude" | "playwright_aria"


@app.get("/api/explore-config")
def get_explore_config():
    backend = settings.robot_backend
    effective = (
        "claude"
        if backend == "real" and _runtime_exploration_mode == "playwright_aria"
        else _runtime_exploration_mode
    )
    return {
        "mode":           _runtime_exploration_mode,
        "effective_mode": effective,
        "robot_backend":  backend,
        "locked":         backend == "real",
        "lock_reason":    "Playwright ARIA requires browser access — not available with a real robot arm." if backend == "real" else None,
    }


@app.patch("/api/explore-config")
def set_explore_config(body: ExploreConfigPatch):
    global _runtime_exploration_mode
    allowed = {"claude", "playwright_aria"}
    if body.mode not in allowed:
        raise HTTPException(400, f"mode must be one of {sorted(allowed)}")
    if body.mode == "playwright_aria" and settings.robot_backend == "real":
        raise HTTPException(400, "playwright_aria is not available when robot_backend=real")
    _runtime_exploration_mode = body.mode
    return {"mode": _runtime_exploration_mode, "status": "ok"}


# ── Human verdict override (for AMBIGUOUS test results) ────────────────────────

class VerdictOverride(BaseModel):
    test_id:    str
    verdict:    str   # "passed" | "failed"
    reviewer:   str = "human"


@app.patch("/api/runs/{run_id}/verdict")
def submit_verdict(run_id: str, body: VerdictOverride, db: Session = Depends(get_db)):
    run = db.query(models.TestRun).filter_by(run_id=run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    result = (
        db.query(models.TestResult)
        .filter_by(run_id=run_id, test_id=body.test_id)
        .first()
    )
    if not result:
        raise HTTPException(404, f"No result for test_id={body.test_id} in run {run_id}")
    if body.verdict not in ("passed", "failed"):
        raise HTTPException(400, "verdict must be 'passed' or 'failed'")

    old_outcome = result.outcome
    result.outcome = body.verdict

    # Re-compute run totals
    run.passed = sum(1 for r in run.results if r.outcome == "passed")
    run.failed = run.total - run.passed
    db.commit()

    _broadcast(run_id, {
        "event":      "verdict_override",
        "run_id":     run_id,
        "test_id":    body.test_id,
        "old_outcome": old_outcome,
        "new_outcome": body.verdict,
        "reviewer":   body.reviewer,
    })
    return {"status": "ok", "test_id": body.test_id, "outcome": body.verdict}


# ── App Explorer ──────────────────────────────────────────────────────────────

class ExploreRequest(BaseModel):
    kiosk_id: str = "K-01"
    kiosk_url: str = "http://localhost:5173"


# In-memory job tracker: explore_id -> {status, message}
_explore_jobs: dict = {}


@app.post("/api/explore", status_code=202)
def start_explore(req: ExploreRequest, db: Session = Depends(get_db)):
    # Verify the kiosk app is reachable before spawning the explorer.
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen(req.kiosk_url, timeout=6)
    except urllib.error.HTTPError:
        pass  # server responded — it's running
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot reach {req.kiosk_url} — "
                f"make sure the kiosk app is running and accessible from this machine, "
                f"then try again."
            ),
        )

    # Remember the URL against this kiosk_id so the whole lifecycle (test planning, execution,
    # results) can reuse it — no need to re-enter it per run.  Upsert KioskConfig.url; other
    # kiosk fields (name, robot, screen dims) are left intact on an existing row.
    kid = (req.kiosk_id or "").strip()
    if kid:
        kcfg = db.query(models.KioskConfig).filter_by(kiosk_id=kid).first()
        if kcfg:
            kcfg.url        = req.kiosk_url
            kcfg.updated_at = datetime.utcnow()
        else:
            db.add(models.KioskConfig(kiosk_id=kid, name=kid, url=req.kiosk_url))
        db.commit()

    explore_id = f"explore-{uuid.uuid4().hex[:8]}"
    _explore_jobs[explore_id] = {"status": "running", "message": "Exploration in progress…"}
    # Resolve the tenant-scoped app_map path NOW, in the request context (the tenant binding is a
    # contextvar that won't propagate into the background thread), and hand it to the explorer.
    _explore_map_path = tenant_paths.app_map_path()
    t = threading.Thread(
        target=_run_explorer,
        args=(explore_id, req.kiosk_url, req.kiosk_id, _explore_map_path),
        daemon=True,
    )
    t.start()
    return {"explore_id": explore_id, "status": "running"}


@app.get("/api/explore/{explore_id}")
def get_explore_status(explore_id: str):
    job = _explore_jobs.get(explore_id)
    if not job:
        raise HTTPException(404, "Explore job not found")
    return {"explore_id": explore_id, **job}


# ── App Map ───────────────────────────────────────────────────────────────────

@app.post("/api/reset", status_code=200)
def reset_all(db: Session = Depends(get_db)):
    """Clear test-execution artifacts AND the uploaded test cases.

    Deletes: test run details (runs, robot events, defects), screenshots taken during
    test execution, results JSON, generated test plans, and the imported/uploaded test cases.
    Preserves: app exploration output (app map + explore/walkthrough/annotated
    screenshots), kiosk/global configuration, and robot setup.
    (App Explorer has its own separate "clear" action.)
    """
    from sqlalchemy import text
    # Test run details + results (raw SQL for reliable deletes; children before parents).
    # Intentionally NOT touched: app_maps, kiosk_configs, device_configs.
    db.execute(text("DELETE FROM test_results"))
    db.execute(text("DELETE FROM defects"))
    db.execute(text("DELETE FROM test_runs"))
    db.execute(text("DELETE FROM robot_events"))
    db.execute(text("DELETE FROM test_cases"))   # uploaded/imported test cases
    db.commit()

    import shutil
    # Execution screenshots only — leave exploration output untouched.  The app map
    # references screenshots/explore_*.png and walkthrough_*.png, and annotated_*.png are
    # the labelled exploration frames, so those (and the annotated/ folder) are preserved.
    _EXEC_PREFIXES = ("after_", "tap_", "verify_", "tier3_", "click_")
    base = _base_screens_dir()
    if base.exists():
        for child in base.iterdir():
            if child.is_dir():
                if child.name.startswith("run-") or child.name == "results":
                    shutil.rmtree(child, ignore_errors=True)
            elif child.name.startswith(_EXEC_PREFIXES):
                child.unlink(missing_ok=True)

    # Results JSON (suite_*.json) + run counter → numbering restarts at 1. Tenant-scoped so a
    # reset clears only the calling tenant's outputs (== the single global dir when single-tenant).
    results_dir = tenant_paths.results_dir()
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            f.unlink(missing_ok=True)
    (results_dir / ".run_seq").unlink(missing_ok=True)

    # Generated test plans (test_plans/<test_id>_<hash>.json), tenant-scoped.
    plans_dir = tenant_paths.test_plans_dir()
    if plans_dir.exists():
        for f in plans_dir.glob("*.json"):
            f.unlink(missing_ok=True)

    return {"status": "ok",
            "message": ("Cleared test runs, execution screenshots, results, generated plans, and "
                        "uploaded test cases. App exploration and configuration preserved.")}


@app.get("/api/runs/{run_id}/screenshots")
def list_run_screenshots(run_id: str):
    """Filenames of the step screenshots captured during a run (in screenshots/<run_id>/)."""
    d = _run_screens_dir(run_id)
    if not d.exists():
        return []
    return sorted([f.name for f in d.iterdir() if f.suffix.lower() in {".png", ".jpg", ".jpeg"}])


@app.get("/api/runs/{run_id}/screenshots/{filename}")
def get_run_screenshot(run_id: str, filename: str):
    from fastapi.responses import FileResponse
    path = _run_screens_dir(run_id) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


# Raw exploration screenshots live at the top of the screenshots dir with these prefixes
# (execution screenshots live in per-run subfolders and are never touched here).
_EXPLORE_SHOT_PREFIXES = ("explore_", "aria_", "scroll_", "walkthrough_")


def _delete_exploration_shots(screen_ids: Optional[set[str]] = None) -> int:
    """Delete exploration screenshots — annotated + raw diagnostic captures.

    screen_ids=None → delete ALL exploration shots (global clear / clean slate).
    screen_ids given → delete only those screens' annotated shots, plus raw shots whose
                       filename references one of those screen ids (per-app clear).
    Execution screenshots (screenshots/<run_id>/…) are never affected — they live in per-run
    SUBFOLDERS, and every loop here only touches FILES at the top level (or in annotated/).
    """
    base = _base_screens_dir()
    removed = 0
    ann_dir = base / "annotated"

    # ── Global clear = true clean slate ───────────────────────────────────────
    # Remove EVERY top-level capture regardless of prefix/extension and the whole annotated
    # folder.  Prefix-filtering (below) missed artifacts like keyboard_map_*.png, calibration.png
    # and health_capture.png, so re-exploring another kiosk left the previous kiosk's shots on the
    # App Map page.  This nukes all of them; execution shots (subfolders) are skipped by is_file().
    if screen_ids is None:
        if base.exists():
            if ann_dir.exists():
                for f in ann_dir.glob("*"):
                    if f.is_file():
                        try: f.unlink(); removed += 1
                        except OSError: pass
            for f in base.iterdir():
                if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    try: f.unlink(); removed += 1
                    except OSError: pass
            # Per-exploration capture folders (screenshots/exploration_<app>_<ts>/) — the raw shots
            # now live here, so a clean slate must nuke them too.  Execution folders (run-*, results)
            # and annotated/ are intentionally NOT matched by the exploration_ prefix.
            import shutil as _shutil
            for d in base.iterdir():
                if d.is_dir() and d.name.startswith("exploration_"):
                    _shutil.rmtree(d, ignore_errors=True); removed += 1
        return removed

    # ── Per-app clear = only the given screens' shots (other apps preserved) ───
    # Annotated: screenshots/annotated/<screen_id>_<ts>.png
    if ann_dir.exists():
        for f in ann_dir.glob("*.png"):
            sid = f.stem.rsplit("_", 1)[0] if f.stem.rsplit("_", 1)[-1].isdigit() else f.stem
            if sid in screen_ids:
                try: f.unlink(); removed += 1
                except OSError: pass

    # Raw diagnostic captures at the top level
    for f in base.glob("*.png"):
        if not f.name.startswith(_EXPLORE_SHOT_PREFIXES) and f.name != "explore_entry.png":
            continue
        if any(sid in f.name for sid in screen_ids):
            try: f.unlink(); removed += 1
            except OSError: pass
    return removed


def _gc_orphan_shots() -> int:
    """Delete exploration screenshots whose screen is no longer in the map (orphans left by
    earlier map-clears that didn't remove files). Keeps shots for screens still in the map."""
    from app_map import store as app_map_store
    p = Path(tenant_paths.app_map_path())
    if not p.exists():
        return _delete_exploration_shots(None)
    try:
        live = set(((app_map_store.load(str(p)) or {}).get("screens") or {}).keys())
    except Exception:
        return 0
    base = _base_screens_dir()
    removed = 0
    ann = base / "annotated"
    if ann.exists():
        for f in ann.glob("*.png"):
            sid = f.stem.rsplit("_", 1)[0] if f.stem.rsplit("_", 1)[-1].isdigit() else f.stem
            if sid not in live:
                try: f.unlink(); removed += 1
                except OSError: pass
    for f in base.glob("*.png"):
        if not f.name.startswith(_EXPLORE_SHOT_PREFIXES) and f.name != "explore_entry.png":
            continue
        if not any(sid in f.name for sid in live):
            try: f.unlink(); removed += 1
            except OSError: pass
    return removed


@app.delete("/api/app-map", status_code=204)
def delete_app_map():
    """Clear the ENTIRE app map + all exploration screenshots (clean slate)."""
    p = Path(tenant_paths.app_map_path())
    if p.exists():
        p.unlink()
    n = _delete_exploration_shots(None)
    print(f"  [APP MAP] Cleared all apps + {n} exploration screenshots")


@app.delete("/api/app-map/{app_id}", status_code=204)
def delete_app_map_app(app_id: str):
    """Clear ONLY one app's screens + its screenshots (per-app re-explore).

    Other apps' screens and screenshots are preserved. If this was the only app, everything is wiped.
    """
    from app_map import store as app_map_store
    p = Path(tenant_paths.app_map_path())
    if not p.exists():
        _delete_exploration_shots(None)
        return
    try:
        existing = app_map_store.load(str(p))
    except Exception:
        p.unlink()
        _delete_exploration_shots(None)
        return

    # Screens (and thus screenshots) that belong to this app, captured before removal.
    app_screen_ids = {
        sid for sid, sc in (existing.get("screens") or {}).items()
        if (sc.get("app_id") or "") == app_id
    }
    updated = app_map_store.remove_app(existing, app_id)
    if updated is None:
        p.unlink()
        n = _delete_exploration_shots(None)          # last app → clean slate
    else:
        p.write_text(json.dumps(updated, indent=2, default=str), encoding="utf-8")
        n = _delete_exploration_shots(app_screen_ids)  # this app's shots
        n += _gc_orphan_shots()                         # + any leftover orphans
        # This app's per-exploration capture folders (screenshots/exploration_<app_id>_<ts>/).
        # Folder name uses the same sanitisation run_explorer.py applies to the app_id.
        import shutil as _shutil
        _safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in app_id) or "single"
        for d in _base_screens_dir().glob(f"exploration_{_safe}_*"):
            if d.is_dir():
                _shutil.rmtree(d, ignore_errors=True); n += 1
    print(f"  [APP MAP] Cleared app '{app_id}' + {n} screenshots")


@app.get("/api/screenshots/annotated")
def list_annotated_screenshots():
    """List annotated screenshots grouped by screen_id."""
    shots_dir = Path(tenant_paths.app_map_path()).parent / "screenshots" / "annotated"
    if not shots_dir.exists():
        return {}
    result: dict[str, list[str]] = {}
    for f in sorted(shots_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True):
        # filename: {screen_id}_{timestamp}.png — split on last underscore+digits
        name = f.stem  # e.g. "login_1720000000000"
        # screen_id is everything before the last _<digits> suffix
        parts = name.rsplit("_", 1)
        screen_id = parts[0] if len(parts) == 2 and parts[1].isdigit() else name
        result.setdefault(screen_id, []).append(f.name)
    return result


@app.get("/api/screenshots/annotated/{filename}")
def get_annotated_screenshot(filename: str):
    from fastapi.responses import FileResponse
    shots_dir = Path(tenant_paths.app_map_path()).parent / "screenshots" / "annotated"
    path = shots_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


@app.get("/api/screenshots")
def list_screenshots():
    shots_dir = Path(tenant_paths.app_map_path()).parent / "screenshots"
    if not shots_dir.exists():
        return []
    return sorted(
        [f.name for f in shots_dir.iterdir() if f.suffix.lower() in {".png", ".jpg", ".jpeg"}],
        reverse=True,
    )


@app.get("/api/screenshots/{filename}")
def get_screenshot(filename: str):
    from fastapi.responses import FileResponse
    shots_dir = Path(tenant_paths.app_map_path()).parent / "screenshots"
    path = shots_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


@app.get("/api/app-map")
def get_app_map():
    from app_map import store as app_map_store
    if not Path(tenant_paths.app_map_path()).exists():
        return {"screens": {}, "exists": False}
    m = app_map_store.load(tenant_paths.app_map_path())
    # Fall back to file modification time when explored_at is not recorded
    explored_at = m.get("explored_at") or None
    if not explored_at:
        mtime = Path(tenant_paths.app_map_path()).stat().st_mtime
        explored_at = datetime.utcfromtimestamp(mtime).isoformat() + "Z"
    return {
        "exists":       True,
        "explored_at":  explored_at,
        "entry_screen": m.get("entry_screen"),
        "apps":         m.get("apps") or {},   # {app_id: {label, entry_screen, screen_count, …}}
        "screens":      {
            sid: {
                "description": sc.get("description", ""),
                "dom_id":      sc.get("dom_id", ""),
                "app_id":      sc.get("app_id", ""),
                "element_count": len(sc.get("elements") or []),
                "elements":    [
                    {
                        "id":     el.get("id"),
                        "label":  el.get("label"),
                        "type":   el.get("type"),
                        "center": el.get("center"),
                    }
                    for el in (sc.get("elements") or [])
                ],
            }
            for sid, sc in (m.get("screens") or {}).items()
        },
    }


# ── Background workers ─────────────────────────────────────────────────────────

def _broadcast(run_id: str, data: dict):
    """Publish a run event to the event bus. With the in-memory bus this delivers synchronously to
    THIS replica's sockets (identical to the original in-process behaviour); with the Redis bus it
    fans out to EVERY replica, each of which delivers to its own local sockets. Called from worker
    threads (tenant already bound), so _ws_channel picks up the correct tenant."""
    try:
        get_event_bus().publish(_ws_channel(run_id), data)
    except Exception:
        # Never let a realtime hiccup break a run; fall back to direct local delivery.
        _deliver_local(run_id, data)


def _deliver_local(run_id: str, data: dict):
    """Send an event to all WebSocket clients on THIS replica watching this run (thread-safe)."""
    msg = json.dumps(data)
    with _ws_lock:
        sockets = list(_ws_connections.get(run_id, []))
    for ws in sockets:
        try:
            if _main_loop and _main_loop.is_running():
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), _main_loop)
            else:
                asyncio.run(ws.send_text(msg))
        except Exception:
            pass


# Guards the global stdout/stderr tee so two overlapping runs can't clobber each other's streams.
_run_tee_active = False


def _execute_run(run_id: str, req: RunRequest, tenant_id: str = ""):
    """Background thread: execute one test suite and write results to DB.

    tenant_id is captured in the request context and re-bound here so this thread's blob writes
    (results / screenshots / plans) land in the right tenant's namespace. No-op single-tenant."""
    if tenant_id:
        set_current_tenant(tenant_id)
    db = next(get_db())
    run = db.query(models.TestRun).filter_by(run_id=run_id).first()
    if not run:
        return

    # Root trace span for the whole run (no-op unless TRACING_BACKEND=otel). Entered manually and
    # closed in the finally below, so the large body isn't reindented. Child spans (llm.invoke, per
    # label) nest under it, giving an end-to-end agent trace per run.
    from ports.tracing import span as _trace_span
    _run_span = _trace_span("agent.run.execute", run_id=run_id, tenant=current_tenant())
    _run_span.__enter__()

    _prev_screens_dir = settings.screenshots_dir
    _prev_kiosk_url   = settings.kiosk_url
    _prev_stdout, _prev_stderr = sys.stdout, sys.stderr
    _log_fh = None
    _teeing = False
    test_results: list = []
    try:
        # Route this run's step screenshots into a per-run folder: screenshots/<run_id>/
        _run_dir = _run_screens_dir(run_id)
        _run_dir.mkdir(parents=True, exist_ok=True)
        settings.screenshots_dir = str(_run_dir)

        # Preserve this run's FULL console log (every action, tapped coordinate, robot API call,
        # tier routing, verdict) to results/<run_id>/run_console.log by teeing stdout/stderr. Guarded
        # by _run_tee_active so overlapping runs don't corrupt the global streams (rare — the Studio
        # runs sequentially); a run that can't tee still gets results.json + the rendered run.log.
        global _run_tee_active
        try:
            _res_dir = _run_results_dir(run_id)
            _res_dir.mkdir(parents=True, exist_ok=True)
            if not _run_tee_active:
                _log_fh = open(_res_dir / "run_console.log", "w", encoding="utf-8", errors="replace")
                sys.stdout = _Tee(_prev_stdout, _log_fh)
                sys.stderr = _Tee(_prev_stderr, _log_fh)
                _run_tee_active = True
                _teeing = True
        except Exception:
            _log_fh = None

        # SINGLE SOURCE OF TRUTH for the execution backend is the Configuration page's Robot
        # Connection setting (settings.robot_backend).  Record it on the run so the history
        # reflects the backend that actually ran.  (This is the EXECUTION backend only — it is
        # independent of the App Exploration mode, claude vs playwright_aria.)
        run.mode       = settings.robot_backend
        run.status     = "running"
        run.started_at = datetime.utcnow()
        db.commit()
        _broadcast(run_id, {"event": "run_started", "run_id": run_id})

        from vision_agent import robot
        from app_map import store as app_map_store
        from test_runner.agent import create_test_runner
        from test_runner.state import TestRunnerState

        # Load test cases — from DB (preferred) or Excel file as fallback
        if req.excel_path:
            from test_runner.reader.excel_reader import read_test_cases
            all_cases = read_test_cases(req.excel_path)
        else:
            # Read from the DB; add preconditions="" so the shape matches excel_reader output
            all_cases = [
                {**_tc_summary(t), "preconditions": getattr(t, "preconditions", "")}
                for t in db.query(models.TestCase).order_by(models.TestCase.test_id).all()
            ]

        if req.filter_tc:
            # Honor the ORDER the caller listed test ids in — the Execution page lets the operator
            # arrange the run queue (drag / move up-down) and the suite MUST execute in exactly that
            # order, NOT test_id order. We walk filter_ids in order and append the matching case(s),
            # de-duplicating so a prefix that overlaps an exact id can't run a test twice.
            filter_ids = [f.strip() for f in req.filter_tc.split(',') if f.strip()]
            test_cases = []
            seen: set[str] = set()
            for fid in filter_ids:
                # exact id first, else prefix match (e.g. "TC-VPS" → all VPS cases), order preserved
                matches = [tc for tc in all_cases if tc["test_id"] == fid] or \
                          [tc for tc in all_cases if tc["test_id"].startswith(fid)]
                for tc in matches:
                    if tc["test_id"] not in seen:
                        seen.add(tc["test_id"])
                        test_cases.append(tc)
        else:
            test_cases = all_cases

        if not test_cases:
            avail = [t["test_id"] for t in all_cases[:10]]
            src = req.excel_path or "database"
            raise ValueError(
                f"No test cases found matching filter '{req.filter_tc or '*'}' "
                f"in {src}. Available: {avail}"
            )

        # Resolve which kiosk(s) each selected test targets and STAMP them onto every test case.
        # parse_steps reads tc["kiosk_ids"] to scope the app_map to the right app(s) before planning:
        #   • single-kiosk test  → one id  → scope to that app
        #   • cross-kiosk E2E    → several → scope to the UNION so BOTH apps' screens are plannable
        # tc["kiosk_id"] (primary/first) drives the initial browser URL. Correct even in mixed runs
        # and for suites uploaded before the device map existed.
        for tc in test_cases:
            ids = _infer_kiosk_ids(tc.get("test_id", ""), tc.get("steps_raw", ""), db)
            tc["kiosk_ids"] = ids
            tc["kiosk_id"]  = ids[0] if ids else "K-01"

        # Run-level kiosk set (for the run label + which URL to open first): explicit caller value
        # wins, else the union across all selected tests (primary kiosk of each, plus any extras).
        if run.kiosk_id:
            kiosk_ids = [k.strip() for k in run.kiosk_id.split(",") if k.strip()]
        else:
            kiosk_ids = list(dict.fromkeys(
                kid for tc in test_cases for kid in (tc.get("kiosk_ids") or [tc.get("kiosk_id")]) if kid
            ))
            run.kiosk_id = ",".join(kiosk_ids) if kiosk_ids else "K-01"
            db.commit()

        # Point the browser at the target kiosk's URL — remembered from exploration (KioskConfig.url).
        # This is the fix for a test opening the wrong kiosk: the URL now follows the test's kiosk,
        # not a single global default.  Falls back to the previous global when unconfigured.
        primary_kiosk = kiosk_ids[0] if kiosk_ids else ""
        if primary_kiosk:
            kcfg = db.query(models.KioskConfig).filter_by(kiosk_id=primary_kiosk).first()
            if kcfg and kcfg.url:
                settings.kiosk_url = kcfg.url
                _broadcast(run_id, {"event": "log", "run_id": run_id,
                                    "message": f"Target kiosk '{primary_kiosk}' → {kcfg.url}"})
                print(f"  [RUN] kiosk '{primary_kiosk}' URL → {kcfg.url}")
            else:
                print(f"  [RUN] ⚠ no URL configured for kiosk '{primary_kiosk}' — using {settings.kiosk_url}. "
                      f"Explore this kiosk in App Explorer (or set its URL in Configuration).")

        _app_map = None
        if Path(tenant_paths.app_map_path()).exists():
            _app_map = app_map_store.load(tenant_paths.app_map_path())
            if "keyboard_map" in _app_map:
                robot.set_keyboard_map(_app_map["keyboard_map"])
            # Single-kiosk run → scope the map to just that kiosk's screens so the planner never
            # plans/verifies against another kiosk's screens (no-op for legacy single-app maps).
            # Multi-kiosk (E2E) runs keep the full map and switch apps per-device via plan tags.
            if len(kiosk_ids) == 1 and primary_kiosk:
                _scoped = app_map_store.scoped_to_app(_app_map, primary_kiosk)
                _n_all  = len((_app_map.get("screens") or {}))
                _n_sc   = len((_scoped.get("screens") or {}))
                if _n_sc != _n_all:
                    print(f"  [RUN] Scoped app_map to kiosk '{primary_kiosk}': {_n_sc}/{_n_all} screens")
                _app_map = _scoped

        # Real robot: calibrate once up front so the very first tap uses the camera's MEASURED
        # resolution (scale factors) instead of the config fallback.  Non-fatal on failure.
        if settings.robot_backend == "real":
            try:
                from vision_agent.robot import real_robot as _rr
                _rr.calibrate()
                _broadcast(run_id, {"event": "log", "run_id": run_id, "message": "Robot calibrated before run"})
            except Exception as _e:
                print(f"  [RUN] calibration skipped ({_e}) — using configured camera dimensions")

        from test_runner import broadcaster as _broadcaster
        _broadcaster.register(run_id, lambda data: _broadcast(run_id, data))

        runner = create_test_runner()
        initial: TestRunnerState = {
            "run_id":              run_id,
            "test_cases":          test_cases,
            "app_map":             _app_map,
            "credentials":         req.credentials,
            "demo_screens":        {},
            "current_tc_idx":      0,
            "current_tc":          None,
            "structured_plan":     None,
            "planned_steps":       [],
            "credential_scenario": "",
            "start_image":         "",
            "test_results":        [],
            "summary":             "",
        }
        try:
            final = runner.invoke(initial)
        finally:
            _broadcaster.unregister(run_id)
            # Close the Playwright browser at end of run regardless of outcome
            try:
                robot.stop()
            except Exception:
                pass

        test_results = final.get("test_results") or []
        for tr in test_results:
            outcome = tr.get("outcome", "failed")
            result = models.TestResult(
                run_id        = run_id,
                test_id       = tr.get("test_id", ""),
                summary       = tr.get("summary", ""),
                outcome       = outcome,
                step_results  = tr.get("step_results", []),
                vision_summary= tr.get("vision_summary", ""),
                completed_at  = datetime.utcnow(),
            )
            db.add(result)
            steps  = tr.get("step_results") or []
            failed = [s for s in steps if not s.get("success", True)]
            _broadcast(run_id, {
                "event":          "test_result",
                "run_id":         run_id,
                "test_id":        tr.get("test_id"),
                "outcome":        outcome,
                "vision_summary": tr.get("vision_summary", ""),
                "failed_steps":   [s.get("step") for s in failed[:3] if s.get("step")],
            })

        run.total        = len(test_results)
        run.passed       = sum(1 for r in test_results if r.get("outcome") == "passed")
        run.failed       = run.total - run.passed
        # A suite that finished but had ANY failing test (incl. a robot-error hard stop that files a
        # defect) is a FAILED run, not "Done" — the StatusBadge shows 'completed' as green "Done", so a
        # run with failures must land on 'failed' (red "Failed"). Only an all-pass run stays 'completed'.
        # LiveMonitor already treats 'failed' as terminal and shows an error banner only when run.error
        # is set (crash), so a completed-with-failures run reads as Failed WITHOUT a false crash banner.
        run.status       = "failed" if run.failed > 0 else "completed"
        run.completed_at = datetime.utcnow()
        db.commit()
        _broadcast(run_id, {"event": "run_completed", "run_id": run_id,
                             "total": run.total, "passed": run.passed, "failed": run.failed})

        # Trigger defect intelligence sub-agent for any failed TCs
        if run.failed > 0:
            failed_results = [
                tr for tr in test_results
                if tr.get("outcome") != "passed"
            ]
            threading.Thread(
                target=_run_defect_agent,
                args=(run_id, req.kiosk_id, failed_results),
                daemon=True,
            ).start()
            # Auto-run the self-healing Auto-Repair agent (fix → test → build → raise PR) and pop it
            # into a new window in the UI. Fires on the FIRST failed test only, once per run.
            if settings.auto_repair_on_failure:
                threading.Thread(
                    target=_run_auto_repair,
                    args=(run_id, req.kiosk_id, failed_results),
                    daemon=True,
                ).start()

    except Exception as e:
        try:
            robot.stop()
        except Exception:
            pass
        run.status = "failed"
        run.error  = str(e)
        run.completed_at = datetime.utcnow()
        db.commit()
        _broadcast(run_id, {"event": "run_error", "run_id": run_id, "error": str(e)})
    finally:
        try:
            _run_span.__exit__(None, None, None)   # close the run trace span
        except Exception:
            pass
        # Restore the base screenshots dir + global kiosk URL so later exploration/other work
        # isn't misdirected by this run's per-kiosk overrides.
        settings.screenshots_dir = _prev_screens_dir
        settings.kiosk_url       = _prev_kiosk_url

        # Preserve this run's results (results.json + rendered run.log) under results/<run_id>/ so
        # every run is kept and never overwritten by a later one. Runs for BOTH pass and fail paths.
        try:
            _events: list = []
            try:
                from vision_agent import robot as _rb
                if hasattr(_rb, "get_events"):
                    _events = list(_rb.get_events() or [])
            except Exception:
                _events = []
            _write_run_artifacts(run_id, run, test_results, _events)
        except Exception as _ae:
            print(f"  [RUN] artifact write skipped: {_ae}")

        # Restore stdout/stderr and close the per-run console log.
        if _teeing:
            sys.stdout, sys.stderr = _prev_stdout, _prev_stderr
            _run_tee_active = False
        if _log_fh is not None:
            try:
                _log_fh.close()
            except Exception:
                pass

        # Mirror this run's execution output (results.json + run.log + run_console.log + per-run
        # screenshots) to the durable object store (MinIO/S3). MUST run AFTER run_console.log is
        # flushed + closed above — archiving it while still open uploaded an EMPTY console log.
        # No-op unless ARCHIVE_TO_OBJECT_STORE.
        try:
            from ports.archive import archive
            archive(str(_run_results_dir(run_id)))
        except Exception:
            pass


def _run_defect_agent(run_id: str, kiosk_id: str, failed_results: list):
    """Background thread: run the Defect Intelligence sub-agent after a run with failures."""
    from defect_agent.agent import create_defect_agent
    from defect_agent.state import DefectAgentState
    from test_runner import broadcaster as _broadcaster

    print(f"\n  [DEFECT] Starting defect intelligence for run {run_id} ({len(failed_results)} failures)")
    _broadcaster.register(run_id, lambda data: _broadcast(run_id, data))
    try:
        agent = create_defect_agent()
        initial: DefectAgentState = {
            "run_id":         run_id,
            "kiosk_id":       kiosk_id,
            "failed_results": failed_results,
            "evaluations":    [],
            "defects":        [],
        }
        agent.invoke(initial)
    except Exception as e:
        print(f"  [DEFECT] Error: {e}")
    finally:
        _broadcaster.unregister(run_id)


def _failure_text_for(tr: dict) -> str:
    """Build a plain-English failure description for the repair agent from a failed TestResult.

    We lead with the test's DESIGN INTENT (its description + preconditions + expected results, pulled
    from the test case) and steer toward the ROOT CAUSE, so the repair fixes the behaviour that produced
    the wrong state — not the surface symptom. A failing assertion like "expected 'PURCHASE' but the
    screen shows 'LOAD'" otherwise tempts a cosmetic relabel instead of fixing why the purchase was never
    persisted / reflected. The design intent also sharpens RAG retrieval toward the code that PRODUCES
    the state (e.g. the cross-kiosk persistence path) rather than the code that only displays a label."""
    test_id = tr.get("test_id", "")
    summary = tr.get("summary", "") or ""
    vision  = tr.get("vision_summary", "") or ""
    steps   = tr.get("step_results") or []
    failed  = [s for s in steps if not s.get("success", True)]
    detail  = "; ".join(
        str(s.get("observation") or s.get("note") or s.get("step") or "")
        for s in failed[:3]
    )

    # Design intent (what the app SHOULD do) from the test case — so the fix targets behaviour, not text.
    description = preconditions = expected = ""
    if test_id:
        try:
            from api.database import SessionLocal
            db = SessionLocal()
            try:
                tc = db.query(models.TestCase).filter_by(test_id=test_id).first()
                if tc:
                    description   = (tc.description or "").strip()
                    preconditions = (tc.preconditions or "").strip()
                    expected      = (tc.expected_results_raw or "").strip()
            finally:
                db.close()
        except Exception:
            pass

    # The ordered ACTIONS the test performed, redacted. This gives BOTH the RAG retrieval query and
    # Claude the vocabulary of what the test was DOING when it failed (e.g. "Sign In", "add to cart",
    # "check balance") — which anchors retrieval to the code path UNDER TEST rather than to the
    # navigation symptom in the failed assertion. Without it, a login failure reads only as "wrong
    # screen: expected products got login", which semantically matches screen/config code and buries
    # the credential-check code that is actually broken (observed: the SignInScreen auth chunk fell to
    # rank ~11, outside the retrieved set, so the repair had "insufficient context"). Type VALUES are
    # redacted so a typed password never reaches the prompt/embedding; an email (a non-secret
    # identifier) is kept because it adds useful retrieval signal and leaks nothing sensitive.
    import re as _re
    step_labels = []
    for s in steps:
        lbl = str(s.get("step") or "").strip()
        if not lbl:
            continue
        m = _re.match(r"(?i)^\s*type:\s*(.*)$", lbl)
        if m:
            val = m.group(1).strip()
            if not ("@" in val and " " not in val):   # keep an email identifier; redact anything else
                lbl = "type: <redacted>"
        if not s.get("success", True):
            lbl += " [FAILED HERE]"
        step_labels.append(lbl)

    parts = [f"{test_id} failed. {summary}."]
    intent = " ".join(p for p in (description, preconditions, expected) if p).strip()
    if intent:
        parts.append(f"EXPECTED BEHAVIOUR (design intent): {intent}")
    if step_labels:
        parts.append("Steps attempted (in order): " + " ; ".join(step_labels))
    if vision:
        parts.append(f"OBSERVED: {vision}")
    if detail:
        parts.append(f"Failing assertions: {detail}")
    parts.append(
        "Fix the ROOT CAUSE of why the expected behaviour did not happen — not the surface symptom. "
        "If the expected outcome is a value that should have been persisted or shared across "
        "screens/kiosks (a balance, a transaction), correct the code that PRODUCES or PERSISTS that "
        "state, NOT code that merely displays or labels it."
    )
    return " ".join(parts).strip()


def _run_auto_repair(run_id: str, kiosk_id: str, failed_results: list):
    """Background thread: auto-run the Auto-Repair agent for the FIRST failed test, streaming its
    stages into an in-memory repair job and signalling the UI (repair_started/repair_done on the
    run WS) so it can pop the repair into a new window."""
    if not failed_results:
        return
    tr = failed_results[0]
    test_id = tr.get("test_id", "")
    failure = _failure_text_for(tr)

    repair_id = f"repair-{uuid.uuid4().hex[:8]}"
    _repair_set(
        repair_id,
        repair_id=repair_id, status="pending", stages={}, auto=True,
        failure=failure, test_id=test_id, run_id=run_id,
        created_at=datetime.utcnow().isoformat(),
    )
    print(f"\n  [REPAIR] Auto-repair for run {run_id} / {test_id} → job {repair_id}")
    _broadcast(run_id, {"event": "repair_started", "run_id": run_id,
                        "repair_id": repair_id, "test_id": test_id})
    cancel_event = threading.Event()
    with _repair_lock:
        _repair_cancel[repair_id] = cancel_event
    try:
        from repair_agent.repair_failed_test import run_repair
        result = run_repair(
            failure, test_id=test_id, apply=True, auto_pr=settings.repair_auto_pr,
            branch_suffix=repair_id.split("-")[-1],
            progress_cb=lambda u: _repair_stage(repair_id, u),
            cancel_event=cancel_event,
        )
        with _repair_lock:
            job = _repair_jobs.setdefault(repair_id, {})
            job["result"] = result
            job["status"] = ("cancelled" if result.get("cancelled")
                             else "succeeded" if result.get("success") else "completed")
            if result.get("error"):
                job["error"] = result["error"]
            job["updated_at"] = datetime.utcnow().isoformat()
        pr = (result.get("stages") or {}).get("pr", {}) or {}
        pr_url = (pr.get("opened") or {}).get("url", "")
        _broadcast(run_id, {"event": "repair_done", "run_id": run_id, "repair_id": repair_id,
                            "test_id": test_id, "success": bool(result.get("success")),
                            "cancelled": bool(result.get("cancelled")), "pr_url": pr_url})
    except Exception as e:
        print(f"  [REPAIR] Auto-repair error: {e}")
        _repair_set(repair_id, status="failed", error=str(e))
        _broadcast(run_id, {"event": "repair_done", "run_id": run_id, "repair_id": repair_id,
                            "test_id": test_id, "success": False, "error": str(e)})
    finally:
        with _repair_lock:
            _repair_cancel.pop(repair_id, None)


def _run_explorer(explore_id: str, kiosk_url: str, kiosk_id: str = "", app_map_path: str = ""):
    """Background thread: run the app explorer and record success/failure.

    app_map_path is the tenant-scoped destination resolved in the request context; it is passed to
    the subprocess as APP_MAP_PATH so exploration writes the correct tenant's map (single-tenant →
    the MVP path). Blank → the subprocess uses its own configured default."""
    import os
    import subprocess
    try:
        # Clean orphaned screenshots from previously-cleared apps so counts stay honest.
        try:
            gc = _gc_orphan_shots()
            if gc:
                print(f"  [APP MAP] GC removed {gc} orphaned exploration screenshots before explore")
        except Exception:
            pass
        env = os.environ.copy()
        # Pass the runtime override so the subprocess picks it up via pydantic-settings
        env["EXPLORATION_MODE"] = _runtime_exploration_mode
        if kiosk_url:
            env["KIOSK_URL"] = kiosk_url          # explore the requested app's URL
        if kiosk_id:
            env["EXPLORE_APP_ID"] = kiosk_id      # tag + merge this app's screens (multi-app)
        if app_map_path:
            env["APP_MAP_PATH"] = app_map_path    # tenant-scoped destination (== MVP path when single-tenant)
        result = subprocess.run(
            [sys.executable, "run_explorer.py"],
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            _explore_jobs[explore_id] = {
                "status": "done",
                "message": "Exploration complete. App map written successfully.",
            }
        else:
            err = (result.stderr or "")[-800:].strip()
            _explore_jobs[explore_id] = {
                "status": "error",
                "message": f"Explorer process exited with code {result.returncode}. {err}",
            }
    except Exception as e:
        _explore_jobs[explore_id] = {
            "status": "error",
            "message": f"Explorer crashed: {e}",
        }


# ── Serialisers ───────────────────────────────────────────────────────────────

def _run_summary(r: models.TestRun) -> dict:
    return {
        "run_id":       r.run_id,
        "kiosk_id":     r.kiosk_id,
        "robot_id":     r.robot_id,
        "mode":         r.mode,
        "status":       r.status,
        "total":        r.total,
        "passed":       r.passed,
        "failed":       r.failed,
        "filter_tc":    r.filter_tc,
        "error":        r.error,
        "started_at":   r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "created_at":   r.created_at.isoformat() if r.created_at else None,
    }


def _tc_summary(t: models.TestCase) -> dict:
    return {
        "test_id":             t.test_id,
        "kiosk_id":            t.kiosk_id,
        "summary":             t.summary,
        "description":         t.description,
        "steps_raw":           t.steps_raw,
        "expected_results_raw":t.expected_results_raw,
        "priority":            t.priority,
        "tags":                t.tags,
    }


def _kiosk_summary(k: models.KioskConfig) -> dict:
    return {
        "kiosk_id":   k.kiosk_id,
        "name":       k.name,
        "url":        k.url,
        "robot_id":   k.robot_id,
        "screen_w_m": k.screen_w_m,
        "screen_h_m": k.screen_h_m,
        "tag_id":     k.tag_id,
        "position":   {"x": k.position_x, "y": k.position_y, "theta": k.position_th},
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host=settings.api_host, port=settings.api_port, reload=True)
