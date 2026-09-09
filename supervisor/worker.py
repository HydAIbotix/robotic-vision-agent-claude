"""
Per-robot worker — runs a test suite against a single robot/kiosk pair.

Each worker:
  1. Loads test cases from Excel (filtered by filter_tc if set)
  2. Loads app_map from the configured path
  3. Invokes the test_runner LangGraph
  4. Returns a WorkerResult

Designed to run in a thread (ThreadPoolExecutor) — uses its own robot_id
context so multiple robots can run concurrently without state collision.
"""
import time
import uuid
from pathlib import Path

from supervisor.state import WorkerAssignment, WorkerResult


def run_worker(assignment: WorkerAssignment, on_event=None) -> WorkerResult:
    """
    Execute the test suite for one robot/kiosk assignment.

    on_event(event_dict) — optional callback for live progress streaming.
    Called from the worker thread; the caller must handle thread safety.
    """
    # Bind this worker thread's tenant from the TENANT_ID env (set by the CLI). Contextvars don't
    # cross the thread boundary, so each worker re-binds. No-op unless MULTI_TENANT_ENABLED.
    import os
    from ports.tenancy import set_current_tenant
    set_current_tenant(os.environ.get("TENANT_ID"))

    run_id = f"run-{assignment.robot_id}-{uuid.uuid4().hex[:8]}"
    result = WorkerResult(
        robot_id   = assignment.robot_id,
        kiosk_id   = assignment.kiosk_id,
        run_id     = run_id,
        status     = "running",
        started_at = time.time(),
    )

    def _emit(event_type: str, **kwargs):
        if on_event:
            on_event({
                "run_id":    run_id,
                "robot_id":  assignment.robot_id,
                "kiosk_id":  assignment.kiosk_id,
                "event":     event_type,
                "ts":        time.time(),
                **kwargs,
            })

    try:
        from test_runner.reader.excel_reader import read_test_cases
        from vision_agent.config import settings
        from vision_agent import robot
        from app_map import store as app_map_store
        from test_runner.agent import create_test_runner
        from test_runner.state import TestRunnerState
        from ports import paths as tenant_paths   # tenant-scoped app_map (== MVP when single-tenant)

        # Load test cases
        all_cases = read_test_cases(assignment.excel_path)
        test_cases = (
            [tc for tc in all_cases if tc["test_id"].startswith(assignment.filter_tc or "")]
            if assignment.filter_tc else all_cases
        )
        _emit("test_cases_loaded", count=len(test_cases))

        if not test_cases:
            result.status = "completed"
            result.finished_at = time.time()
            result.error = f"No test cases matched filter={assignment.filter_tc!r}"
            return result

        # Load app_map (tenant-scoped path; identical to the MVP path when single-tenant)
        _app_map = None
        _map_path = tenant_paths.app_map_path()
        if Path(_map_path).exists():
            _app_map = app_map_store.load(_map_path)
            if "keyboard_map" in _app_map:
                robot.set_keyboard_map(_app_map["keyboard_map"])
        _emit("app_map_loaded", screens=len((_app_map or {}).get("screens", {})))

        # Run the test suite
        _emit("suite_started", total=len(test_cases))
        runner = create_test_runner()
        initial: TestRunnerState = {
            "test_cases":          test_cases,
            "app_map":             _app_map,
            "credentials":         assignment.credentials,
            "demo_screens":        assignment.demo_screens,
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
        result.total   = len(test_results)
        result.passed  = sum(1 for r in test_results if r.get("outcome") == "passed")
        result.failed  = result.total - result.passed
        result.results = test_results
        result.status  = "completed"
        _emit("suite_completed",
              total=result.total, passed=result.passed, failed=result.failed)

    except Exception as exc:
        result.status = "failed"
        result.error  = str(exc)
        _emit("suite_error", error=str(exc))

    result.finished_at = time.time()
    return result
