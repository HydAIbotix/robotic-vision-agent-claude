"""
explore_screen — analyze the current screenshot, add the screen to the AppMap,
queue every explorable action, and (once per app) map the virtual keyboard.

API call budget per screen:
  • _analyze_fresh             : 1 call  — uses original ANALYZE_SCREEN prompt
                                           (cached system msg, accurate coords)
  • Pass 2 coord-correction    : 0–N calls only for low-confidence elements
  • SUGGEST_EXPLORABLE_ACTIONS : 1 text-only call (no image re-sent)
  • _collect_scrolled_elements : 1 call for ALL scroll screenshots batched together
  • _map_keyboard              : 1 call, once per app
"""
import io
import json
import time
import base64
from pathlib import Path
from PIL import Image, ImageDraw
from langchain_core.messages import HumanMessage
from vision_agent.nodes.analyze import analyze_screen as _analyze
from vision_agent.llm import get_llm
from vision_agent.screen_cache import compute_hash
from vision_agent.storage import get_storage
from vision_agent.config import settings
from vision_agent import robot
from app_explorer.state import ExplorerState, ExplorationAction
from app_explorer.prompts import SUGGEST_EXPLORABLE_ACTIONS, BATCH_SCROLL_ELEMENTS, MAP_KEYBOARD

_DYNAMIC_SCREEN_KEYWORDS = {"cart", "order", "history", "search", "result", "basket", "checkout_items"}
_VIEWPORT_W = 1400
_VIEWPORT_H = 900


# ── Screen analysis ───────────────────────────────────────────────────────────

def _analyze_fresh(image_path: str) -> dict:
    """Run analyze_screen with the app_map cache disabled.

    The cache is bypassed during exploration so visually-similar screens (e.g.
    sign-up vs login) are identified independently by Claude rather than matched
    to a cached hash.  The original ANALYZE_SCREEN system prompt is unchanged,
    so its server-side cache still warms up and coordinate accuracy is preserved.
    """
    orig = settings.use_app_map_cache
    settings.use_app_map_cache = False
    try:
        return _analyze({
            "image_path":      image_path,
            "screen_history":  [],
            "task_description": "",
            "screen_analysis": None,
            "planned_steps":   [],
            "current_step_idx": 0,
            "step_results":    [],
            "retry_count":     0,
            "decision_tree":   {},
            "outcome":         "running",
            "summary":         "",
            "error_message":   None,
        })
    finally:
        settings.use_app_map_cache = orig


# ── Win 3: batch all scroll screenshots into one LLM call ────────────────────

def _norm_to_px(vals: list, img_w: int, img_h: int) -> list[int]:
    return [int(v * (img_w if i % 2 == 0 else img_h)) for i, v in enumerate(vals)]


def _collect_scrolled_elements(base_elements: list, image_path: str, screen_id: str) -> list:
    """
    Scroll the page and collect elements below the viewport in a SINGLE LLM call.

    All scroll screenshots are sent together as multiple images in one message
    instead of one LLM call per scroll position.
    """
    try:
        scroll_info = robot.get_page_scroll_info()
    except Exception:
        return base_elements

    total_h    = scroll_info.get("scrollHeight",   _VIEWPORT_H)
    viewport_h = scroll_info.get("viewportHeight", _VIEWPORT_H)

    if total_h <= viewport_h + 50:
        return base_elements

    print(f"  [SCROLL]  page height {total_h}px — viewport {viewport_h}px — batching scroll screenshots…")

    # ── Collect all scroll screenshots ────────────────────────────────────────
    scroll_captures: list[tuple[str, int]] = []   # (image_path, scroll_y_offset)
    step = int(viewport_h * 0.8)
    scroll_y = 0

    while True:
        scroll_y += step
        robot.scroll_page(_VIEWPORT_W // 2, viewport_h // 2, step)
        time.sleep(0.5)

        actual = robot.get_page_scroll_info().get("scrollTop", scroll_y)
        if actual <= scroll_y - step + 10:
            break   # browser couldn't scroll further — hit the bottom

        ts   = int(time.time() * 1000)
        path = str(Path(settings.screenshots_dir) / f"scroll_{screen_id}_{ts}.png")
        robot.capture_screen(path)
        scroll_captures.append((path, actual))

        if actual + viewport_h >= total_h - 20:
            break

    # Reset scroll to top
    robot.scroll_page(_VIEWPORT_W // 2, viewport_h // 2, -total_h)
    time.sleep(0.4)

    if not scroll_captures:
        return base_elements

    # ── Build one multi-image message ─────────────────────────────────────────
    content: list = []
    for i, (path, sy) in enumerate(scroll_captures, start=1):
        img_bytes = get_storage().load(path)
        b64 = base64.standard_b64encode(img_bytes).decode()
        content.append({"type": "text", "text": f"Screenshot {i} (scroll_y={sy}px from top):"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        })
    content.append({
        "type": "text",
        "text": BATCH_SCROLL_ELEMENTS.format(count=len(scroll_captures)),
    })

    # ── One LLM call for all scroll screenshots ───────────────────────────────
    llm = get_llm()
    raw = llm.invoke([HumanMessage(content=content)]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    result = json.loads(raw)

    # ── Adjust y-coordinates by scroll offset, convert normalized → pixels ────
    all_elements = list(base_elements)
    for entry in result.get("screenshots") or []:
        idx = entry.get("index", 1) - 1
        if idx < 0 or idx >= len(scroll_captures):
            continue
        path, sy = scroll_captures[idx]
        img_bytes = get_storage().load(path)
        img_w, img_h = Image.open(io.BytesIO(img_bytes)).size

        for el in entry.get("elements") or []:
            el = dict(el)
            el["bbox"]   = _norm_to_px(el.get("bbox",   [0.0, 0.0, 0.0, 0.0]), img_w, img_h)
            el["center"] = _norm_to_px(el.get("center", [0.5, 0.5]),            img_w, img_h)
            # Shift y by the scroll offset → absolute kiosk pixel y
            el["center"][1] += sy
            if el.get("bbox") and len(el["bbox"]) == 4:
                el["bbox"][1] += sy
                el["bbox"][3] += sy
            all_elements.append(el)

    return _dedup_elements(all_elements)


def _dedup_elements(elements: list, radius: int = 30) -> list:
    result: list = []
    for el in elements:
        cx, cy = el["center"]
        if not any(
            abs(e["center"][0] - cx) < radius and abs(e["center"][1] - cy) < radius
            for e in result
        ):
            result.append(el)
    return result


# ── Annotated screenshot ──────────────────────────────────────────────────────

def _save_annotated(image_path: str, screen_id: str, elements: list) -> None:
    """Draw bounding boxes and centre dots on the screenshot for visual QA."""
    annotated_dir = Path(settings.screenshots_dir) / "annotated"
    annotated_dir.mkdir(parents=True, exist_ok=True)

    img  = Image.open(image_path).copy()
    draw = ImageDraw.Draw(img)
    palette = {
        "button": "#2563EB", "input": "#16A34A", "link": "#7C3AED",
        "select": "#D97706", "text": "#6B7280",
    }

    for el in elements:
        color = palette.get(el.get("type", ""), "#F59E0B")
        if el.get("bbox") and len(el["bbox"]) == 4:
            x1, y1, x2, y2 = el["bbox"]
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
        cx, cy = el["center"]
        r = 5
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        lx = el["bbox"][0] if el.get("bbox") else cx + 8
        ly = (el["bbox"][1] - 14) if el.get("bbox") else (cy - 14)
        label = f"{el['id']} [{el['type']}]"
        draw.text((lx + 1, ly + 1), label, fill="#000000")
        draw.text((lx, ly), label, fill=color)

    ts      = int(time.time() * 1000)
    out_path = annotated_dir / f"{screen_id}_{ts}.png"
    img.save(str(out_path))
    print(f"  [ANNOTATE] {out_path.name}  ({len(elements)} elements)")


# ── Keyboard mapper ───────────────────────────────────────────────────────────

def _map_keyboard(elements: list, app_map: dict) -> dict:
    """Tap first INPUT element (not a text label), screenshot keyboard, map keys."""
    if "keyboard_map" in app_map:
        return app_map

    # Only activate on genuine input fields — exclude "text" type (headings/labels)
    input_el = next(
        (e for e in elements if e.get("type") in ("input", "email", "password")),
        None,
    )
    if input_el is None:
        return app_map

    cx, cy = input_el["center"]
    px, py = int(cx), int(cy)
    print(f"\n  [KEYBOARD] Tapping '{input_el['id']}' @ ({px},{py}) to reveal keyboard…")
    robot.tap(px, py)
    time.sleep(0.6)

    ts      = int(time.time() * 1000)
    kb_path = str(Path(settings.screenshots_dir) / f"keyboard_map_{ts}.png")
    Path(kb_path).parent.mkdir(parents=True, exist_ok=True)
    robot.capture_screen(kb_path)

    image_bytes = get_storage().load(kb_path)
    b64         = base64.standard_b64encode(image_bytes).decode()
    llm = get_llm()
    msg = HumanMessage(content=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
        {"type": "text", "text": MAP_KEYBOARD},
    ])
    raw = llm.invoke([msg]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    kb_data = json.loads(raw)

    key_count = len(kb_data.get("keys", {}))
    print(f"  [KEYBOARD] Mapped {key_count} keys")

    if key_count == 0:
        print("  [KEYBOARD] No keys found — virtual keyboard may not have appeared")
        return app_map

    done_coords = (
        kb_data["keys"].get("done")
        or kb_data["keys"].get("return")
        or kb_data["keys"].get("enter")
    )
    if done_coords:
        dx, dy = int(done_coords[0] * _VIEWPORT_W), int(done_coords[1] * _VIEWPORT_H)
        print(f"  [KEYBOARD] Dismissing via 'done' @ ({dx},{dy})")
        robot.tap(dx, dy)
        time.sleep(0.4)
    else:
        print("  [KEYBOARD] 'done' key not found — keyboard may still be visible")

    robot.set_keyboard_map(kb_data)
    return {**app_map, "keyboard_map": kb_data}


# ── Main node ─────────────────────────────────────────────────────────────────

def explore_screen(state: ExplorerState) -> dict:
    # ── 1. Analyze screenshot — uses original ANALYZE_SCREEN prompt ───────────
    #    Cache bypassed so visually-similar screens get fresh identification.
    #    Coordinate accuracy is preserved (same prompt, same Pass 2 correction).
    vision_result = _analyze_fresh(state["current_image_path"])
    screen    = vision_result["screen_analysis"]
    screen_id = screen["screen_id"]

    # ── 2. Always prefer identify_result's screen_id over _analyze_fresh ──────
    #    _analyze_fresh can misidentify visually-similar screens (e.g. sign-up
    #    form → "login" because both have email+password inputs, or developer
    #    settings → "unknown").  identify_result has navigation context so its
    #    screen_id is authoritative.
    fallback = state.get("last_result_screen_id", "")
    if fallback and fallback not in ("unknown", "") and screen_id != fallback:
        print(f"  [EXPLORE] screen_id '{screen_id}' → using identify_result '{fallback}'")
        screen_id = fallback
        screen = {**screen, "screen_id": fallback}

    # ── 3. Add/update screen in AppMap ────────────────────────────────────────
    app_map  = {**state["app_map"], "screens": dict(state["app_map"].get("screens") or {})}
    existing = app_map["screens"].get(screen_id, {})

    if not existing.get("elements"):
        # New screen or skeleton (elements:[]) from identify_result — full capture
        elements = screen.get("elements") or []
        elements = _collect_scrolled_elements(elements, state["current_image_path"], screen_id)

        image_bytes = get_storage().load(state["current_image_path"])
        screen_hash = compute_hash(image_bytes)
        is_dynamic  = any(kw in screen_id.lower() for kw in _DYNAMIC_SCREEN_KEYWORDS)
        label       = "New screen" if screen_id not in app_map["screens"] else "Skeleton updated"
        tag         = "DYNAMIC" if is_dynamic else "STATIC"
        print(f"\n  [EXPLORE] {label}: '{screen_id}' [{tag}]  {len(elements)} elements  hash={screen_hash[:12]}…")

        app_map["screens"][screen_id] = {
            "screen_id":   screen_id,
            "description": screen["description"],
            "elements":    elements,
            "transitions": existing.get("transitions") or {},
            "screen_hash": screen_hash,
            "is_dynamic":  is_dynamic,
        }
        _save_annotated(state["current_image_path"], screen_id, elements)
    else:
        print(f"\n  [EXPLORE] Re-visiting '{screen_id}' ({len(existing['elements'])} elements already mapped)")
        elements = existing["elements"]

    # ── 4. Map the virtual keyboard (once per app) ────────────────────────────
    app_map = _map_keyboard(elements, app_map)

    # ── 5. Ask Claude to suggest explorable actions (text-only, no image re-sent)
    creds   = state.get("credentials") or {}
    valid   = creds.get("valid", {})
    invalid = creds.get("invalid", {})

    elements_text = "\n".join(
        f"  {el['id']} [{el['type']}] \"{el.get('label', '')}\" — {el.get('description', '')}"
        for el in elements
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

    # ── 6. Queue actions not yet explored ─────────────────────────────────────
    explored    = set(state.get("explored_action_keys") or [])
    new_actions: list[ExplorationAction] = []
    for a in data.get("explorable_actions") or []:
        full_key = f"{screen_id}::{a['action_key']}"
        if full_key not in explored:
            new_actions.append({
                "action_key":          a["action_key"],
                "screen_id":           screen_id,
                "description":         a.get("description", ""),
                "steps":               a.get("steps") or [],
                "credential_scenario": a.get("credential_scenario"),
            })

    queue = list(state.get("exploration_queue") or []) + new_actions
    print(f"  [EXPLORE] '{screen_id}': queued {len(new_actions)} new actions  (queue depth: {len(queue)})")

    approach_paths = dict(state.get("approach_paths") or {})
    if screen_id not in approach_paths:
        approach_paths[screen_id] = []

    return {
        "app_map":            app_map,
        "current_screen_id":  screen_id,
        "exploration_queue":  queue,
        "last_result_is_new": False,
        "approach_paths":     approach_paths,
    }
