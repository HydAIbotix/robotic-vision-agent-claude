"""
plan_steps node — converts the task_description into an ordered list of atomic steps
(tap / type / verify) using the screen elements identified by analyze_screen.

Skipped on re-entry (after retry) — existing plan is reused.
"""
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState
from vision_agent.prompts import PLAN_STEPS
from vision_agent.llm import get_llm, invoke_json


def plan_steps(state: VisionAgentState) -> dict:
    if state.get("planned_steps"):
        return {}  # already planned; re-entry after retry keeps existing plan

    screen = state["screen_analysis"]
    elements_text = "\n".join(
        f"  {el['id']} [{el['type']}] \"{el['label']}\" — {el['description']}"
        for el in screen["elements"]
    )

    prompt = PLAN_STEPS.format(
        task_description=state["task_description"],
        screen_id=screen["screen_id"],
        screen_description=screen["description"],
        elements=elements_text,
    )

    # Resilient parse — an empty/malformed model response returns [] rather than crashing.
    steps = invoke_json(get_llm(), [HumanMessage(content=prompt)], default=[], label="plan")
    if not isinstance(steps, list):
        steps = []

    if not steps:
        # Could not derive a plan (screen had no usable elements, or the model returned
        # nothing after retries).  Emit one verify step so the graph finalizes with a clear
        # failure instead of indexing into an empty list.
        print("\n  [PLAN] No steps produced — emitting a single diagnostic verify step")
        steps = [f"verify: could not determine any action on screen '{screen.get('screen_id', 'unknown')}'"]

    print(f"\n  [PLAN] {len(steps)} steps:")
    for i, s in enumerate(steps, 1):
        print(f"    {i}. {s}")

    return {"planned_steps": steps, "current_step_idx": 0}
