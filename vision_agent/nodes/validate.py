"""
validate_step node — sends the after-screenshot to Claude and asks whether the
last robot action achieved its expected outcome. Updates the step result and
records the transition in the decision tree.
"""
import base64
import json
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState
from vision_agent.prompts import VALIDATE_STEP
from vision_agent.llm import get_fast_llm
from vision_agent.storage import get_storage


def validate_step(state: VisionAgentState) -> dict:
    last: dict = state["step_results"][-1]

    # Robot execution already failed (element not found, etc.) — no LLM call needed
    if last.get("error"):
        return {}

    image_bytes = get_storage().load(state["image_path"])
    b64 = base64.standard_b64encode(image_bytes).decode()

    screen_before = state["screen_analysis"]["screen_id"]
    prompt = VALIDATE_STEP.format(
        action_description=last["step_instruction"],
        action_type=last["action_type"],
        screen_before=screen_before,
    )

    llm = get_fast_llm()   # Haiku — binary yes/no, ~0.8 s vs Sonnet's ~2.5 s
    msg = HumanMessage(content=[
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": prompt},
    ])
    response = llm.invoke([msg])
    raw = response.content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    v = json.loads(raw)

    success: bool = v.get("success", False)
    new_screen: str = v.get("new_screen_id", "unknown")
    observation: str = v.get("observation", "")
    hint: str | None = v.get("recovery_hint")

    print(f"    [VALIDATE] {'✓' if success else '✗'} {observation}")

    # Update the last step result in place
    updated_last = {**last, "success": success, "observation": observation,
                    "error": None if success else (hint or "Validation failed")}
    updated_results = [*state["step_results"][:-1], updated_last]

    # Record this transition in the decision tree
    dt = {k: dict(v) for k, v in (state.get("decision_tree") or {}).items()}
    screen_id = state["screen_analysis"]["screen_id"]
    if screen_id not in dt:
        dt[screen_id] = {}
    dt[screen_id][last["step_instruction"]] = new_screen

    return {"step_results": updated_results, "decision_tree": dt}
