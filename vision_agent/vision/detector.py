"""
Generic UI element detector using OpenCV Canny edge detection.

Finds rectangular interactive elements (buttons, inputs, dropdowns, etc.)
by their BORDERS — which always contrast with the surrounding background,
regardless of what colors the UI uses.

Algorithm:
  1. Adaptive Canny edge detection (threshold derived from image median brightness)
  2. Find all closed contours in the edge map
  3. Keep contours whose bounding rect is the right size for an interactive element
     and that approximate a rectangle (high solidity)
  4. Deduplicate overlapping detections

This works for any color scheme — light themes, dark themes, colored kiosks, etc.
"""
import cv2
import numpy as np
from PIL import Image


def detect_interactive_rects(image: Image.Image) -> list[dict]:
    """
    Return [{"bbox": [x1,y1,x2,y2], "center": [cx,cy], "hint": "interactive"}]
    sorted top-to-bottom, in original image pixel coordinates.
    """
    arr = np.array(image.convert("RGB"))
    h, w = arr.shape[:2]

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    # Mild Gaussian blur: smooths pixel noise without blurring element borders
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)

    # Auto-threshold Canny using the image median brightness.
    # This adapts to each screen's contrast level — no hardcoded values.
    v = float(np.median(blurred))
    lo = max(0,   int(v * 0.33))
    hi = min(255, int(v * 1.33))
    edges = cv2.Canny(blurred, lo, hi)

    # Small dilation closes 1-2px gaps in thin element borders
    kernel = np.ones((2, 2), np.uint8)
    dilated = cv2.dilate(edges, kernel, iterations=1)

    # RETR_LIST: find all closed contours (not just outermost),
    # because interactive elements sit inside the page/card hierarchy
    contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    # Size thresholds for interactive elements
    min_w = int(w * 0.08)   # at least 8% of screen width
    max_w = int(w * 0.60)   # at most 60% of screen width (filters full-width headers)
    min_h = 20
    max_h = int(h * 0.18)   # at most 18% of screen height (filters tall sections)

    candidates: list[dict] = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)

        if not (min_w <= bw <= max_w and min_h <= bh <= max_h):
            continue

        # Solidity: ratio of contour area to its bounding box area.
        # A closed rectangular border has area ≈ bw × bh  (solidity near 1.0).
        # Noise, text glyphs, and irregular blobs have much lower solidity.
        area = cv2.contourArea(cnt)
        if area < (bw * bh) * 0.20:
            continue

        candidates.append({
            "bbox":   [x, y, x + bw, y + bh],
            "center": [x + bw // 2, y + bh // 2],
            "hint":   "interactive",
        })

    result = _deduplicate(candidates)
    result.sort(key=lambda r: r["bbox"][1])
    return result


def _deduplicate(rects: list[dict], iou_thr: float = 0.40) -> list[dict]:
    """
    Remove overlapping rectangles. When two rects overlap above iou_thr,
    keep the larger one (sorted descending by area before dedup).
    """
    by_area = sorted(
        rects,
        key=lambda r: (r["bbox"][2] - r["bbox"][0]) * (r["bbox"][3] - r["bbox"][1]),
        reverse=True,
    )
    out: list[dict] = []
    for r in by_area:
        if not any(_iou(r["bbox"], s["bbox"]) > iou_thr for s in out):
            out.append(r)
    return out


def _iou(a: list[int], b: list[int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (aa + ab - inter)
