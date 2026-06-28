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


@app.on_event("startup")
def startup():
    init_db()
    print(f"  [API] Management API ready at http://{settings.api_host}:{settings.api_port}")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ── Test Runs ─────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    robot_id:   str = "R-01"
    kiosk_id:   str = "K-01"
    excel_path: str
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


@app.post("/api/runs", status_code=202)
def start_run(req: RunRequest, db: Session = Depends(get_db)):
    run_id = f"run-{uuid.uuid4().hex[:10]}"
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


# ── Test Cases ────────────────────────────────────────────────────────────────

@app.get("/api/test-cases")
def list_test_cases(kiosk_id: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.TestCase)
    if kiosk_id:
        q = q.filter_by(kiosk_id=kiosk_id)
    return [_tc_summary(t) for t in q.order_by(models.TestCase.test_id).all()]


@app.post("/api/test-cases/upload")
def upload_test_cases(
    file: UploadFile = File(...),
    kiosk_id: str = "K-02",
    db: Session = Depends(get_db),
):
    import tempfile, openpyxl
    from test_runner.reader.excel_reader import read_test_cases

    # Save upload to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(file.file.read())
        tmp_path = tmp.name

    try:
        cases = read_test_cases(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    saved = 0
    for tc in cases:
        existing = db.query(models.TestCase).filter_by(test_id=tc["test_id"]).first()
        if existing:
            for k, v in tc.items():
                setattr(existing, k, v)
            existing.kiosk_id = kiosk_id
        else:
            db.add(models.TestCase(kiosk_id=kiosk_id, **tc))
            saved += 1
    db.commit()
    return {"imported": len(cases), "new": saved}


# ── Configuration ─────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config(db: Session = Depends(get_db)):
    kiosks = db.query(models.KioskConfig).all()
    return {
        "robot_backend":  settings.robot_backend,
        "robot_ip":       settings.robot_ip,
        "robot_id":       settings.robot_id,
        "viewport":       {"width": settings.viewport_width, "height": settings.viewport_height},
        "camera":         {"width": settings.robot_camera_width, "height": settings.robot_camera_height},
        "kiosks":         [_kiosk_summary(k) for k in kiosks],
    }


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


# ── App Explorer ──────────────────────────────────────────────────────────────

class ExploreRequest(BaseModel):
    kiosk_id: str = "K-01"
    kiosk_url: str = "http://localhost:5173"


# In-memory job tracker: explore_id -> {status, message}
_explore_jobs: dict = {}


@app.post("/api/explore", status_code=202)
def start_explore(req: ExploreRequest):
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

    explore_id = f"explore-{uuid.uuid4().hex[:8]}"
    _explore_jobs[explore_id] = {"status": "running", "message": "Exploration in progress…"}
    t = threading.Thread(
        target=_run_explorer,
        args=(explore_id, req.kiosk_url),
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
    """Delete all runs, results, test cases, app maps, robot events — keep kiosk config."""
    from sqlalchemy import text
    # Use raw SQL for reliable deletes (ORM bulk-delete skips cascade logic)
    db.execute(text("DELETE FROM test_results"))
    db.execute(text("DELETE FROM test_runs"))
    db.execute(text("DELETE FROM test_cases"))
    db.execute(text("DELETE FROM app_maps"))
    db.execute(text("DELETE FROM robot_events"))
    db.commit()
    # Remove the app_map.json file
    p = Path(settings.app_map_path)
    if p.exists():
        p.unlink()
    return {"status": "ok", "message": "All data cleared. Kiosk configuration preserved."}


@app.delete("/api/app-map", status_code=204)
def delete_app_map():
    p = Path(settings.app_map_path)
    if p.exists():
        p.unlink()


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
        "screens":      {
            sid: {
                "description": sc.get("description", ""),
                "dom_id":      sc.get("dom_id", ""),
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
    """Send event to all WebSocket clients watching this run."""
    msg = json.dumps(data)
    with _ws_lock:
        for ws in list(_ws_connections.get(run_id, [])):
            try:
                asyncio.run(ws.send_text(msg))
            except Exception:
                pass


def _execute_run(run_id: str, req: RunRequest):
    """Background thread: execute one test suite and write results to DB."""
    db = next(get_db())
    run = db.query(models.TestRun).filter_by(run_id=run_id).first()
    if not run:
        return

    try:
        run.status     = "running"
        run.started_at = datetime.utcnow()
        db.commit()
        _broadcast(run_id, {"event": "run_started", "run_id": run_id})

        from test_runner.reader.excel_reader import read_test_cases
        from vision_agent import robot
        from app_map import store as app_map_store
        from test_runner.agent import create_test_runner
        from test_runner.state import TestRunnerState

        all_cases = read_test_cases(req.excel_path)
        test_cases = (
            [tc for tc in all_cases if tc["test_id"].startswith(req.filter_tc)]
            if req.filter_tc else all_cases
        )

        _app_map = None
        if Path(settings.app_map_path).exists():
            _app_map = app_map_store.load(settings.app_map_path)
            if "keyboard_map" in _app_map:
                robot.set_keyboard_map(_app_map["keyboard_map"])

        runner = create_test_runner()
        initial: TestRunnerState = {
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
        final = runner.invoke(initial)

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
            _broadcast(run_id, {
                "event":    "test_result",
                "run_id":   run_id,
                "test_id":  tr.get("test_id"),
                "outcome":  outcome,
            })

        run.total        = len(test_results)
        run.passed       = sum(1 for r in test_results if r.get("outcome") == "passed")
        run.failed       = run.total - run.passed
        run.status       = "completed"
        run.completed_at = datetime.utcnow()
        db.commit()
        _broadcast(run_id, {"event": "run_completed", "run_id": run_id,
                             "total": run.total, "passed": run.passed, "failed": run.failed})

    except Exception as e:
        run.status = "failed"
        run.error  = str(e)
        run.completed_at = datetime.utcnow()
        db.commit()
        _broadcast(run_id, {"event": "run_error", "run_id": run_id, "error": str(e)})


def _run_explorer(explore_id: str, kiosk_url: str):
    """Background thread: run the app explorer and record success/failure."""
    import subprocess, sys
    try:
        result = subprocess.run(
            [sys.executable, "run_explorer.py"],
            stderr=subprocess.PIPE,
            text=True,
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
