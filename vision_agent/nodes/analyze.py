"""
analyze_screen node — loads the current screenshot and asks Claude to identify
every UI element with its type, label, description, and approximate location.

Coordinate strategy (three-pass):
  1. OpenCV Canny edge detector finds rectangular elements (inputs, buttons)
     with precise bounding boxes — color-agnostic, works on any UI design.
  2. Claude provides semantic labels plus normalized coordinate estimates.
     Normalized coords are converted to pixels and used as anchors for steps 3/4.
  3. Each snappable element (input, button, dropdown, stepper) is matched to
     the nearest eligible OpenCV-detected rect using 2D Euclidean distance.
  4. Link/text elements are corrected by finding actual text-line positions
     from horizontal gradient analysis in the zone below the last detected rect.
"""
import base64
import io
import json
import numpy as np
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

    2D distance correctly handles screens with multiple buttons on the same row
    (e.g. three "Add to Cart" buttons side by side).  Pure y-distance would give
    ties; x-distance breaks them correctly.

    Only considers rects whose width ≤ 45% of image width to prevent full-width
    navigation bars from stealing the match.
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


# ── Text-link coordinate correction ──────────────────────────────────────────

def _find_text_lines_below(gray: np.ndarray,
                            search_from: int,
                            band_h: int = 12,
                            search_range: int = 200) -> list[int]:
    """
    Find y-coordinates of text lines by scanning for rows with high horizontal
    gradient (character edges) in the band [search_from, search_from+search_range].

    Returns a list of peak y-values, sorted top-to-bottom.
    """
    h = gray.shape[0]
    y1, y2 = min(search_from, h - 1), min(search_from + search_range, h)
    if y2 <= y1:
        return []

    zone = gray[y1:y2].astype(np.float32)
    # Per-row mean horizontal gradient — text rows spike relative to blank rows
    hgrad = np.abs(np.diff(zone, axis=1)).mean(axis=1)
    threshold = max(2.0, float(hgrad.mean() + hgrad.std()))

    peaks, in_peak, seg_start = [], False, 0
    for i, v in enumerate(hgrad):
        if v > threshold and not in_peak:
            in_peak, seg_start = True, i
        elif v <= threshold and in_peak:
            in_peak = False
            peaks.append(int(np.argmax(hgrad[seg_start:i])) + seg_start + y1)
    if in_peak:
        peaks.append(int(np.argmax(hgrad[seg_start:])) + seg_start + y1)
    return peaks


def _find_text_clusters(gray: np.ndarray,
                         text_y: int,
                         x_start: int,
                         x_end: int,
                         band_h: int = 12,
                         min_cluster_w: int = 15,
                         merge_gap: int = 20) -> list[tuple[int, int]]:
    """
    Find x-extents of text clusters at text_y by:
      1. Taking the MAX brightness per column over a ±band_h/2 row window.
         (catches anti-aliased characters regardless of which exact row they peak on)
      2. Using an adaptive threshold: bg_gray + 0.25*(255 - bg_gray)
         so the threshold scales with whatever background color the UI uses.
      3. Merging clusters whose gap ≤ merge_gap pixels (handles multi-word links
         like "Sign up" = two words separated by just a few pixels).

    Returns [(x1, x2), ...] for each merged cluster, sorted left-to-right.
    """
    h, w = gray.shape
    y1 = max(0, text_y - band_h // 2)
    y2 = min(h, text_y + band_h // 2 + 1)
    xs, xe = max(0, x_start), min(w, x_end)
    if y2 <= y1 or xe <= xs:
        return []

    band = gray[y1:y2, xs:xe].astype(np.float32)   # (rows, cols)
    col_max = band.max(axis=0)                       # brightest pixel per column

    # Adaptive threshold relative to the background brightness.
    # Use the global band median (all pixels), NOT the col_max median:
    # even when >50% of columns contain a text pixel, most individual pixels
    # are still background, so the global median tracks the true background gray.
    bg_gray = float(np.median(band))
    threshold = bg_gray + 0.25 * (255.0 - bg_gray)

    text_mask = col_max > threshold

    # Raw clusters
    raw: list[tuple[int, int]] = []
    in_c, c_start = False, 0
    for i, t in enumerate(text_mask):
        if t and not in_c:
            in_c, c_start = True, i
        elif not t and in_c:
            in_c = False
            if i - c_start >= min_cluster_w:
                raw.append((c_start + xs, i + xs))
    if in_c and len(text_mask) - c_start >= min_cluster_w:
        raw.append((c_start + xs, len(text_mask) + xs))

    if not raw:
        return []

    # Merge clusters separated by ≤ merge_gap pixels (e.g. "Sign" + " " + "up")
    merged: list[tuple[int, int]] = [raw[0]]
    for a, b in raw[1:]:
        prev_x2 = merged[-1][1]
        if a - prev_x2 <= merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    return merged


def _correct_link_coords(elements: list[dict],
                          detected: list[dict],
                          gray: np.ndarray,
                          img_w: int) -> None:
    """
    Fix y- and x-coordinates for link/text elements.

    Because links are plain text without rectangular borders, OpenCV Canny
    misses them. Claude's normalized estimates are often off by 100+ px.

    Strategy:
      - Scan the zone just below the last detected interactive element for
        horizontal-gradient peaks → finds actual text-line y-coordinates.
      - Within each text line, detect columns of bright pixels to locate each
        word cluster → gives accurate x-centers.
      - Match Claude's link elements (sorted by x) to detected clusters (sorted
        by x) using nearest-neighbor assignment.
    """
    link_els = [el for el in elements if el["type"] == "link"]
    if not link_els or not detected:
        return

    # Use only non-wide rects (same 45% width filter as _snap_to_opencv) to
    # compute x bounds — the header rect (≈47% wide) would otherwise extend the
    # search zone into header content and generate spurious text clusters.
    max_snap_w = int(img_w * _MAX_SNAP_WIDTH_FRAC)
    eligible = [r for r in detected if (r["bbox"][2] - r["bbox"][0]) <= max_snap_w]
    if not eligible:
        return

    last_bottom = max(r["bbox"][3] for r in eligible)
    text_lines = _find_text_lines_below(gray, last_bottom + 3)
    if not text_lines:
        return

    # Group links by their nearest detected text line
    from collections import defaultdict
    line_groups: dict[int, list[dict]] = defaultdict(list)
    for el in link_els:
        nearest = min(text_lines, key=lambda y: abs(y - el["center"][1]))
        line_groups[nearest].append(el)

    # x search range: left/right edge of the form's interactive elements
    x_start = min(r["bbox"][0] for r in eligible)
    x_end   = max(r["bbox"][2] for r in eligible)

    for line_y, els_on_line in line_groups.items():
        # Correct y for all elements on this line
        for el in els_on_line:
            half_h = max(8, (el["bbox"][3] - el["bbox"][1]) // 2)
            el["center"][1] = line_y
            el["bbox"][1]   = line_y - half_h
            el["bbox"][3]   = line_y + half_h

        # Find text clusters at this line → correct x
        clusters = _find_text_clusters(gray, line_y, x_start, x_end)
        if not clusters:
            continue

        # Sort both by x and pair positionally
        sorted_els = sorted(els_on_line, key=lambda e: e["center"][0])
        for el, (cx1, cx2) in zip(sorted_els, clusters):
            cx = (cx1 + cx2) // 2
            half_w = max(20, (cx2 - cx1) // 2)
            el["center"][0] = cx
            el["bbox"][0]   = cx1
            el["bbox"][2]   = cx2


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

    # ── Step 4: snap inputs/buttons to OpenCV-precise coordinates ─────────
    _snap_to_opencv(analysis["elements"], detected, img_w)

    # ── Step 5: correct link y/x to actual text-line positions ───────────
    gray = np.array(img.convert("L"))
    _correct_link_coords(analysis["elements"], detected, gray, img_w)

    history = list(state.get("screen_history") or [])
    if not history or history[-1] != analysis["screen_id"]:
        history.append(analysis["screen_id"])

    return {"screen_analysis": analysis, "screen_history": history}
