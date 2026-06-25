"""
analyze_screen node — pure Claude vision pipeline.

Two-pass strategy:
  Pass 1: send the screenshot to Claude; receive screen_id, description, and
          every interactive element with normalized coordinates + per-element
          confidence score.
  Pass 2: for any element whose confidence is below the configured threshold,
          send the same screenshot back with the proposed center and ask Claude
          to confirm or correct it.  This self-correction loop replaces all
          previous OpenCV edge-detection and gradient-scan coordinate heuristics.

No OpenCV, no numpy.  Works on any UI color scheme or layout out of the box.
"""
import base64
import io
import json
from PIL import Image
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState, ScreenAnalysis
from vision_agent.prompts import ANALYZE_SCREEN, CORRECT_ELEMENT_COORD
from vision_agent.llm import get_llm
from vision_agent.storage import get_storage
from vision_agent.config import settings


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
            # Close any open array and object so the parser can succeed
            for suffix in ("]}", "]}}", "}]}"):
                try:
                    return json.loads(text + suffix)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        # Last resort: return a minimal valid structure
        print("  [WARN] Could not recover — returning empty screen analysis")
        return {"screen_id": "unknown", "description": "parse error", "elements": []}


def _norm_to_px(vals: list[float], img_w: int, img_h: int) -> list[int]:
    """Convert normalized 0.0–1.0 values to pixel coordinates."""
    return [int(v * (img_w if i % 2 == 0 else img_h)) for i, v in enumerate(vals)]


def analyze_screen(state: VisionAgentState) -> dict:
    image_bytes = get_storage().load(state["image_path"])
    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size
    b64 = base64.standard_b64encode(image_bytes).decode()

    llm = get_llm()

    def _image_msg(text: str) -> HumanMessage:
        return HumanMessage(content=[
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": text},
        ])

    # ── Pass 1: full screen analysis ──────────────────────────────────────────
    analysis  = _parse_json(llm.invoke([_image_msg(ANALYZE_SCREEN)]).content)
    elements: list[dict] = analysis.get("elements") or []

    # ── Pass 2: self-correction for low-confidence coordinates ────────────────
    threshold = settings.coordinate_confidence_threshold
    low_conf  = [el for el in elements if el.get("confidence", 1.0) < threshold]

    if low_conf:
        print(f"  [ANALYZE] {len(low_conf)} element(s) below confidence {threshold:.2f} — requesting correction")

    for el in low_conf:
        cx, cy = el["center"]
        prompt = CORRECT_ELEMENT_COORD.format(
            element_id=el["id"],
            element_type=el["type"],
            label=el["label"],
            cx=cx,
            cy=cy,
            confidence=el.get("confidence", 0.0),
        )
        try:
            correction = _parse_json(llm.invoke([_image_msg(prompt)]).content)
            old_center  = list(el["center"])
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

    print(f"\n  [SCREEN] {screen['screen_id']} — {screen['description']}")
    print(f"  {'ID':<30} {'TYPE':<10} {'CENTER':<14} CONF")
    for el in screen["elements"]:
        cx, cy = el["center"]
        print(f"  {el['id']:<30} {el['type']:<10} [{cx:4d},{cy:4d}]   {el.get('confidence', 1.0):.2f}")

    return {"screen_analysis": screen, "screen_history": history}
