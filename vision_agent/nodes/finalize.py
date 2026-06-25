"""
finalize node — computes the overall pass/fail outcome and persists the result JSON.
"""
import time
from vision_agent.state import VisionAgentState
from vision_agent.storage import get_storage


def finalize(state: VisionAgentState) -> dict:
    results = state.get("step_results") or []
    passed_count = sum(1 for r in results if r["success"])
    all_passed = passed_count == len(results) and len(results) > 0

    outcome = "passed" if all_passed else "failed"
    screens = " → ".join(state.get("screen_history") or [])
    summary = (
        f"{outcome.upper()}: {passed_count}/{len(results)} steps succeeded. "
        f"Path: {screens}."
    )

    result_doc = {
        "task": state["task_description"],
        "outcome": outcome,
        "summary": summary,
        "step_results": results,
        "screen_history": state.get("screen_history") or [],
        "decision_tree": state.get("decision_tree") or {},
        "timestamp": time.time(),
    }
    key = f"results/{int(time.time())}_result.json"
    get_storage().save_json(result_doc, key)

    return {"outcome": outcome, "summary": summary}
