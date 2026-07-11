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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from vision_agent.config import settings
from api.database import get_db, init_db
from api import models

app = FastAPI(title="Robotic Kiosk Test Management API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Live WebSocket connections keyed by run_id
_ws_connections: dict[str, list[WebSocket]] = {}
_ws_lock = threading.Lock()

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
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


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
    return Path(settings.app_map_path).parent / "screenshots"


def _run_screens_dir(run_id: str) -> Path:
    return _base_screens_dir() / run_id


def _next_run_number() -> int:
    """Monotonic run counter persisted in results/.run_seq.  Reset deletes it → restarts at 1."""
    p = Path(settings.results_dir) / ".run_seq"
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

    # Start runner in background thread
    t = threading.Thread(
        target=_execute_run,
        args=(run_id, req),
        daemon=True,
    )
    _active_runs[run_id] = {"thread": t, "req": req}
    t.start()

    return {"run_id": run_id, "status": "pending"}


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
    with _ws_lock:
        _ws_connections.setdefault(run_id, []).append(ws)
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
    return {"alias": d.alias, "kiosk_id": d.kiosk_id or "", "description": d.description or "",
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
        "exploration_mode": _runtime_exploration_mode,
        "card_service_url": settings.card_service_url,
        "viewport":         {"width": settings.viewport_width, "height": settings.viewport_height},
        "camera":           {"width": settings.robot_camera_width, "height": settings.robot_camera_height},
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
    agv_url:       Optional[str] = None   # mobile base (AGV) controller URL
    arm_url:       Optional[str] = None   # arm/camera/card controller URL


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
        "persisted":        persisted,
        "restart_required": backend_changed,
    }


class DeviceConfigRequest(BaseModel):
    alias:       str
    kiosk_id:    str = ""
    description: str = ""
    pos_x:       float = 0.0
    pos_y:       float = 0.0
    pos_theta:   float = 0.0


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
     has completion / card-reader buttons, no input element), emit a SINGLE
     {{"action": "vision_required", "device": "<alias>", "screen_id": "<screen>",
       "description": "Enter {{{{captured.NAME}}}} into the payment/card field and complete the
       payment using that SAME captured value"}} — live vision enters it at run time. Do NOT tap a
     completion button in its place.
   - MULTIPLE reuses (e.g. buy two items, paying for EACH with the same captured card): the captured
     value must be entered AGAIN for every payment — emit a separate {{{{captured.NAME}}}} entry (or a
     separate vision_required) per payment. One button-tap can never stand in for a value that must be
     typed each time.
   - After entering the captured value and completing a payment, add a "verify" of the RESULT (the
     order-confirmation / result / updated screen) before moving on, so a payment that silently did
     not complete is caught immediately instead of desyncing the next step.
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
    if Path(settings.app_map_path).exists():
        app_map = app_map_store.load(settings.app_map_path)
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
    t = threading.Thread(
        target=_run_explorer,
        args=(explore_id, req.kiosk_url, req.kiosk_id),
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

    # Results JSON (suite_*.json) + run counter → numbering restarts at 1.
    results_dir = Path(settings.results_dir)
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            f.unlink(missing_ok=True)
    (results_dir / ".run_seq").unlink(missing_ok=True)

    # Generated test plans (test_plans/<test_id>_<hash>.json).
    plans_dir = Path(__file__).resolve().parent.parent / "test_plans"
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
    p = Path(settings.app_map_path)
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
    p = Path(settings.app_map_path)
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
    p = Path(settings.app_map_path)
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
    print(f"  [APP MAP] Cleared app '{app_id}' + {n} screenshots")


@app.get("/api/screenshots/annotated")
def list_annotated_screenshots():
    """List annotated screenshots grouped by screen_id."""
    shots_dir = Path(settings.app_map_path).parent / "screenshots" / "annotated"
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
    shots_dir = Path(settings.app_map_path).parent / "screenshots" / "annotated"
    path = shots_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


@app.get("/api/screenshots")
def list_screenshots():
    shots_dir = Path(settings.app_map_path).parent / "screenshots"
    if not shots_dir.exists():
        return []
    return sorted(
        [f.name for f in shots_dir.iterdir() if f.suffix.lower() in {".png", ".jpg", ".jpeg"}],
        reverse=True,
    )


@app.get("/api/screenshots/{filename}")
def get_screenshot(filename: str):
    from fastapi.responses import FileResponse
    shots_dir = Path(settings.app_map_path).parent / "screenshots"
    path = shots_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


@app.get("/api/app-map")
def get_app_map():
    from app_map import store as app_map_store
    if not Path(settings.app_map_path).exists():
        return {"screens": {}, "exists": False}
    m = app_map_store.load(settings.app_map_path)
    # Fall back to file modification time when explored_at is not recorded
    explored_at = m.get("explored_at") or None
    if not explored_at:
        mtime = Path(settings.app_map_path).stat().st_mtime
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
    """Send event to all WebSocket clients watching this run (thread-safe)."""
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


def _execute_run(run_id: str, req: RunRequest):
    """Background thread: execute one test suite and write results to DB."""
    db = next(get_db())
    run = db.query(models.TestRun).filter_by(run_id=run_id).first()
    if not run:
        return

    _prev_screens_dir = settings.screenshots_dir
    _prev_kiosk_url   = settings.kiosk_url
    try:
        # Route this run's step screenshots into a per-run folder: screenshots/<run_id>/
        _run_dir = _run_screens_dir(run_id)
        _run_dir.mkdir(parents=True, exist_ok=True)
        settings.screenshots_dir = str(_run_dir)

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
            filter_ids = [f.strip() for f in req.filter_tc.split(',') if f.strip()]
            test_cases = [
                tc for tc in all_cases
                if any(tc["test_id"] == fid or tc["test_id"].startswith(fid) for fid in filter_ids)
            ]
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
        if Path(settings.app_map_path).exists():
            _app_map = app_map_store.load(settings.app_map_path)
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
        run.status       = "completed"
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
        # Restore the base screenshots dir + global kiosk URL so later exploration/other work
        # isn't misdirected by this run's per-kiosk overrides.
        settings.screenshots_dir = _prev_screens_dir
        settings.kiosk_url       = _prev_kiosk_url


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


def _run_explorer(explore_id: str, kiosk_url: str, kiosk_id: str = ""):
    """Background thread: run the app explorer and record success/failure."""
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
