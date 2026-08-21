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
import math
import re
import time
import base64
from pathlib import Path
from PIL import Image, ImageDraw
from langchain_core.messages import HumanMessage
from vision_agent.nodes.analyze import analyze_screen as _analyze
from vision_agent.llm import get_llm, get_explorer_llm, invoke_json, detect_image_media_type
from vision_agent.screen_cache import compute_hash
from vision_agent.storage import get_storage
from vision_agent.config import settings
from vision_agent import robot
from app_explorer.state import ExplorerState, ExplorationAction
from app_explorer.prompts import SUGGEST_EXPLORABLE_ACTIONS, BATCH_SCROLL_ELEMENTS, MAP_KEYBOARD

# Screen IDs containing these keywords are marked is_dynamic=True in the app_map.
# Dynamic screens display user-specific or query-specific content that changes between visits
# (e.g. cart contents, search results, order history, booking details).
# Extend this set for domains not covered here — the list is not app-specific.
_DYNAMIC_SCREEN_KEYWORDS = {
    # Shopping / commerce
    "cart", "basket", "bag", "wishlist",
    # Transactions / orders
    "order", "transaction", "receipt", "invoice", "payment",
    # Search / filter
    "search", "result", "filter", "query",
    # User activity / history
    "history", "activity", "log", "timeline", "audit",
    # Scheduling / reservations
    "booking", "reservation", "appointment", "schedule",
    # User-specific data
    "profile", "account", "dashboard", "personalised", "personalized",
}
# Scroll-math fallbacks (used only when the backend can't report live viewport dims). Sourced from
# settings so they track a non-default exploration viewport (e.g. 1920×1080 to match a kiosk camera).
from vision_agent.config import settings as _settings  # noqa: E402
_VIEWPORT_W = _settings.viewport_width
_VIEWPORT_H = _settings.viewport_height


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

# Scale-tolerant coordinate conversion (handles normalized 0-1 AND raw-pixel values that
# vision models sometimes mix in one response). Single source of truth in analyze.py.
from vision_agent.nodes.analyze import _norm_to_px  # noqa: E402


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
            "source": {"type": "base64", "media_type": detect_image_media_type(img_bytes), "data": b64},
        })
    content.append({
        "type": "text",
        "text": BATCH_SCROLL_ELEMENTS.format(count=len(scroll_captures)),
    })

    # ── One LLM call for all scroll screenshots ───────────────────────────────
    result = invoke_json(get_explorer_llm(), [HumanMessage(content=content)],
                         default={"screenshots": []}, label="scroll")

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
            # Shift y by the scroll offset → absolute screen pixel y
            el["center"][1] += sy
            if el.get("bbox") and len(el["bbox"]) == 4:
                el["bbox"][1] += sy
                el["bbox"][3] += sy
            all_elements.append(el)

    return _dedup_elements(all_elements)


# Generic tokens that carry no identity — dropped before comparing a semantic element id to a DOM
# testid/aria-label, so the match keys on the meaningful words (product + action), not boilerplate.
_GENERIC_ID_TOKENS = {
    "button", "btn", "link", "input", "field", "icon", "control", "tab", "the", "a", "an",
    "of", "to", "for", "value", "quantity", "qty", "item", "el", "element", "screen", "id",
}

# Direction synonyms — Claude names a quantity stepper by the SYMBOL/direction it sees ("plus"/"minus")
# while the DOM names it by the ACTION ("increase"/"decrease"): app_map `nexora_quantity_plus` vs DOM
# testid `quantity-increase-<id>` / aria "Increase … quantity". Without canonicalising these to a common
# token the only shared identity token is the product name (1 token → too weak to snap), so the stepper
# keeps its raw vision coordinate and — in the tight arm-reachable layout — lands on the Add-to-Cart
# button just below it (observed: qty never incremented, so Add-to-Cart stayed disabled and the whole
# cart→payment→success flow was skipped). Mapping plus↔increase / minus↔decrease makes the match
# {product, increase} = 2 tokens → an unambiguous strong snap. NOTE: "add"/"remove" are deliberately NOT
# synonyms of increase/decrease — they'd collide with the Add-to-Cart / cart-remove buttons.
_TOKEN_SYNONYMS = {
    "plus": "increase", "increment": "increase", "incr": "increase",
    "minus": "decrease", "decrement": "decrease", "decr": "decrease",
}


def _meaningful_tokens(*strings: str) -> set:
    """Lowercase alphanumeric tokens from the given strings, minus generic boilerplate, with direction
    words canonicalised (plus→increase, minus→decrease).

    Splits on any non-alphanumeric boundary so `nexora_phone_increase_button`,
    `quantity-increase-nexora-phone-x2`, `Increase Nexora Phone X2 quantity` AND
    `nexora_quantity_plus` all reduce to the same identity tokens {nexora, …, increase}.
    """
    toks: set = set()
    for s in strings:
        for t in re.split(r"[^a-z0-9]+", (s or "").lower()):
            if t and len(t) >= 2 and t not in _GENERIC_ID_TOKENS:
                toks.add(_TOKEN_SYNONYMS.get(t, t))
    return toks


def _dom_correct_elements(elements: list) -> list:
    """Correct Claude's element coordinates using live DOM positions.

    Claude's vision analysis sometimes misplaces elements by 100-300 px — most
    commonly sidebar navigation items that sit at a different x than the product
    cards, confusing the model.  This function queries the browser DOM for every
    interactive element's actual center, then text-matches each Claude element to
    a DOM element.  If the match is unambiguous and the offset exceeds 30 px, the
    coordinate is corrected to the DOM-true value before it is stored in the map.

    Only runs when the backend supports DOM access (playwright mode).
    Real robot arm stubs return [] — correction is skipped transparently.
    """
    try:
        dom_els = robot.get_dom_element_centers()
    except AttributeError:
        return elements

    if not dom_els:
        return elements

    corrected = []
    used: set[int] = set()   # DOM element indices already claimed (each maps to one Claude element)
    for el in elements:
        label = (el.get("label") or "").lower().strip()
        if not label:
            corrected.append(el)
            continue

        cx, cy = el["center"][0], el["center"][1]

        # Is Claude's estimate even on-screen? Vision occasionally returns coordinates on the
        # wrong scale (e.g. 0-1000 instead of 0-1), landing far off-canvas. When the estimate is
        # off-screen its distance to the real element is meaningless — so we must NOT reject a
        # text match on distance. We snap by TEXT and use distance only to disambiguate multiple
        # same-text matches when the estimate is plausible; otherwise we fall back to DOM order.
        plausible = (0 <= cx <= _VIEWPORT_W * 1.1) and (0 <= cy <= _VIEWPORT_H * 1.1)

        # Find DOM elements whose text overlaps with the Claude label.
        # Ratio guard: when checking "label in dom_text", the label must cover
        # at least 70% of the DOM text to prevent short labels like "password"
        # from matching "Forgot password?" (ratio 8/16 = 0.50 → rejected).
        # Full strings ("Categories" / "Sign In") hit ratio 1.0 and pass.
        matches = []
        for idx, dom_el in enumerate(dom_els):
            if idx in used:
                continue   # one DOM element maps to at most one Claude element
            dom_text = dom_el["text"].lower().strip()
            if not dom_text or len(dom_text) < 2:
                continue

            label_in_dom = (label in dom_text) and (len(label) / len(dom_text) >= 0.7)
            dom_in_label = dom_text in label   # dom is a subset of the label — always safe
            if not (label_in_dom or dom_in_label):
                continue

            dist = math.hypot(dom_el["cx"] - cx, dom_el["cy"] - cy)
            # Prefer type-matching elements (input→input, button→button, link→a/button).
            el_type = el.get("type", "")
            dom_tag = dom_el.get("tag", "")
            type_match = (
                (el_type == "input"  and dom_tag == "input")  or
                (el_type == "button" and dom_tag == "button") or
                (el_type == "link"   and dom_tag in ("a", "button"))
            )
            matches.append((0 if type_match else 1, dist, idx, dom_el))

        # Fallback — semantic id ↔ DOM testid/aria token match. The text pass above misses controls
        # whose DOM text is a bare symbol ("+"/"−") or whose vision label doesn't overlap the DOM text
        # (quantity steppers, icon-only buttons). Their coordinates then keep the raw vision estimate,
        # which in a TIGHT single-column layout can land inside an ADJACENT element — observed: the "+"
        # stepper estimate fell inside the Add-to-Cart button just below it, so the walkthrough tapped a
        # disabled button and the whole purchase flow (cart → payment → success) never got explored.
        # The app_map element id is Claude's reliable semantic name; match its tokens against each unused
        # DOM element's testid + aria-label (layout-independent ground truth) and snap to the DOM centre.
        if not matches:
            id_tokens = _meaningful_tokens(el.get("id", ""), label)
            el_type   = el.get("type", "")
            if id_tokens:
                cands = []   # (overlap_count, type_match, idx, dom_el) for every unused DOM element sharing ≥1 token
                for idx, dom_el in enumerate(dom_els):
                    if idx in used:
                        continue
                    dom_tokens = _meaningful_tokens(dom_el.get("testid", ""), dom_el.get("aria", ""))
                    overlap = id_tokens & dom_tokens
                    if not overlap:
                        continue
                    dom_tag = dom_el.get("tag", "")
                    type_match = (
                        (el_type == "input"  and dom_tag == "input")  or
                        (el_type == "button" and dom_tag == "button") or
                        (el_type == "link"   and dom_tag in ("a", "button"))
                    )
                    cands.append((len(overlap), type_match, idx, dom_el))

                pick = None
                strong = [c for c in cands if c[0] >= 2]
                if strong:
                    # ≥2 shared identity tokens → unambiguous (e.g. {nexora, phone, increase} is unique
                    # to that one product's control). Prefer most overlap, then a type match, then nearest.
                    strong.sort(key=lambda c: (-c[0], not c[1],
                                               math.hypot(c[3]["cx"] - cx, c[3]["cy"] - cy) if plausible else c[2]))
                    pick = strong[0]
                else:
                    # A SINGLE shared SPECIFIC token is enough ONLY when it is UNAMBIGUOUS: exactly one
                    # unused, TYPE-COMPATIBLE DOM element shares it. This snaps login inputs — app_map id
                    # 'email_input'/'password_input' vs DOM testid 'signin-email'/'signin-password' share
                    # only 'email'/'password' ('input' is generic) — to their TRUE DOM centre, which the
                    # raw vision estimate placed ~40-70px too high (observed: the arm tapped the TOP edge
                    # of the email field, not its centre). Safe: if two elements share the token (e.g. two
                    # 'add' buttons) there is >1 candidate → stays ambiguous → no snap (keeps vision).
                    typed_single = [c for c in cands if c[1]]
                    if len(typed_single) == 1:
                        pick = typed_single[0]

                if pick:
                    _, _, t_idx, t_dom = pick
                    matches = [(0, math.hypot(t_dom["cx"] - cx, t_dom["cy"] - cy), t_idx, t_dom)]

        if matches:
            # Rank type-match first. Among equal type-rank, prefer the closest to Claude's
            # estimate when that estimate is on-screen; otherwise use DOM order (idx) so repeated
            # labels/placeholders (e.g. two "e.g. 1234" inputs) map to distinct elements in order.
            matches.sort(key=(lambda m: (m[0], m[1])) if plausible else (lambda m: (m[0], m[2])))
            _, best_dist, best_idx, best = matches[0]
            used.add(best_idx)
            el = dict(el)
            # Store the matched DOM testid (stable across runs, great for determinism).
            if best.get("testid"):
                el["testid"] = best["testid"]
            # The DOM centre is exact ground truth — snap to it. This fixes vision coordinates
            # that were mis-scaled or misplaced, regardless of how far off Claude's estimate was.
            if [best["cx"], best["cy"]] != [cx, cy]:
                if not plausible or best_dist > 30:
                    print(
                        f"  [DOM-FIX] '{el['id']}': ({cx},{cy}) -> ({best['cx']},{best['cy']})"
                        f"  d={best_dist:.0f}px  label='{label[:20]}' dom='{best['text'][:20]}'"
                    )
                new_cx, new_cy = best["cx"], best["cy"]
                el["center"] = [new_cx, new_cy]
                if el.get("bbox") and len(el["bbox"]) == 4:
                    bw = abs(el["bbox"][2] - el["bbox"][0])
                    bh = abs(el["bbox"][3] - el["bbox"][1])
                    # A mis-scaled bbox is also garbage — rebuild a sane one around the centre.
                    if not plausible:
                        bw, bh = (min(bw, 220) or 120), (min(bh, 90) or 44)
                    el["bbox"] = [new_cx - bw // 2, new_cy - bh // 2,
                                  new_cx + bw // 2, new_cy + bh // 2]

        corrected.append(el)
    return corrected


# Interactive DOM tags a backfilled element may come from → its app_map element "type".
_BACKFILL_TAG_TYPE = {"input": "input", "select": "input", "a": "link", "button": "button"}


def _testid_to_element_id(testid: str) -> str:
    """Derive a readable app_map element id from a stable data-testid.

    e.g. 'station-topup-tap' → 'topup_tap', 'signin-email' → 'signin_email'. A common leading
    'station-'/'kiosk-' UI-scope prefix is dropped so the id reads as the control, not the page.
    """
    t = (testid or "").strip().lower()
    for pfx in ("station-", "kiosk-", "pos-", "data-"):
        if t.startswith(pfx):
            t = t[len(pfx):]
            break
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_") or "control"


def _backfill_unmatched_dom(elements: list) -> list:
    """Add interactive DOM controls (buttons/inputs/links carrying a data-testid) that Claude's
    vision pass never proposed — so re-exploration doesn't silently DROP a real, tappable control.

    Root cause this fixes: when two controls share the SAME visible label (e.g. the VPS card
    station renders TWO 'Tap Real Card' buttons — one to issue a new card, one to top up an
    existing card), vision emits a single element for the pair and the second button is lost from
    the app_map. Any test needing the missing button (TC-VPS-011 "add money to an existing card")
    then can't be planned against it and taps the wrong control.

    Conservative by design — it only adds a DOM node when ALL of these hold, so it never
    duplicates or displaces a vision-mapped element:
      • the node carries a stable data-testid (an intentional, layout-independent hook),
      • its tag is interactive (button / input / select / a),
      • no existing element already claims that testid, AND
      • no existing element already sits on that spot (within 24px) under a different id.
    Runs only where the backend exposes the DOM (playwright); real-arm stubs return [] → skipped.
    """
    try:
        dom_els = robot.get_dom_element_centers()
    except AttributeError:
        return elements
    if not dom_els:
        return elements

    have_testids = {(e.get("testid") or "").strip() for e in elements if e.get("testid")}
    centers = [tuple(e["center"]) for e in elements if e.get("center")]
    added: list = []
    for dom_el in dom_els:
        tid = (dom_el.get("testid") or "").strip()
        tag = dom_el.get("tag", "")
        if not tid or tid in have_testids or tag not in _BACKFILL_TAG_TYPE:
            continue
        cx, cy = dom_el["cx"], dom_el["cy"]
        if any(abs(px - cx) < 24 and abs(py - cy) < 24 for px, py in centers):
            continue   # a vision element already covers this spot (mapped under a different id)
        el_type = _BACKFILL_TAG_TYPE[tag]
        eid = _testid_to_element_id(tid)
        # guarantee a unique id within the screen
        base = eid
        n = 2
        existing_ids = {e.get("id") for e in elements} | {a["id"] for a in added}
        while eid in existing_ids:
            eid = f"{base}_{n}"; n += 1
        label = (dom_el.get("text") or dom_el.get("aria") or "").strip()[:60]
        bw, bh = (220 if el_type == "input" else 120), 44
        added.append({
            "id": eid, "type": el_type, "label": label,
            "center": [cx, cy],
            "bbox": [cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2],
            "confidence": 0.6, "testid": tid,
            "description": f"Backfilled from DOM testid={tid!r} — vision did not emit a distinct "
                           f"element (commonly a duplicate-label control).",
        })
        have_testids.add(tid)
        centers.append((cx, cy))
    if added:
        print(f"  [DOM-BACKFILL] +{len(added)} unmapped testid control(s): "
              + ", ".join(f"{a['id']}@({a['center'][0]},{a['center'][1]})" for a in added))
    return elements + added


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
    # Annotated shots live in the SHARED base screenshots/annotated/ folder (keyed by screen_id
    # across the whole multi-app map), NOT in the per-exploration subfolder that settings.screenshots_dir
    # points at during a run.  This keeps the App Map page's annotated view (served from the fixed
    # screenshots/annotated/) working, and keeps re-explorations' annotated shots coherent per screen.
    annotated_dir = Path(settings.app_map_path).parent / "screenshots" / "annotated"
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
            # Normalize: PIL requires top-left ≤ bottom-right
            draw.rectangle([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)], outline=color, width=2)
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
    print(f"\n  [KEYBOARD MAP - ONE-TIME SETUP] Tapping '{input_el['id']}' @ ({px},{py}) to reveal virtual keyboard…")
    try:
        robot.tap(px, py)
    except Exception as e:
        print(f"  [KEYBOARD MAP] Browser error during tap ({e}) — skipping keyboard mapping for now")
        return app_map
    time.sleep(0.6)

    ts      = int(time.time() * 1000)
    kb_path = str(Path(settings.screenshots_dir) / f"keyboard_map_{ts}.png")
    Path(kb_path).parent.mkdir(parents=True, exist_ok=True)
    robot.capture_screen(kb_path)

    image_bytes = get_storage().load(kb_path)
    b64         = base64.standard_b64encode(image_bytes).decode()
    media_type  = detect_image_media_type(image_bytes)
    llm = get_llm()
    msg = HumanMessage(content=[
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": MAP_KEYBOARD},
    ])
    kb_data = invoke_json(llm, [msg], default={"keys": {}}, label="keyboard")

    key_count = len(kb_data.get("keys", {}))
    print(f"  [KEYBOARD] Mapped {key_count} keys")

    if key_count == 0:
        print("  [KEYBOARD] No keys found — virtual keyboard may not have appeared")
        return app_map

    # Load the map FIRST so type_text("") can use the done-key coords as fallback
    robot.set_keyboard_map(kb_data)

    # Dismiss the keyboard via type_text("") which tries data-testid="keyboard-done"
    # before falling back to the map coordinate.  Avoids robot.tap() which uses
    # DOM snap and can accidentally land on a nearby key (e.g. '-') and type it.
    done_coords = kb_data["keys"].get("done") or kb_data["keys"].get("return") or kb_data["keys"].get("enter")
    if done_coords:
        dx, dy = int(done_coords[0] * _VIEWPORT_W), int(done_coords[1] * _VIEWPORT_H)
        print(f"  [KEYBOARD] Dismissing via 'done' @ ({dx},{dy})")
    else:
        print("  [KEYBOARD] 'done' key not found — attempting dismiss via testid")
    robot.type_text("")   # no chars typed; just triggers testid-first keyboard dismiss
    time.sleep(0.3)

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

    if not existing.get("elements") or existing.get("is_dynamic"):
        # New screen, skeleton, OR dynamic screen (cart/payment/etc.) — always full re-capture.
        # Dynamic screens change content between visits (cart empty vs. with items, payment
        # state, order history), so stored elements may be stale.  Re-analyzing on every visit
        # ensures the walkthrough sees the proceed-to-payment button when the cart has items,
        # even if the main exploration first visited the cart when it was empty.
        elements = screen.get("elements") or []
        elements = _dom_correct_elements(elements)   # fix coordinates using live DOM positions
        elements = _backfill_unmatched_dom(elements) # add testid'd controls vision missed (dup labels)
        elements = _collect_scrolled_elements(elements, state["current_image_path"], screen_id)

        image_bytes = get_storage().load(state["current_image_path"])
        screen_hash = compute_hash(image_bytes)
        is_dynamic  = any(kw in screen_id.lower() for kw in _DYNAMIC_SCREEN_KEYWORDS)
        if screen_id not in app_map["screens"]:
            label = "New screen"
        elif existing.get("is_dynamic"):
            label = "Dynamic re-capture"
        else:
            label = "Skeleton updated"
        tag         = "DYNAMIC" if is_dynamic else "STATIC"
        dom_id      = robot.get_dom_screen_id()   # e.g. "products", "categories", ""
        print(f"\n  [EXPLORE] {label}: '{screen_id}' [{tag}]  {len(elements)} elements  hash={screen_hash[:12]}…")
        if dom_id:
            print(f"  [EXPLORE] DOM screen: '{dom_id}'")

        app_map["screens"][screen_id] = {
            "screen_id":            screen_id,
            "description":          screen["description"],
            "elements":             elements,
            "transitions":          existing.get("transitions") or {},
            "screen_hash":          screen_hash,
            "is_dynamic":           is_dynamic,
            "dom_id":               dom_id,   # DOM testid → reliably identifies SPA state views
            "reference_screenshot": state["current_image_path"],  # raw capture for real-robot verification
        }
        _save_annotated(state["current_image_path"], screen_id, elements)
    else:
        # Static screen already fully mapped — just refresh the reference screenshot.
        print(f"\n  [EXPLORE] Re-visiting static '{screen_id}' ({len(existing['elements'])} elements already mapped)")
        elements = existing["elements"]
        app_map["screens"][screen_id] = {
            **existing,
            "reference_screenshot": state["current_image_path"],
        }

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

    captured = dict(state.get("captured_values") or {})
    available_captured = (
        "\n".join(f"  {{{{captured.{k}}}}} = {v!r}" for k, v in captured.items())
        if captured else "  (none captured yet)"
    )

    prompt = SUGGEST_EXPLORABLE_ACTIONS.format(
        screen_id=screen_id,
        screen_description=screen["description"],
        elements_text=elements_text,
        available_captured=available_captured,
        valid_email=valid.get("email", "tester@example.com"),
        valid_password=valid.get("password", "Password123"),
        invalid_email=invalid.get("email", "baduser@example.com"),
        invalid_password=invalid.get("password", "WrongPass!"),
    )

    data = invoke_json(get_explorer_llm(), [HumanMessage(content=prompt)],
                       default={"explorable_actions": []}, label="suggest_actions")

    # Accumulate any identifier Claude captured on this screen (issued card number, order id, …)
    # so later screens' management flows can reuse it via {{captured.NAME}}.  Never overwrite an
    # existing capture with an empty/placeholder value.
    new_caps = data.get("captured_values") or {}
    if isinstance(new_caps, dict):
        for k, v in new_caps.items():
            if v and str(v).strip() and "{{" not in str(v):
                captured[str(k)] = str(v).strip()
        if new_caps:
            print(f"  [EXPLORE] '{screen_id}': captured values → {list(new_caps.keys())}")

    # ── 5b. Persist element dependencies as ground truth on the screen ────────
    # These record which action elements (add / submit / confirm / proceed) consume state
    # set by other elements on this screen, plus the recipe to satisfy the precondition.
    # The test planner reads these instead of re-inferring prerequisites every run.
    from app_map.store import sane_dependency as _sane_dep
    deps = [
        d for d in (data.get("element_dependencies") or [])
        if d.get("element_id") and d.get("requires") and _sane_dep(d, elements)
    ]
    _rejected = [d.get("element_id") for d in (data.get("element_dependencies") or [])
                 if d.get("element_id") and d.get("requires") and not _sane_dep(d, elements)]
    if _rejected:
        print(f"  [EXPLORE] '{screen_id}': dropped {len(_rejected)} invalid dependency(ies) "
              f"(quantity stepper gating a non-add control): {_rejected}")
    if deps:
        app_map["screens"][screen_id] = {
            **app_map["screens"].get(screen_id, {}),
            "dependencies": deps,
        }
        print(f"  [EXPLORE] '{screen_id}': recorded {len(deps)} element dependency(ies) — "
              f"{', '.join(d['element_id'] for d in deps)}")

    # ── 6. Queue actions not yet explored ─────────────────────────────────────
    explored = set(state.get("explored_action_keys") or [])

    # Screens that have been fully mapped already (have element lists).
    # Used to deduplicate sidebar-nav actions: once "categories" is in the map
    # there is no need to queue navigate_categories from every other screen.
    fully_explored_ids = {
        sid for sid, sc in (app_map.get("screens") or {}).items()
        if sc.get("elements")
    }

    # Global element_transitions: element_id → dest_screen_id collected from all
    # previously executed single-tap actions.  Used to skip "sign_out_button" or
    # "home_button" actions on every screen once the destination is already mapped.
    element_transitions = app_map.get("element_transitions") or {}

    new_actions: list[ExplorationAction] = []
    skipped_dedup = 0
    for a in data.get("explorable_actions") or []:
        full_key = f"{screen_id}::{a['action_key']}"
        if full_key in explored:
            continue

        steps = a.get("steps") or []
        if len(steps) == 1 and steps[0].get("action_type") == "tap":
            eid = steps[0].get("element_id", "")

            # Element-level dedup: if we already know where this exact element leads
            # and that destination is fully explored, there is no new information to gain.
            # Exception: if the known destination IS this screen (element stayed put),
            # don't dedup — the element has an unmet precondition; a multi-step action
            # that sets up the required state may navigate to a new screen.
            if eid and eid in element_transitions:
                known_dest = element_transitions[eid]
                if known_dest in fully_explored_ids and known_dest != screen_id:
                    skipped_dedup += 1
                    continue

            # Action-key dedup: skip nav actions whose action_key embeds a fully-mapped
            # screen name (e.g. "navigate_reports" once "reports" is fully explored).
            # Guard: len(sid) >= 5 avoids false matches for very short screen IDs.
            action_key_lower = a["action_key"].lower()
            if any(sid in action_key_lower for sid in fully_explored_ids if len(sid) >= 5):
                skipped_dedup += 1
                continue

        new_actions.append({
            "action_key":          a["action_key"],
            "screen_id":           screen_id,
            "description":         a.get("description", ""),
            "steps":               steps,
            "credential_scenario": a.get("credential_scenario"),
        })

    if skipped_dedup:
        print(f"  [DEDUP]  Skipped {skipped_dedup} nav action(s) — destinations already mapped")

    # DFS order: prepend new actions so we explore the current branch deeply
    # before backtracking.  Combined with stateful execution this means
    # consecutive actions from the same source screen need no browser reset.
    existing_queue = list(state.get("exploration_queue") or [])
    queue = new_actions + existing_queue
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
        "captured_values":    captured,
    }
