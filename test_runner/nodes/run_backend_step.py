"""
run_backend_step — stub for backend/API validation steps.

Steps classified as "backend" (e.g. verifying DB state, checking API responses)
are routed here.  Currently a pass-through stub; replace with real API calls
when backend validation is integrated.
"""
from test_runner.state import TestRunnerState, TestResult


def run_backend_step(state: TestRunnerState) -> dict:
    tc = state["current_tc"]
    print(f"\n  [BACKEND] {tc['test_id']} — backend validation (stub, always passes)")

    # TODO: implement real backend validation
    # - Connect to backend API / database
    # - Run each backend-type step from parsed_steps
    # - Return actual pass/fail results

    test_result: TestResult = {
        "test_id":        tc["test_id"],
        "summary":        tc["summary"],
        "outcome":        "passed",
        "step_results":   [],
        "vision_summary": "Backend validation: stub (not yet implemented)",
    }

    return {
        "test_results": [*(state.get("test_results") or []), test_result],
    }
