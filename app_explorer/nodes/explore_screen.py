"""
explore_screen — analyze the current screenshot, add the screen to the AppMap,
and queue every explorable action discovered by Claude.
"""
import json
from pathlib import Path
from langchain_core.messages import HumanMessage
from vision_agent.nodes.analyze import analyze_screen as _analyze
from vision_agent.llm import get_llm
from vision_agent.screen_cache import compute_hash
from vision_agent.storage import get_storage
from app_explorer.state import ExplorerState, ExplorationAction
from app_explorer.prompts import SUGGEST_EXPLORABLE_ACTIONS

# Screens whose content changes per session — always re-analyze at test time.
_DYNAMIC_SCREEN_KEYWORDS = {"cart", "order", "history", "search", "result", "basket", "checkout_items"}


def explore_screen(state: ExplorerState) -> dict:
    # ── 1. Analyze screenshot with existing vision node ───────────────────────
    vision_result = _analyze({
        "image_path": state["current_image_path"],
        "screen_history": [],
        "task_description": "",
        "screen_analysis": None,
        "planned_steps": [],
        "current_step_idx": 0,
        "step_results": [],
        "retry_count": 0,
        "decision_tree": {},
        "outcome": "running",
        "summary": "",
        "error_message": None,
    })
    screen = vision_result["screen_analysis"]
    screen_id = screen["screen_id"]

    # ── 2. Add screen to AppMap if not already present ────────────────────────
    app_map = {**state["app_map"], "screens": dict(state["app_map"].get("screens") or {})}
    if screen_id not in app_map["screens"]:
        # Compute perceptual hash for fast cache lookup at test time
        image_bytes = get_storage().load(state["current_image_path"])
        screen_hash = compute_hash(image_bytes)

        # Tag dynamic screens — their content changes per session, so coordinates
        # from this map cannot be reused without re-analyzing at test time.
        is_dynamic = any(kw in screen_id.lower() for kw in _DYNAMIC_SCREEN_KEYWORDS)

        app_map["screens"][screen_id] = {
            "screen_id":   screen_id,
            "description": screen["description"],
            "elements":    screen["elements"],
            "transitions": {},
            "screen_hash": screen_hash,
            "is_dynamic":  is_dynamic,
        }
        tag = "DYNAMIC" if is_dynamic else "STATIC"
        print(f"\n  [EXPLORE] New screen added: '{screen_id}' [{tag}] hash={screen_hash[:12]}…")
    else:
        print(f"\n  [EXPLORE] Re-visiting known screen: '{screen_id}'")

    # ── 3. Ask Claude to suggest explorable actions ────────────────────────────
    creds = state.get("credentials") or {}
    valid = creds.get("valid", {})
    invalid = creds.get("invalid", {})

    elements_text = "\n".join(
        f"  {el['id']} [{el['type']}] \"{el['label']}\" — {el['description']}"
        for el in screen["elements"]
    )

    prompt = SUGGEST_EXPLORABLE_ACTIONS.format(
        screen_id=screen_id,
        screen_description=screen["description"],
        elements_text=elements_text,
        valid_email=valid.get("email", "tester@kiosk.local"),
        valid_password=valid.get("password", "Password123"),
        invalid_email=invalid.get("email", "baduser@example.com"),
        invalid_password=invalid.get("password", "WrongPass!"),
    )

    llm = get_llm()
    raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)

    # ── 4. Queue actions not yet explored ─────────────────────────────────────
    explored = set(state.get("explored_action_keys") or [])
    new_actions: list[ExplorationAction] = []
    for a in data.get("explorable_actions") or []:
        full_key = f"{screen_id}::{a['action_key']}"
        if full_key not in explored:
            new_actions.append({
                "action_key": a["action_key"],
                "screen_id": screen_id,
                "description": a.get("description", ""),
                "steps": a.get("steps") or [],
                "credential_scenario": a.get("credential_scenario"),
            })

    queue = list(state.get("exploration_queue") or []) + new_actions
    print(f"  [EXPLORE] '{screen_id}': queued {len(new_actions)} new actions (queue depth: {len(queue)})")

    return {
        "app_map": app_map,
        "current_screen_id": screen_id,
        "exploration_queue": queue,
        "last_result_is_new": False,
    }
