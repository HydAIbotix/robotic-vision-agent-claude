"""
validate_step node — sends the after-screenshot to Claude and asks whether the
last robot action achieved its expected outcome. Updates the step result and
records the transition in the decision tree.
"""
import base64
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState
from vision_agent.prompts import VALIDATE_STEP
from vision_agent.llm import get_llm, invoke_json, detect_image_media_type
from vision_agent.storage import get_storage


def validate_step(state: VisionAgentState) -> dict:
    last: dict = state["step_results"][-1]

    # Robot execution already failed (element not found, etc.) — no LLM call needed
    if last.get("error"):
        return {}

    image_bytes = get_storage().load(state["image_path"])
    b64 = base64.standard_b64encode(image_bytes).decode()
    media_type = detect_image_media_type(image_bytes)

    screen_before = state["screen_analysis"]["screen_id"]
    prompt = VALIDATE_STEP.format(
        action_description=last["step_instruction"],
        action_type=last["action_type"],
        screen_before=screen_before,
    )

    llm = get_llm()   # Opus — Tier-3 validation must reason correctly about screen state
                      # (e.g. "checkout didn't navigate because the cart is empty"), not just
                      # answer a shallow yes/no.  Tier-3 is a fallback path, so latency is fine.
    msg = HumanMessage(content=[
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": prompt},
    ])
    # Resilient parse — an unparseable response is treated as a failed (retryable) step
    # rather than crashing the run.
    v = invoke_json(llm, [msg], default={
        "success": False,
        "new_screen_id": "unknown",
        "observation": "validation response could not be parsed",
        "recovery_hint": None,
    }, label="validate")

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
