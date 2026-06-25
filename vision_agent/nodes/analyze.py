"""
analyze_screen node — loads the current screenshot and asks Claude to identify
every UI element with its type, label, description, and approximate location.

Coordinate strategy (two-pass):
  1. OpenCV Canny edge detector finds rectangular elements with precise bounding
     boxes — color-agnostic, works on any UI design.
  2. Claude provides semantic labels plus normalized coordinate estimates.
     Normalized coords are converted to pixels; they serve as-is for text
     elements (links, labels) that edge detection cannot isolate.
  3. Each snappable element (input, button, dropdown, stepper) from Claude is
     matched to the nearest OpenCV-detected rect by vertical position.
     Only rects narrower than 45% of image width are eligible for snapping —
     this prevents full-width navigation bars from stealing the match.
"""
import base64
import io
import json
from PIL import Image
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState, ScreenAnalysis
from vision_agent.prompts import ANALYZE_SCREEN
from vision_agent.llm import get_llm
from vision_agent.storage import get_storage
from vision_agent.vision.detector import detect_interactive_rects


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    return json.loads(text)


def _to_pixels(norm: list[float], w: int, h: int) -> list[int]:
    """Convert normalized 0.0–1.0 values to pixel coordinates."""
    result = []
    for i, v in enumerate(norm):
        dim = w if i % 2 == 0 else h
        result.append(int(v * dim))
    return result


# Element types that are physically tappable — matched to OpenCV-detected rects
_SNAPPABLE = {"input", "button", "dropdown", "stepper"}

# Rects wider than this fraction of the image are likely navigation bars or
# section headers, not individual interactive controls
_MAX_SNAP_WIDTH_FRAC = 0.45


def _snap_to_opencv(elements: list[dict], detected: list[dict], img_w: int) -> None:
    """
    Match each snappable Claude element to the nearest OpenCV-detected rect using
    2D Euclidean distance — one-to-one (each rect used at most once).

    Using 2D distance correctly handles screens with multiple buttons on the same
    row (e.g., three 'Add to Cart' buttons side by side): pure y-distance would
    give ties, but x-distance breaks them correctly.

    Only considers rects whose width ≤ 45% of image width to prevent full-width
    navigation bars from being matched to individual interactive controls.

    Links, labels, and images keep Claude's normalized coordinates unchanged.
    """
    max_snap_w = int(img_w * _MAX_SNAP_WIDTH_FRAC)
    eligible = [r for r in detected if (r["bbox"][2] - r["bbox"][0]) <= max_snap_w]

    snap_els = [el for el in elements if el["type"] in _SNAPPABLE]

    used: set[int] = set()
    for el in snap_els:
        ex, ey = el["center"]
        best_idx, best_dist = -1, float("inf")
        for i, rect in enumerate(eligible):
            if i in used:
                continue
            rx, ry = rect["center"]
            d = ((rx - ex) ** 2 + (ry - ey) ** 2) ** 0.5
            if d < best_dist:
                best_dist, best_idx = d, i
        if best_idx >= 0:
            used.add(best_idx)
            el["bbox"]   = eligible[best_idx]["bbox"]
            el["center"] = eligible[best_idx]["center"]


def analyze_screen(state: VisionAgentState) -> dict:
    image_bytes = get_storage().load(state["image_path"])

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_w, img_h = img.size

    # ── Step 1: edge-based rectangle detection (color-agnostic) ──────────
    detected = detect_interactive_rects(img)

    # ── Step 2: semantic labeling via Claude ──────────────────────────────
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = ANALYZE_SCREEN.format(
        width=img_w, height=img_h,
        cx_example=0.5, cy_example=0.5,
    )
    llm = get_llm()
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": prompt},
    ])
    response = llm.invoke([msg])
    analysis: ScreenAnalysis = _parse_json(response.content)

    # ── Step 3: convert normalized → pixels ──────────────────────────────
    for el in analysis["elements"]:
        el["bbox"]   = _to_pixels(el["bbox"],   img_w, img_h)
        el["center"] = _to_pixels(el["center"], img_w, img_h)

    # ── Step 4: snap snappable elements to OpenCV-precise coordinates ─────
    _snap_to_opencv(analysis["elements"], detected, img_w)

    history = list(state.get("screen_history") or [])
    if not history or history[-1] != analysis["screen_id"]:
        history.append(analysis["screen_id"])

    return {"screen_analysis": analysis, "screen_history": history}
