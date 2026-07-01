"""
identify_result — analyze the result screenshot, determine which screen it is,
record the transition in the AppMap, and flag whether a new screen was discovered.
"""
import base64
import json
from langchain_core.messages import HumanMessage
from vision_agent import robot
from vision_agent.storage import get_storage
from vision_agent.llm import get_explorer_llm
from vision_agent.screen_cache import compute_hash, lookup_screen
from app_explorer.state import ExplorerState
from app_explorer.prompts import IDENTIFY_RESULT_SCREEN


def _record_transition(app_map: dict, action: dict, result_screen_id: str) -> dict:
    """Write action_key → result_screen_id into the source screen's transitions."""
    new_map = {**app_map, "screens": {k: dict(v) for k, v in (app_map.get("screens") or {}).items()}}
    from_sid = action["screen_id"]
    if from_sid in new_map["screens"]:
        screen = dict(new_map["screens"][from_sid])
        transitions = dict(screen.get("transitions") or {})
        transitions[action["action_key"]] = result_screen_id
        screen["transitions"] = transitions
        new_map["screens"][from_sid] = screen
    return new_map


def _record_element_transition(app_map: dict, action: dict, result_screen_id: str) -> dict:
    """Track element_id → destination globally for cross-screen deduplication.

    When a single-tap action on element 'sign_out_button' leads to 'signin', this is
    stored once.  explore_screen then skips 'sign_out' actions on every other screen
    because the destination ('signin') is already fully explored.

    Only single-tap actions are tracked — multi-step flows (login form, etc.) are
    screen-specific and should not be globally deduplicated.
    """
    steps = action.get("steps") or []
    if len(steps) != 1 or steps[0].get("action_type") != "tap":
        return app_map
    element_id = steps[0].get("element_id", "")
    if not element_id or not result_screen_id:
        return app_map
    element_transitions = dict(app_map.get("element_transitions") or {})
    if element_id not in element_transitions:
        element_transitions[element_id] = result_screen_id
        return {**app_map, "element_transitions": element_transitions}
    return app_map


def identify_result(state: ExplorerState) -> dict:
    action = state.get("last_executed_action")
    if not action:
        return {"last_result_is_new": False, "last_result_screen_id": state["current_screen_id"]}

    image_bytes = get_storage().load(state["current_image_path"])
    app_map     = state["app_map"]

    # ── Primary: DOM-based screen detection (SPA state navigation) ───────────
    # SPA apps share the same URL across views — perceptual hashes collide between
    # screens that have a similar layout.  get_dom_screen_id() reads a stable
    # DOM marker (e.g. data-testid) to reliably identify the current view.
    dom_id        = robot.get_dom_screen_id()    # e.g. "categories", "products", ""
    known_screens = app_map.get("screens") or {}

    if dom_id:
        # Find the already-explored screen whose dom_id matches
        dom_match = next(
            (sid for sid, sc in known_screens.items() if sc.get("dom_id") == dom_id),
            None,
        )
        if dom_match:
            # Known screen — fast path, zero LLM calls
            result_screen_id = dom_match
            print(f"\n  [RESULT]  {action['screen_id']}::{action['action_key']}  ->  '{result_screen_id}'  (DOM-hit: {dom_id}, 0 LLM calls)")
            new_map        = _record_transition(app_map, action, result_screen_id)
            # Only record element_transitions for genuine cross-screen navigations.
            # Staying on the source screen means the element has an unmet precondition
            # (e.g. a button that needs prior state set up before it navigates).
            # Do not record this as permanent — a multi-step action may navigate successfully.
            if result_screen_id != action["screen_id"]:
                new_map = _record_element_transition(new_map, action, result_screen_id)
            approach_paths = dict(state.get("approach_paths") or {})
            return {
                "app_map":               new_map,
                "current_screen_id":     result_screen_id,
                "last_result_is_new":    False,
                "last_result_screen_id": result_screen_id,
                "approach_paths":        approach_paths,
            }
        # dom_id not yet mapped → new screen.
        # Skip perceptual-hash lookup (it will collide with layout-sharing siblings)
        # and fall straight through to Claude vision with dom_id as the suggested id.

    else:
        # ── Secondary: perceptual-hash lookup (no DOM signal available) ───────
        # Only used when backend is real robot arm or demo stubs (no browser DOM).
        # lookup_screen only matches screens with a screen_hash (fully explored).
        cached = lookup_screen(image_bytes, app_map)
        if cached:
            result_screen_id = cached["screen_id"]
            print(f"\n  [RESULT]  {action['screen_id']}::{action['action_key']}  ->  '{result_screen_id}'  (hash-hit, 0 LLM calls)")
            new_map        = _record_transition(app_map, action, result_screen_id)
            if result_screen_id != action["screen_id"]:
                new_map = _record_element_transition(new_map, action, result_screen_id)
            approach_paths = dict(state.get("approach_paths") or {})
            return {
                "app_map":               new_map,
                "current_screen_id":     result_screen_id,
                "last_result_is_new":    False,
                "last_result_screen_id": result_screen_id,
                "approach_paths":        approach_paths,
            }

    # ── Slow path: Claude vision identification ───────────────────────────────
    b64 = base64.standard_b64encode(image_bytes).decode()
    known_desc = "\n".join(
        f"  {sid}: {sc['description']}"
        for sid, sc in known_screens.items()
    ) or "  (none yet)"

    # Provide DOM hint so Claude assigns the right screen_id for new SPA screens
    dom_hint = f"\nDOM screen hint: '{dom_id}' — use this as the screen_id for any new screen." if dom_id else ""

    prompt = IDENTIFY_RESULT_SCREEN.format(
        action_description=action["description"],
        from_screen_id=action["screen_id"],
        known_screens=known_desc,
        dom_screen_hint=dom_hint,
    )

    llm = get_explorer_llm()
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": prompt},
    ])
    raw = llm.invoke([msg]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    v = json.loads(raw)

    result_screen_id = v["screen_id"]
    is_new           = v.get("is_new_screen", False)
    description      = v.get("description", "")
    transition_type  = v.get("transition_type", "navigation_success")

    print(f"\n  [RESULT]  {action['screen_id']}::{action['action_key']}  ->  '{result_screen_id}'  ({'NEW' if is_new else 'known'})  [{transition_type}]")

    new_map = _record_transition(app_map, action, result_screen_id)
    if result_screen_id != action["screen_id"]:
        new_map = _record_element_transition(new_map, action, result_screen_id)

    # If new screen: add a skeleton entry so explore_screen can flesh it out
    if is_new and result_screen_id not in new_map["screens"]:
        new_map["screens"][result_screen_id] = {
            "screen_id":   result_screen_id,
            "description": description,
            "elements":    [],
            "transitions": {},
        }

    # ── Track approach path so execute_action can reset to any screen ─────────
    approach_paths = dict(state.get("approach_paths") or {})
    if is_new and result_screen_id not in approach_paths:
        parent_path = approach_paths.get(action["screen_id"], [])
        approach_paths[result_screen_id] = parent_path + [action]

    return {
        "app_map":              new_map,
        "current_screen_id":    result_screen_id,
        "last_result_is_new":   is_new,
        "last_result_screen_id": result_screen_id,
        "approach_paths":       approach_paths,
    }
