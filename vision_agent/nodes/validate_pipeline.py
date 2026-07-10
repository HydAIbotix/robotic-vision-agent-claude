"""
validate_pipeline — 4-node content validation pipeline.

Pipeline (nodes run in priority order; stops at first definitive answer):

  Node 1A — DOM screen identity           (playwright only)
    get_dom_screen_id() → fast, zero LLM

  Node 1B — Perceptual hash match         (real robot)
    Compare current screenshot against stored screen_hash in app_map.
    Zero LLM, sub-100 ms. Falls through to Node 3 when inconclusive.

  Node 2A — DOM text query                (playwright only)
    text_is_present() → DOM query, zero LLM

  Node 2B — Region OCR at known coords    (real robot)
    pytesseract on a 320×60 px crop around each element's center.
    Zero LLM.  Falls back to full-image OCR, then to Claude Opus when
    pytesseract is not installed or extracts too little text.

  Node 3 — Claude Vision Fallback         (all backends, last resort)
    Only invoked when: hash is inconclusive, dynamic screen, or OCR fails.

Usage:
    result = run_validate_pipeline(
        expected_screen="products",
        expected_text="$2597.00",          # None to skip text check
        step_description="cart total shown",
        image_path="./screenshots/after.png",
        app_map={...},
        backend="playwright",              # or "real" or "demo"
        save_path="",
    )
    # result keys: success, screen_match, text_match, method, actual_screen, observation, note
"""
import re
from vision_agent import robot
from vision_agent.config import settings


def run_validate_pipeline(
    expected_screen: str,
    expected_text: str | None,
    step_description: str,
    image_path: str,
    app_map: dict,
    backend: str = "",
    save_path: str = "",
    value_element_id: str = "",
) -> dict:
    """
    Run the validation pipeline and return a structured result dict.

    Returns:
        success (bool|None)        — overall pass/fail; None = verification gap
        screen_match (bool|None)   — Node 1 result; None if skipped
        text_match (bool|None)     — Node 2 result; None if skipped
        method (str)               — which node produced the final verdict
        actual_screen (str)        — detected screen id (best effort)
        observation (str)          — human-readable explanation
        note (str)                 — extra diagnostic detail
    """
    backend = backend or settings.robot_backend
    fmt_note: str = ""   # set when a value matches only after ignoring number formatting (5000 vs 5,000)

    # ── Node 1: Screen Identity ───────────────────────────────────────────────
    screen_match: bool | None = None
    actual_screen: str = ""
    screen_note: str = ""

    if expected_screen:
        screen_in_map = expected_screen in (app_map.get("screens") or {})
        if not screen_in_map:
            # Screen not yet explored — not a test failure, just a gap in the map.
            return {
                "success":       None,
                "screen_match":  None,
                "text_match":    None,
                "method":        "verification_gap",
                "actual_screen": "",
                "observation":   (
                    f"Screen '{expected_screen}' is not in the app_map — "
                    f"cannot verify. Re-run App Explorer to add this screen."
                ),
                "note": "verification_gap: expected_screen not in app_map",
            }

        if backend == "playwright":
            # DOM path: fast, zero LLM
            vr = robot.verify_current_screen(expected_screen, app_map, save_path)
            actual_screen = vr.get("actual_screen", "")
            screen_match  = vr.get("match", False)
            id_method     = vr.get("method", "dom")
            if screen_match:
                screen_note = f"on '{actual_screen}' [{id_method}]"
            else:
                return {
                    "success":       False,
                    "screen_match":  False,
                    "text_match":    None,
                    "method":        "screen_id",
                    "actual_screen": actual_screen,
                    "observation":   (
                        f"Wrong screen: expected '{expected_screen}', "
                        f"got '{actual_screen}' [{id_method}]"
                    ),
                    "note": "",
                }

        elif backend == "demo":
            screen_match  = True
            actual_screen = expected_screen
            screen_note   = f"on '{expected_screen}' [demo]"

        else:
            # real robot — perceptual hash comparison (zero LLM)
            hr = _match_by_phash(image_path, app_map, expected_screen)
            phash_success = hr.get("success")   # True / False / None
            actual_screen = hr.get("actual_screen", "")
            id_method     = hr.get("method", "phash")
            if phash_success is False:
                return {
                    "success":       False,
                    "screen_match":  False,
                    "text_match":    None,
                    "method":        "screen_id",
                    "actual_screen": actual_screen,
                    "observation":   (
                        f"Wrong screen: expected '{expected_screen}', "
                        f"got '{actual_screen}' [{id_method}]"
                    ),
                    "note": hr.get("note", ""),
                }
            elif phash_success is True:
                screen_match  = True
                actual_screen = actual_screen or expected_screen
                screen_note   = f"on '{actual_screen}' [{id_method}]"
            # else None: inconclusive → falls through to Node 3

    # ── Node 2: Text / Content Validation ─────────────────────────────────────
    text_match: bool | None = None

    if expected_text:
        # ── Anchored read (preferred) ─────────────────────────────────────────
        # When the step names the specific element that holds the value, read THAT
        # element's live text — deterministic, field-exact, zero LLM.  Resolves the
        # "which of several amounts?" ambiguity and fixes both the verdict (no longer
        # passes on the wrong field) and the mismatch message.
        anchored = _anchored_read(value_element_id, expected_screen, app_map, backend, image_path)
        if anchored is not None:
            if _value_matches(expected_text, anchored):
                text_match = True
                if _is_format_only_diff(expected_text, anchored):
                    fmt_note = (f"Value matches after ignoring number formatting: expected "
                                f"'{expected_text}', screen shows '{anchored}'. The overall value is "
                                f"correct — human review: confirm the formatting difference is acceptable.")
                    print(f"  [VALIDATE] {fmt_note}")
            else:
                return {
                    "success":       False,
                    "screen_match":  screen_match,
                    "text_match":    False,
                    "method":        "text_validation_anchored",
                    "actual_screen": actual_screen,
                    "observation":   (f"Value mismatch on '{value_element_id}': expected "
                                      f"'{expected_text}', but the field shows '{anchored}'"),
                    "note":          "",
                }
        else:
            # ── Unanchored fallback: existing local-first path (DOM/OCR → Claude) ──
            text_match = _check_text(
                expected_text, backend, image_path, step_description,
                app_map, expected_screen,
            )
            if text_match is False:
                observed = _claude_extract_value(image_path, expected_text, step_description)
                obs = f"Value mismatch: expected '{expected_text}' on '{actual_screen or expected_screen}'"
                obs += f", but the screen shows '{observed}'" if observed else ", but it is not present"
                return {
                    "success":       False,
                    "screen_match":  screen_match,
                    "text_match":    False,
                    "method":        "text_validation",
                    "actual_screen": actual_screen,
                    "observation":   obs,
                    "note":          "",
                }

    # ── Node 3: Claude Vision Fallback ────────────────────────────────────────
    # Only invoked when: no expected_screen check was done, phash was inconclusive,
    # OR text check was inconclusive (None).
    if screen_match is None or (expected_text and text_match is None):
        claude_result = _claude_vision_validate(image_path, step_description, expected_screen)
        if not claude_result["success"]:
            return {
                "success":       False,
                "screen_match":  screen_match,
                "text_match":    text_match,
                "method":        "claude_vision_fallback",
                "actual_screen": claude_result.get("new_screen_id", actual_screen),
                "observation":   claude_result.get("observation", "Claude vision validation failed"),
                "note":          claude_result.get("recovery_hint") or "",
            }
        actual_screen = actual_screen or claude_result.get("new_screen_id", "")
        return {
            "success":       True,
            "screen_match":  screen_match if screen_match is not None else True,
            "text_match":    text_match   if text_match   is not None else True,
            "method":        "claude_vision_fallback",
            "actual_screen": actual_screen,
            "observation":   claude_result.get("observation", "Claude vision validated"),
            "note":          "",
        }

    # All active nodes passed
    parts = []
    if screen_match:
        parts.append(screen_note)
    if text_match:
        parts.append(f"text '{expected_text}' found")
    if fmt_note:
        parts.append(fmt_note)
    return {
        "success":       True,
        "screen_match":  screen_match,
        "text_match":    text_match,
        "method":        "pipeline",
        "actual_screen": actual_screen,
        "observation":   "; ".join(parts) or "validation passed",
        "note":          fmt_note,
        "human_review":  bool(fmt_note),
    }


# ── Node 1B: Perceptual hash match ────────────────────────────────────────────

def _match_by_phash(image_path: str, app_map: dict, expected_screen: str) -> dict:
    """
    Compare the current screenshot against stored screen_hash values in app_map.

    Thresholds (hex-char Hamming distance — 64 chars, 0 = identical):
      ≤ 8  : confident match        → success=True
      > 20 AND best_other ≤ 8: confident mismatch → success=False
      otherwise                     : inconclusive  → success=None

    Dynamic screens (is_dynamic=True) always return success=None so Claude
    can re-examine content that differs between sessions.
    """
    from pathlib import Path
    from app_map.store import get_phash_cache

    if not image_path or not Path(image_path).exists():
        return {"success": None, "method": "phash_no_image", "actual_screen": ""}

    try:
        from vision_agent.screen_cache import compute_hash
        current_hash = compute_hash(Path(image_path).read_bytes())
    except Exception as exc:
        print(f"  [PHASH] Hash error: {exc}")
        return {"success": None, "method": "phash_error", "actual_screen": ""}

    hash_cache = get_phash_cache(app_map)
    if expected_screen not in hash_cache:
        return {"success": None, "method": "phash_no_stored_hash", "actual_screen": ""}

    def _hamming(a: str, b: str) -> int:
        return sum(c1 != c2 for c1, c2 in zip(a, b))

    expected_dist = _hamming(current_hash, hash_cache[expected_screen])
    other_dists   = {
        sid: _hamming(current_hash, h)
        for sid, h in hash_cache.items()
        if sid != expected_screen
    }
    best_other_sid  = min(other_dists, key=other_dists.get) if other_dists else None
    best_other_dist = other_dists[best_other_sid] if best_other_sid else 999

    is_dynamic = (app_map.get("screens") or {}).get(expected_screen, {}).get("is_dynamic", False)
    print(
        f"  [PHASH] expected='{expected_screen}' dist={expected_dist}  "
        f"best_other='{best_other_sid}' dist={best_other_dist}  dynamic={is_dynamic}"
    )

    MATCH_THRESHOLD    = 8
    MISMATCH_THRESHOLD = 20

    if not is_dynamic and expected_dist <= MATCH_THRESHOLD:
        return {"success": True,  "method": "phash_match",
                "actual_screen": expected_screen, "distance": expected_dist}

    if not is_dynamic and expected_dist > MISMATCH_THRESHOLD and best_other_dist <= MATCH_THRESHOLD:
        return {"success": False, "method": "phash_mismatch",
                "actual_screen": best_other_sid or "", "distance": expected_dist}

    return {"success": None,  "method": "phash_inconclusive",
            "actual_screen": "", "distance": expected_dist}


# ── Node 2 helpers ────────────────────────────────────────────────────────────

# ── Anchored value read (element-specific, deterministic, zero-LLM) ───────────

def _find_element(app_map: dict, screen_id: str, element_id: str) -> dict | None:
    """Return the app_map element dict for element_id on screen_id, or None."""
    if not element_id or not screen_id:
        return None
    screen = (app_map.get("screens") or {}).get(screen_id) or {}
    for el in screen.get("elements") or []:
        if el.get("id") == element_id:
            return el
    return None


def _norm_value(s: str) -> str:
    """Normalise an on-screen value for comparison: drop whitespace, thousands separators, and
    currency symbols; lowercase. Keeps the decimal point (756.67 ≠ 75667). So '5000', '5,000',
    '$5,000', and '5 000' all normalise to the same token — a pure number-FORMAT difference."""
    return re.sub(r"[\s,$€£₹]", "", s or "").lower()


def _value_matches(expected: str, observed: str) -> bool:
    """Loose equality for on-screen values: ignore whitespace/commas/currency/case and allow the
    expected token to be contained in a longer field text (e.g. 'Total: $856.67')."""
    e, o = _norm_value(expected), _norm_value(observed)
    if not e or not o:
        return False
    return e in o or o in e


def _is_format_only_diff(expected: str, observed: str) -> bool:
    """True when two values are EQUAL after stripping formatting but differ raw — e.g. expected
    '5000' vs an on-screen '5,000'/'$5,000'. The underlying value is identical; only the number
    format differs, so the step PASSES but flags a human review. Uses normalized EQUALITY (not
    containment) so a genuinely different value like '5000' vs '50000' is NOT treated as a format
    difference (that stays a plain content match/mismatch, never a false 'format-only' note)."""
    if (expected or "").strip() == (observed or "").strip():
        return False   # identical raw → nothing to review
    e, o = _norm_value(expected), _norm_value(observed)
    if not e or not o:
        return False
    if e == o:
        return True    # same value, separators/currency stripped (5000 vs 5,000)
    try:               # numeric equality catches trailing zeros: 3000 == 3000.00
        return abs(float(e) - float(o)) < 1e-9
    except (ValueError, TypeError):
        return False


def _ocr_element_text(image_path: str, el: dict) -> str | None:
    """OCR the element's bbox (small padding) from the camera frame. Returns the text,
    or None when pytesseract is unavailable or the image can't be read."""
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return None
    from pathlib import Path
    if not image_path or not Path(image_path).exists():
        return None
    try:
        img = Image.open(image_path)
    except Exception:
        return None
    bbox = el.get("bbox")
    if bbox and len(bbox) == 4:
        x1, y1, x2, y2 = bbox
        crop = img.crop((max(0, int(x1) - 8), max(0, int(y1) - 6),
                         min(img.width, int(x2) + 8), min(img.height, int(y2) + 6)))
    else:
        cx, cy = el.get("center") or [0, 0]
        crop = img.crop((max(0, int(cx) - 160), max(0, int(cy) - 30),
                         min(img.width, int(cx) + 160), min(img.height, int(cy) + 30)))
    try:
        txt = pytesseract.image_to_string(crop, config="--psm 7").strip()
        return txt or None
    except Exception:
        return None


def _anchored_read(value_element_id: str, expected_screen: str, app_map: dict,
                   backend: str, image_path: str) -> str | None:
    """Read the live text of the specific app_map element the step asserts a value for.

    Returns the observed string, or None when there is no usable anchor (unknown
    element, no test id / center for playwright, OCR unavailable) — in which case the
    caller falls back to the generic local-first text check.
    """
    el = _find_element(app_map, expected_screen, value_element_id)
    if not el:
        return None
    if backend == "playwright":
        testid = el.get("testid")
        if testid:
            txt = robot.query_element_text(f'[data-testid="{testid}"]')
            if txt and txt.strip():
                return txt.strip()
        center = el.get("center")
        if center and len(center) == 2:
            txt = robot.text_at_point(int(center[0]), int(center[1]))
            if txt and txt.strip():
                return txt.strip()
        return None
    if backend == "demo":
        return None   # demo has no live screen — use the existing always-true path
    # real robot — OCR the element's region from the camera frame
    return _ocr_element_text(image_path, el)


def _check_text(
    expected_text: str,
    backend: str,
    image_path: str,
    description: str,
    app_map: dict | None = None,
    expected_screen: str = "",
) -> bool | None:
    """
    Check for expected_text on the current screen.

    playwright  → DOM text query (zero LLM)
    demo        → always True
    real robot  → region OCR via pytesseract (zero LLM), then Claude Opus fallback

    Returns True / False / None  (None = inconclusive → triggers Node 3).
    """
    if backend == "playwright":
        if robot.text_is_present(expected_text, exact=True):
            return True
        if robot.text_is_present(expected_text, exact=False):
            return True
        normalised = re.sub(r"[,]", "", expected_text)
        if normalised != expected_text and robot.text_is_present(normalised, exact=False):
            return True
        return False

    elif backend == "demo":
        return True

    else:
        # Real robot: region OCR first (zero LLM), then Claude Opus if needed
        ocr = _region_ocr_text_check(image_path, expected_text, app_map or {}, expected_screen)
        if ocr is not None:
            return ocr
        return _claude_vision_text_check(image_path, expected_text, description)


# ── Node 2B: Region OCR ───────────────────────────────────────────────────────

def _region_ocr_text_check(
    image_path: str,
    expected_text: str,
    app_map: dict,
    expected_screen: str,
) -> bool | None:
    """
    Crop a 320×60 px band around each element's center and run pytesseract OCR.
    Falls back to a full-image scan if no element crop matches.

    Returns True / False / None.
    None means pytesseract is not installed or extracted too little text —
    the caller falls back to Claude Opus.
    """
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return None

    from pathlib import Path
    if not image_path or not Path(image_path).exists():
        return None

    try:
        img = Image.open(image_path)
    except Exception:
        return None

    norm_exp = re.sub(r"[\s,]", "", expected_text).lower()

    def _matches(raw: str) -> bool:
        return (
            norm_exp in re.sub(r"[\s,]", "", raw).lower()
            or expected_text.lower() in raw.lower()
        )

    # Try element-level crops first (narrow band around each element centre)
    screen_data = (app_map.get("screens") or {}).get(expected_screen, {})
    for el in screen_data.get("elements") or []:
        cx, cy = el.get("center") or [0, 0]
        if cx == 0 and cy == 0:
            continue
        band = img.crop((
            max(0, int(cx) - 160), max(0, int(cy) - 30),
            min(img.width, int(cx) + 160), min(img.height, int(cy) + 30),
        ))
        try:
            text = pytesseract.image_to_string(band, config="--psm 7").strip()
            if _matches(text):
                return True
        except Exception:
            pass

    # Full-image fallback
    try:
        full_text = pytesseract.image_to_string(img).strip()
    except Exception:
        return None

    if _matches(full_text):
        return True
    if len(full_text.strip()) < 20:
        return None  # too little text extracted — OCR likely failed
    return False


# ── Node 3: Claude Vision Fallback ────────────────────────────────────────────

def _claude_vision_text_check(image_path: str, expected_text: str, description: str) -> bool | None:
    """Ask Claude Opus whether expected_text is visible in the screenshot."""
    import base64, json
    from langchain_core.messages import HumanMessage
    from vision_agent.llm import get_fast_llm
    from vision_agent.storage import get_storage

    try:
        image_bytes = get_storage().load(image_path)
        b64 = base64.standard_b64encode(image_bytes).decode()
        prompt = (
            f"Examine this screenshot carefully.\n"
            f"Is the text '{expected_text}' visible anywhere on screen?\n"
            f"Context: {description}\n\n"
            f"Return ONLY valid JSON:\n"
            f'{{ "found": true/false, "observation": "<one sentence>" }}'
        )
        llm = get_fast_llm()
        msg = HumanMessage(content=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64},
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt},
        ])
        raw = llm.invoke([msg]).content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        v = json.loads(raw)
        return bool(v.get("found", False))
    except Exception as e:
        print(f"  [VALIDATE] Claude text check error: {e}")
        return None


def _claude_extract_value(image_path: str, expected_text: str, description: str) -> str:
    """On a text mismatch, ask Claude what the ACTUAL corresponding value on screen is.

    Returns the observed value (e.g. the real order total "$756.67") so the failure message can
    say "expected '$856.67' but the screen shows '$756.67'" instead of a bare "not found".
    Best-effort — returns "" if it cannot be determined.
    """
    import base64, json
    from langchain_core.messages import HumanMessage
    from vision_agent.llm import get_fast_llm
    from vision_agent.storage import get_storage
    try:
        b64 = base64.standard_b64encode(get_storage().load(image_path)).decode()
        prompt = (
            f"A test expected to see the value '{expected_text}' on this screen.\n"
            f"Context: {description}\n\n"
            f"Look at the screenshot and report the ACTUAL corresponding value shown (the total "
            f"amount, count, id, or status that '{expected_text}' was meant to match). If the "
            f"expected value is genuinely absent, report whatever IS shown in its place.\n"
            f'Return ONLY valid JSON: {{ "observed": "<actual value on screen, or empty>" }}'
        )
        llm = get_fast_llm()
        msg = HumanMessage(content=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64},
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt},
        ])
        raw = llm.invoke([msg]).content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        return str(json.loads(raw).get("observed", "")).strip()
    except Exception as e:
        print(f"  [VALIDATE] Claude value extract error: {e}")
        return ""


def _claude_vision_validate(image_path: str, description: str, screen_before: str) -> dict:
    """Node 3 fallback — full VALIDATE_STEP call via Claude Opus."""
    import base64, json
    from langchain_core.messages import HumanMessage
    from vision_agent.llm import get_fast_llm
    from vision_agent.prompts import VALIDATE_STEP
    from vision_agent.storage import get_storage

    if not image_path:
        return {
            "success": False,
            "observation": "No screenshot available for Claude vision fallback",
            "new_screen_id": "",
            "recovery_hint": None,
        }

    try:
        image_bytes = get_storage().load(image_path)
        b64 = base64.standard_b64encode(image_bytes).decode()
        prompt = VALIDATE_STEP.format(
            action_description=description,
            action_type="verify",
            screen_before=screen_before or "unknown",
        )
        llm = get_fast_llm()
        msg = HumanMessage(content=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64},
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt},
        ])
        raw = llm.invoke([msg]).content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        return {
            "success": False,
            "observation": f"Claude vision fallback error: {e}",
            "new_screen_id": "",
            "recovery_hint": None,
        }
