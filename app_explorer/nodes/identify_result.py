"""
identify_result — analyze the result screenshot, determine which screen it is,
record the transition in the AppMap, and flag whether a new screen was discovered.
"""
import base64
import json
from langchain_core.messages import HumanMessage
from vision_agent.storage import get_storage
from vision_agent.llm import get_llm
from app_explorer.state import ExplorerState
from app_explorer.prompts import IDENTIFY_RESULT_SCREEN


def identify_result(state: ExplorerState) -> dict:
    action = state.get("last_executed_action")
    if not action:
        return {"last_result_is_new": False, "last_result_screen_id": state["current_screen_id"]}

    # ── Send result screenshot to Claude for identification ───────────────────
    image_bytes = get_storage().load(state["current_image_path"])
    b64 = base64.standard_b64encode(image_bytes).decode()

    app_map = state["app_map"]
    known_screens = "\n".join(
        f"  {sid}: {sc['description']}"
        for sid, sc in (app_map.get("screens") or {}).items()
    ) or "  (none yet)"

    prompt = IDENTIFY_RESULT_SCREEN.format(
        action_description=action["description"],
        from_screen_id=action["screen_id"],
        known_screens=known_screens,
    )

    llm = get_llm()
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": prompt},
    ])
    raw = llm.invoke([msg]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    v = json.loads(raw)

    result_screen_id = v["screen_id"]
    is_new            = v.get("is_new_screen", False)
    description       = v.get("description", "")
    transition_type   = v.get("transition_type", "navigation_success")

    print(f"\n  [RESULT]  {action['screen_id']}::{action['action_key']}  ->  '{result_screen_id}'  ({'NEW' if is_new else 'known'})  [{transition_type}]")

    # ── Record the transition in the AppMap ───────────────────────────────────
    new_map = {**app_map, "screens": {k: dict(v) for k, v in (app_map.get("screens") or {}).items()}}

    from_sid = action["screen_id"]
    if from_sid in new_map["screens"]:
        screen = dict(new_map["screens"][from_sid])
        transitions = dict(screen.get("transitions") or {})
        transitions[action["action_key"]] = result_screen_id
        screen["transitions"] = transitions
        new_map["screens"][from_sid] = screen

    # If new screen: add a skeleton entry so explore_screen can flesh it out
    if is_new and result_screen_id not in new_map["screens"]:
        new_map["screens"][result_screen_id] = {
            "screen_id":   result_screen_id,
            "description": description,
            "elements":    [],
            "transitions": {},
        }

    return {
        "app_map":              new_map,
        "current_screen_id":    result_screen_id,
        "last_result_is_new":   is_new,
        "last_result_screen_id": result_screen_id,
    }
