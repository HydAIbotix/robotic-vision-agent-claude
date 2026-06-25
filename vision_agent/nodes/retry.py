"""
handle_retry node — increments the retry counter and logs recovery context.
Control returns to analyze_screen to re-read the current state before retrying.
"""
from vision_agent.state import VisionAgentState


def handle_retry(state: VisionAgentState) -> dict:
    retry_count = (state.get("retry_count") or 0) + 1
    last = (state.get("step_results") or [{}])[-1]
    step = state["planned_steps"][state["current_step_idx"]]

    print(f"\n  [RETRY {retry_count}] '{step}'")
    if last.get("error"):
        print(f"    Reason: {last['error']}")
    if last.get("observation"):
        print(f"    Last observation: {last['observation']}")

    return {"retry_count": retry_count}
