"""
finalize node — computes the overall pass/fail outcome and persists the result JSON.
"""
import time
from vision_agent.state import VisionAgentState
from vision_agent.storage import get_storage


def finalize(state: VisionAgentState) -> dict:
    results = state.get("step_results") or []

    # Retries produce duplicate step_instruction entries — only the last result
    # for each step counts. A step that failed then recovered via retry is a pass.
    final_by_step: dict = {}
    for r in results:
        final_by_step[r["step_instruction"]] = r
    final_results = list(final_by_step.values())

    passed_count = sum(1 for r in final_results if r["success"])
    all_passed = passed_count == len(final_results) and len(final_results) > 0

    outcome = "passed" if all_passed else "failed"
    screens = " → ".join(state.get("screen_history") or [])
    retry_count = len(results) - len(final_results)
    retry_note = f" ({retry_count} step(s) recovered via retry)" if retry_count else ""
    summary = (
        f"{outcome.upper()}: {passed_count}/{len(final_results)} steps succeeded{retry_note}. "
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
