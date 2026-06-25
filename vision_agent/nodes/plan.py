"""
plan_steps node — converts the task_description into an ordered list of atomic steps
(tap / type / verify) using the screen elements identified by analyze_screen.

Skipped on re-entry (after retry) — existing plan is reused.
"""
import json
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState
from vision_agent.prompts import PLAN_STEPS
from vision_agent.llm import get_llm


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

    llm = get_llm()
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()

    steps: list[str] = json.loads(raw)
    print(f"\n  [PLAN] {len(steps)} steps:")
    for i, s in enumerate(steps, 1):
        print(f"    {i}. {s}")

    return {"planned_steps": steps, "current_step_idx": 0}
