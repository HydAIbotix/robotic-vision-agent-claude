"""
Supervisor graph — parallel multi-robot orchestrator.

Uses ThreadPoolExecutor to run N workers concurrently, one per robot/kiosk pair.
Collects live events via a thread-safe queue and yields them to callers.

Two usage patterns:
  1. Blocking   : results = run_parallel(assignments)
  2. Streaming  : for event in stream_parallel(assignments): handle(event)
"""
import time
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from supervisor.state import SupervisorResult, WorkerAssignment, WorkerResult
from supervisor.worker import run_worker


def run_parallel(
    assignments: list[dict | WorkerAssignment],
    max_workers: int = 4,
) -> SupervisorResult:
    """
    Run all assignments in parallel and block until all complete.

    assignments — list of dicts or WorkerAssignment objects:
        robot_id    : str  (e.g. "R-01")
        kiosk_id    : str  (e.g. "K-01")
        excel_path  : str  path to the test cases Excel file
        filter_tc   : str | None  optional test-case ID prefix filter
        credentials : dict  {"valid": {...}, "invalid": {...}}
        demo_screens: dict  only needed in demo mode

    Returns a SupervisorResult aggregating all workers' results.
    """
    t0 = time.time()

    # Normalise dicts → WorkerAssignment
    normed: list[WorkerAssignment] = []
    for a in assignments:
        if isinstance(a, dict):
            normed.append(WorkerAssignment(**a))
        else:
            normed.append(a)

    event_queue: queue.Queue = queue.Queue()

    def _on_event(ev: dict) -> None:
        event_queue.put(ev)
        _print_event(ev)

    worker_results: list[WorkerResult] = []

    print(f"\n{'='*60}")
    print(f"  SUPERVISOR — {len(normed)} robot(s) in parallel")
    for a in normed:
        print(f"    {a.robot_id}  →  {a.kiosk_id}  [{a.excel_path}]"
              + (f"  filter={a.filter_tc}" if a.filter_tc else ""))
    print(f"{'='*60}\n")

    futures: dict[Future, WorkerAssignment] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(normed))) as pool:
        for assignment in normed:
            f = pool.submit(run_worker, assignment, _on_event)
            futures[f] = assignment

        for f in as_completed(futures):
            wr = f.result()          # propagates exceptions from the worker
            worker_results.append(wr)

    wall = round(time.time() - t0, 1)
    total   = sum(r.total  for r in worker_results)
    passed  = sum(r.passed for r in worker_results)
    failed  = sum(r.failed for r in worker_results)

    print(f"\n{'='*60}")
    print(f"  SUPERVISOR COMPLETE  {wall}s")
    print(f"  Total: {total}  Passed: {passed}  Failed: {failed}")
    for r in worker_results:
        icon = "✓" if r.status == "completed" and r.failed == 0 else "✗"
        print(f"  {icon}  {r.robot_id}/{r.kiosk_id}  "
              f"{r.passed}/{r.total} passed  "
              + (f"ERROR: {r.error}" if r.error else ""))
    print(f"{'='*60}\n")

    return SupervisorResult(
        workers      = worker_results,
        total_tests  = total,
        total_passed = passed,
        total_failed = failed,
        wall_time_s  = wall,
    )


def stream_parallel(
    assignments: list[dict | WorkerAssignment],
    max_workers: int = 4,
):
    """
    Generator version — yields live events while workers run.

    Usage:
        for event in stream_parallel(assignments):
            websocket.send(json.dumps(event))
    """
    event_queue: queue.Queue = queue.Queue()
    done_flag = threading.Event()

    def _on_event(ev: dict) -> None:
        event_queue.put(ev)

    def _run_all():
        run_parallel(assignments, max_workers=max_workers)
        done_flag.set()
        event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_run_all, daemon=True)
    thread.start()

    while True:
        try:
            ev = event_queue.get(timeout=1.0)
        except queue.Empty:
            if done_flag.is_set():
                break
            continue
        if ev is None:
            break
        yield ev


# ── Internal helpers ───────────────────────────────────────────────────────────

def _print_event(ev: dict) -> None:
    event = ev.get("event", "")
    rid   = ev.get("robot_id", "")
    if event == "test_cases_loaded":
        print(f"  [{rid}] Loaded {ev['count']} test cases")
    elif event == "suite_started":
        print(f"  [{rid}] Suite started — {ev['total']} tests")
    elif event == "suite_completed":
        print(f"  [{rid}] Suite done — {ev['passed']}/{ev['total']} passed")
    elif event == "suite_error":
        print(f"  [{rid}] ERROR: {ev['error']}")
