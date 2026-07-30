"""
analyze_screen node — App Explorer coordinate cache + Claude vision pipeline.

Fast path (App Explorer map present, static screen):
  Compute a perceptual hash of the screenshot and look it up in the App Explorer
  map. On a cache hit, return pre-computed element coordinates directly — zero
  LLM calls (~50 ms vs ~2.5 s). Dynamic screens (cart, orders, search results)
  are tagged is_dynamic=True in the map and always fall through to Claude.

Slow path (cache miss or dynamic screen — full Claude Vision):
  Pass 1: send the screenshot to Claude; receive screen_id, description, and
          every interactive element with normalized coordinates + per-element
          confidence score.
  Pass 2: for any element whose confidence is below the configured threshold,
          send the same screenshot back with the proposed center and ask Claude
          to confirm or correct it.  This self-correction loop replaces all
          previous OpenCV edge-detection and gradient-scan coordinate heuristics.

No OpenCV, no numpy.  Works on any UI color scheme or layout out of the box.

Prompt caching strategy (Anthropic, 5-min TTL, 10% cost on hit):
  system_msg  — ANALYZE_SCREEN instructions are static across every call in a
                run; cached on first call, ~350 tokens at 10% on subsequent ones.
  image_block — same screenshot bytes reused in Pass 2 self-correction;
                cached on Pass 1, ~1600 tokens at 10% for each Pass 2 call.
"""
import base64
import io
import json
from PIL import Image
from langchain_core.messages import HumanMessage, SystemMessage
from vision_agent.state import VisionAgentState, ScreenAnalysis
from vision_agent.prompts import ANALYZE_SCREEN, CORRECT_ELEMENT_COORD
from vision_agent.llm import get_llm, detect_image_media_type
from vision_agent.storage import get_storage
from vision_agent.config import settings
from vision_agent.screen_cache import load_app_map, lookup_screen


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Response was truncated (hit max_tokens).  Try to salvage whatever
        # complete elements were returned before the cutoff.
        print("  [WARN] JSON truncated — recovering partial response")
        try:
            for suffix in ("]}", "]}}", "}]}"):
                try:
                    return json.loads(text + suffix)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        print("  [WARN] Could not recover — returning empty screen analysis")
        return {"screen_id": "unknown", "description": "parse error", "elements": []}


def _norm_to_px(vals: list[float], img_w: int, img_h: int) -> list[int]:
    """Convert model coordinates to pixels, tolerant of MIXED scales.

    Vision models are asked for normalized 0.0–1.0 coordinates but, in practice, return raw
    pixels (0–img) for some elements and normalized values for others *within the same
    response*. Blindly multiplying a pixel value by the image dimension blows it up ~1000×
    and pushes the element off-canvas. So detect the scale per value:

      v <= 1.5            → normalized fraction   → v * dim
      1.5 < v <= dim*1.1  → already in pixels      → v  (clamped to the image)
      v  > dim*1.1        → 0–1000 scale / junk    → (v / 1000) * dim

    Normalized inputs (the common case) are unchanged; only out-of-range values are rescued.
    Generic: no per-app or per-element logic.
    """
    out: list[int] = []
    for i, v in enumerate(vals):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            fv = 0.0
        dim = img_w if i % 2 == 0 else img_h
        if fv <= 1.5:
            px = fv * dim
        elif fv <= dim * 1.1:
            px = fv
        else:
            px = (fv / 1000.0) * dim
        out.append(int(max(0.0, min(px, float(dim)))))
    return out


def _center2(vals) -> list:
    """Coerce a model-returned `center` to EXACTLY two coordinates [x, y].

    Claude's vision occasionally returns a `center` with the wrong arity — a 4-value bbox
    (`[x1,y1,x2,y2]`), a 3-value point, or a stray extra number. `_norm_to_px` preserves the length,
    so a wrong-length center then crashes downstream unpacking (`cx, cy = el["center"]`) and takes the
    whole exploration down. This normalizes at the source: a 4-value bbox becomes its midpoint, any
    other odd length is truncated/padded, and garbage falls back to the frame centre [0.5, 0.5].
    Works in normalized OR pixel space (it's a pure length coercion).
    """
    try:
        v = [float(x) for x in vals]
    except (TypeError, ValueError):
        return [0.5, 0.5]
    if len(v) == 2:
        return v
    if len(v) == 4:                       # looks like a bbox → use its midpoint
        return [(v[0] + v[2]) / 2.0, (v[1] + v[3]) / 2.0]
    if len(v) >= 2:
        return v[:2]
    return [0.5, 0.5]


def _log_screen(screen: dict) -> None:
    print(f"\n  [SCREEN] {screen['screen_id']} — {screen['description']}")
    print(f"  {'ID':<30} {'TYPE':<10} {'CENTER':<14} CONF")
    for el in screen["elements"]:
        cx, cy = (int(v) for v in _center2(el.get("center", [0, 0])))
        print(f"  {el['id']:<30} {el['type']:<10} [{cx:4d},{cy:4d}]   {el.get('confidence', 1.0):.2f}")


def analyze_image_elements(image_bytes: bytes) -> ScreenAnalysis:
    """Run the full Claude-Vision element analysis on raw image bytes — the SLOW PATH of
    analyze_screen, extracted so it can be reused WITHOUT any graph state or the app_map cache.

    Two passes (identical to the App Explorer): Pass 1 extracts screen_id + description + every
    interactive element with normalized coords and a confidence; Pass 2 asks Claude to confirm/
    correct any element below the confidence threshold. Coordinates are converted to pixels via the
    scale-tolerant _norm_to_px. Returns {screen_id, description, elements:[…]} with pixel bbox/center.

    Used by (a) analyze_screen's slow path below, and (b) the Camera Vision Test diagnostic endpoint,
    so the page tests the EXACT vision code the explorer uses — no reimplementation, no drift."""
    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size
    b64 = base64.standard_b64encode(image_bytes).decode()
    media_type = detect_image_media_type(image_bytes)

    llm = get_llm()

    # Static instructions — identical on every analyze_screen call in a run.
    # cache_control writes to Anthropic's server cache on the first call;
    # all subsequent calls within 5 minutes pay only 10% for these tokens.
    system_msg = SystemMessage(content=[{
        "type": "text",
        "text": ANALYZE_SCREEN,
        "cache_control": {"type": "ephemeral"},
    }])

    # The image block is shared by Pass 1 and every Pass 2 correction call.
    # cache_control on the image means Pass 1 writes ~1600 tokens to cache;
    # each Pass 2 call for the same screenshot pays 10% for those tokens.
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": b64,
        },
        "cache_control": {"type": "ephemeral"},
    }

    # ── Pass 1: full screen analysis ──────────────────────────────────────────
    analysis = _parse_json(llm.invoke([
        system_msg,
        HumanMessage(content=[image_block]),
    ]).content)
    elements: list[dict] = analysis.get("elements") or []
    # Normalize every center to exactly [x, y] up front so a malformed model response (bbox-as-center,
    # 3-value point) can't crash the correction loop, the log, or any downstream coordinate unpacking.
    for el in elements:
        if "center" in el:
            el["center"] = _center2(el["center"])

    # ── Pass 2: self-correction for low-confidence coordinates ────────────────
    threshold = settings.coordinate_confidence_threshold
    low_conf  = [el for el in elements if el.get("confidence", 1.0) < threshold]

    if low_conf:
        print(f"  [ANALYZE] {len(low_conf)} element(s) below confidence {threshold:.2f} — requesting correction")

    for el in low_conf:
        cx, cy = el["center"]
        correction_prompt = CORRECT_ELEMENT_COORD.format(
            element_id=el["id"],
            element_type=el["type"],
            label=el["label"],
            cx=cx,
            cy=cy,
            confidence=el.get("confidence", 0.0),
        )
        try:
            correction = _parse_json(llm.invoke([
                system_msg,
                HumanMessage(content=[
                    image_block,
                    {"type": "text", "text": correction_prompt},
                ]),
            ]).content)
            old_center       = list(el["center"])
            el["center"]     = _center2(correction["center"])
            el["confidence"] = correction.get("confidence", el["confidence"])
            tag = "CONFIRMED" if correction.get("confirmed") else "CORRECTED"
            print(f"    [{tag}] {el['id']:30s}  {old_center} -> {el['center']}  ({correction.get('reason', '')})")
        except Exception as exc:
            print(f"    [WARN]  Correction skipped for {el['id']}: {exc}")

    # ── Convert normalized → pixels ───────────────────────────────────────────
    for el in elements:
        el["bbox"]   = _norm_to_px(el.get("bbox",   [0.0, 0.0, 0.0, 0.0]), img_w, img_h)
        el["center"] = _norm_to_px(el.get("center", [0.5, 0.5]),            img_w, img_h)

    screen: ScreenAnalysis = {
        "screen_id":   analysis.get("screen_id",   "unknown"),
        "description": analysis.get("description", ""),
        "elements":    elements,
    }
    return screen


def analyze_screen(state: VisionAgentState) -> dict:
    image_bytes = get_storage().load(state["image_path"])

    # ── Fast path: App Explorer coordinate cache ──────────────────────────────
    # If the App Explorer has already mapped this app, look up the screen by
    # perceptual hash. Static screens (login, payment, success) return cached
    # element coordinates with zero LLM calls. Dynamic screens fall through.
    if settings.use_app_map_cache:
        app_map = load_app_map(settings.app_map_path)
        cached = lookup_screen(image_bytes, app_map)
        if cached:
            history = list(state.get("screen_history") or [])
            if not history or history[-1] != cached["screen_id"]:
                history.append(cached["screen_id"])
            _log_screen(cached)
            return {"screen_analysis": cached, "screen_history": history}

    # ── Slow path: full Claude Vision analysis ────────────────────────────────
    screen = analyze_image_elements(image_bytes)

    history = list(state.get("screen_history") or [])
    if not history or history[-1] != screen["screen_id"]:
        history.append(screen["screen_id"])

    _log_screen(screen)
    return {"screen_analysis": screen, "screen_history": history}
