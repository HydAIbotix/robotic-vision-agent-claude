"""
explore_screen_aria — ARIA-tree-based screen exploration for playwright mode.

Replaces explore_screen when exploration_mode="playwright_aria".
Uses page.accessibility.snapshot() instead of Claude vision to discover
interactive elements — zero LLM calls for element discovery.

Claude IS still called once per screen for SUGGEST_EXPLORABLE_ACTIONS
(action ordering, conditional navigation reasoning).  But the screenshot
step and all coordinate-estimation calls are eliminated.

Differences from explore_screen (Claude vision mode):
  - Element list comes from ARIA tree (roles, names, states) — no screenshot sent to Claude.
  - Pixel coordinates come from page.locator(...).bounding_box() — exact, not estimated.
  - Screen ID comes from get_dom_screen_id() — unchanged.
  - Falls back to Claude vision explore_screen if ARIA tree is empty or backend != playwright.

Output format: identical to explore_screen.
"""
import json
import re
import time
from pathlib import Path
from langchain_core.messages import HumanMessage
from vision_agent.llm import get_llm
from vision_agent.config import settings
from vision_agent import robot
from app_explorer.state import ExplorerState, ExplorationAction
from app_explorer.prompts import SUGGEST_EXPLORABLE_ACTIONS
from app_explorer.nodes.explore_screen import _DYNAMIC_SCREEN_KEYWORDS, _VIEWPORT_W, _VIEWPORT_H, _save_annotated

# Interactive ARIA roles we consider for exploration
_INTERACTIVE_ROLES = {
    "button", "link", "menuitem", "option", "radio", "checkbox",
    "switch", "tab", "textbox", "combobox", "spinbutton", "slider",
    "searchbox", "menuitemcheckbox", "menuitemradio", "treeitem",
}


def _walk_aria(node: dict, results: list, depth: int = 0) -> None:
    """Recursively collect interactive leaf nodes from the ARIA tree."""
    if not node or depth > 12:
        return
    role = (node.get("role") or "").lower()
    name = (node.get("name") or "").strip()
    if role in _INTERACTIVE_ROLES and name and not node.get("disabled", False):
        results.append({
            "role":     role,
            "name":     name,
            "value":    node.get("value") or "",
            "checked":  node.get("checked"),
            "expanded": node.get("expanded"),
        })
    for child in node.get("children") or []:
        _walk_aria(child, results, depth + 1)


def _name_to_id(name: str, role: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9 ]", "", name).lower().strip()
    base = re.sub(r"\s+", "_", base)[:40]
    suffix = {
        "textbox": "_input", "searchbox": "_input", "combobox": "_select",
        "checkbox": "_checkbox", "radio": "_radio", "tab": "_tab",
    }.get(role, "_button")
    if role == "link":
        suffix = ""
    return (base + suffix).strip("_") or f"{role}_element"


def _aria_to_elements(aria_nodes: list) -> list[dict]:
    """Convert ARIA nodes → element dicts matching explore_screen output format."""
    elements: list[dict] = []
    seen_names: set[str] = set()

    for node in aria_nodes:
        name = node["name"]
        role = node["role"]
        if name in seen_names:
            continue
        seen_names.add(name)

        eid = _name_to_id(name, role)

        # Resolve pixel center via DOM
        box = None
        for selector in (
            f'[aria-label="{name}"]',
            f'button:has-text("{name}")',
            f'a:has-text("{name}")',
            f'[role="{role}"]:has-text("{name}")',
            f'input[placeholder="{name}"]',
        ):
            box = robot.get_element_bounding_box(selector)
            if box:
                break

        if box:
            cx, cy, conf = box["cx"], box["cy"], 0.97
        else:
            cx, cy, conf = _VIEWPORT_W // 2, _VIEWPORT_H // 2, 0.3

        etype = {
            "button": "button", "link": "link", "textbox": "input",
            "searchbox": "input", "combobox": "dropdown", "spinbutton": "stepper",
            "checkbox": "button", "radio": "button", "switch": "button",
            "tab": "button", "menuitem": "button", "option": "button",
        }.get(role, "button")

        desc_parts = [f"{role.title()}: {name}"]
        if node.get("checked") is True:
            desc_parts.append("currently checked")
        elif node.get("checked") is False:
            desc_parts.append("currently unchecked")
        if node.get("value"):
            desc_parts.append(f"value: {node['value']}")

        elements.append({
            "id":          eid,
            "type":        etype,
            "label":       name,
            "description": "; ".join(desc_parts),
            "center":      [cx, cy],
            "bbox":        [max(0, cx-50), max(0, cy-20), min(_VIEWPORT_W, cx+50), min(_VIEWPORT_H, cy+20)],
            "confidence":  conf,
        })

    return elements[:25]


def _derive_description(aria_tree: dict, screen_id: str) -> str:
    headings: list[str] = []

    def _find(node: dict, d: int = 0) -> None:
        if d > 5:
            return
        if (node.get("role") or "").lower() == "heading" and node.get("name"):
            headings.append(node["name"].strip())
        for c in node.get("children") or []:
            _find(c, d + 1)

    _find(aria_tree)
    return headings[0][:100] if headings else screen_id.replace("_", " ").title()


def explore_screen_aria(state: ExplorerState) -> dict:
    """Analyze current screen via ARIA tree; fall back to Claude vision if unavailable."""
    from app_explorer.nodes.explore_screen import explore_screen as _claude_explore

    if settings.robot_backend != "playwright":
        print("  [ARIA] Non-playwright backend — using Claude vision explorer")
        return _claude_explore(state)

    # ── Screen identity ────────────────────────────────────────────────────────
    screen_id = robot.get_dom_screen_id()
    if not screen_id:
        print("  [ARIA] DOM screen_id unavailable — using Claude vision explorer")
        return _claude_explore(state)

    app_map     = state.get("app_map") or {}
    known       = app_map.get("screens") or {}
    existing    = known.get(screen_id, {})
    explored    = set(state.get("explored_action_keys") or [])
    queue       = list(state.get("exploration_queue") or [])
    approach    = dict(state.get("approach_paths") or {})

    # Re-visiting: refresh reference_screenshot but skip re-analysis
    if existing.get("elements"):
        print(f"\n  [ARIA] Re-visiting '{screen_id}' — refreshing screenshot only")
        ts = int(time.time() * 1000)
        snap = str(Path(settings.screenshots_dir) / f"aria_{screen_id}_{ts}.png")
        Path(snap).parent.mkdir(parents=True, exist_ok=True)
        robot.capture_screen(snap)
        new_screen = {**existing, "reference_screenshot": snap}
        new_map = {**app_map, "screens": {**known, screen_id: new_screen}}
        return {
            "app_map":            new_map,
            "current_screen_id":  screen_id,
            "exploration_queue":  queue,
            "last_result_is_new": False,
            "approach_paths":     approach,
        }

    print(f"\n  [ARIA] Exploring '{screen_id}' via ARIA tree (0 image-LLM calls)")

    # ── Screenshot (for annotated map + real-robot reference) ─────────────────
    ts = int(time.time() * 1000)
    snap = str(Path(settings.screenshots_dir) / f"aria_{screen_id}_{ts}.png")
    Path(snap).parent.mkdir(parents=True, exist_ok=True)
    capture    = robot.capture_screen(snap)
    image_path = capture["image_path"]

    # ── ARIA tree → element list ───────────────────────────────────────────────
    aria_tree  = robot.get_aria_snapshot()
    aria_nodes: list[dict] = []
    if aria_tree:
        _walk_aria(aria_tree, aria_nodes)

    if not aria_nodes:
        print("  [ARIA] Empty ARIA tree — falling back to Claude vision explorer")
        return _claude_explore({**state, "current_image_path": image_path})

    elements    = _aria_to_elements(aria_nodes)
    description = _derive_description(aria_tree, screen_id)
    is_dynamic  = any(kw in screen_id.lower() for kw in _DYNAMIC_SCREEN_KEYWORDS)
    dom_id      = screen_id  # already from get_dom_screen_id()

    print(f"  [ARIA] Found {len(elements)} elements  [{('DYNAMIC' if is_dynamic else 'STATIC')}]")

    # ── Build app_map entry ────────────────────────────────────────────────────
    new_screen_data = {
        "screen_id":            screen_id,
        "description":          description,
        "elements":             elements,
        "transitions":          existing.get("transitions") or {},
        "is_dynamic":           is_dynamic,
        "dom_id":               dom_id,
        "reference_screenshot": image_path,
        "explored_via":         "playwright_aria",
    }
    new_map = {**app_map, "screens": {**known, screen_id: new_screen_data}}
    _save_annotated(image_path, screen_id, elements)

    # ── Suggest explorable actions (1 text LLM call) ──────────────────────────
    creds   = state.get("credentials") or {}
    valid   = creds.get("valid", {})
    invalid = creds.get("invalid", {})

    elements_text = "\n".join(
        f"  {el['id']} [{el['type']}] \"{el.get('label','')}\" — {el.get('description','')}"
        for el in elements
    )
    prompt = SUGGEST_EXPLORABLE_ACTIONS.format(
        screen_id=screen_id,
        screen_description=description,
        elements_text=elements_text,
        valid_email=valid.get("email",    "tester@example.com"),
        valid_password=valid.get("password","Password123"),
        invalid_email=invalid.get("email",  "baduser@example.com"),
        invalid_password=invalid.get("password","WrongPass!"),
    )
    llm = get_llm()
    raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)

    # ── Dedup (identical logic to explore_screen) ─────────────────────────────
    fully_explored_ids = {
        sid for sid, sc in (new_map.get("screens") or {}).items()
        if sc.get("elements")
    }
    element_transitions = new_map.get("element_transitions") or {}

    new_actions: list[ExplorationAction] = []
    skipped = 0
    for a in data.get("explorable_actions") or []:
        full_key = f"{screen_id}::{a['action_key']}"
        if full_key in explored:
            continue
        steps = a.get("steps") or []
        if len(steps) == 1 and steps[0].get("action_type") == "tap":
            eid = steps[0].get("element_id", "")
            if eid and eid in element_transitions:
                known_dest = element_transitions[eid]
                if known_dest in fully_explored_ids and known_dest != screen_id:
                    skipped += 1
                    continue
            action_key_lower = a["action_key"].lower()
            if any(sid in action_key_lower for sid in fully_explored_ids if len(sid) >= 5):
                skipped += 1
                continue
        new_actions.append({
            "action_key":          a["action_key"],
            "screen_id":           screen_id,
            "description":         a.get("description", ""),
            "steps":               steps,
            "credential_scenario": a.get("credential_scenario"),
        })

    if skipped:
        print(f"  [DEDUP]  Skipped {skipped} action(s) — destinations already mapped")

    merged_queue = new_actions + queue  # DFS: explore current branch first
    print(f"  [ARIA] '{screen_id}': queued {len(new_actions)} actions  (queue depth: {len(merged_queue)})")

    if screen_id not in approach:
        approach[screen_id] = []

    return {
        "app_map":            new_map,
        "current_screen_id":  screen_id,
        "current_image_path": image_path,
        "exploration_queue":  merged_queue,
        "last_result_is_new": screen_id not in known,
        "approach_paths":     approach,
    }
