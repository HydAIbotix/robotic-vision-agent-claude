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
from vision_agent.llm import get_llm
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
    """Convert normalized 0.0–1.0 values to pixel coordinates."""
    return [int(v * (img_w if i % 2 == 0 else img_h)) for i, v in enumerate(vals)]


def _log_screen(screen: dict) -> None:
    print(f"\n  [SCREEN] {screen['screen_id']} — {screen['description']}")
    print(f"  {'ID':<30} {'TYPE':<10} {'CENTER':<14} CONF")
    for el in screen["elements"]:
        cx, cy = el["center"]
        print(f"  {el['id']:<30} {el['type']:<10} [{cx:4d},{cy:4d}]   {el.get('confidence', 1.0):.2f}")


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
    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size
    b64 = base64.standard_b64encode(image_bytes).decode()

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
            "media_type": "image/png",
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
            el["center"]     = correction["center"]
            el["confidence"] = correction.get("confidence", el["confidence"])
            tag = "CONFIRMED" if correction.get("confirmed") else "CORRECTED"
            print(f"    [{tag}] {el['id']:30s}  {old_center} -> {el['center']}  ({correction.get('reason', '')})")
        except Exception as exc:
            print(f"    [WARN]  Correction skipped for {el['id']}: {exc}")

    # ── Convert normalized → pixels ───────────────────────────────────────────
    for el in elements:
        el["bbox"]   = _norm_to_px(el.get("bbox",   [0.0, 0.0, 0.0, 0.0]), img_w, img_h)
        el["center"] = _norm_to_px(el.get("center", [0.5, 0.5]),            img_w, img_h)

    # ── Build and log ScreenAnalysis ──────────────────────────────────────────
    screen: ScreenAnalysis = {
        "screen_id":   analysis.get("screen_id",   "unknown"),
        "description": analysis.get("description", ""),
        "elements":    elements,
    }

    history = list(state.get("screen_history") or [])
    if not history or history[-1] != screen["screen_id"]:
        history.append(screen["screen_id"])

    _log_screen(screen)
    return {"screen_analysis": screen, "screen_history": history}
