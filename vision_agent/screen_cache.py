"""
Screen coordinate cache — hash-based lookup into the App Explorer map.

Instead of calling analyze_screen (Claude Vision, ~2.5s) for every screenshot,
this module first checks if the current screen matches a known screen from the
App Explorer map using a perceptual hash. On a cache hit, returns pre-computed
element coordinates directly and skips the LLM entirely. On a miss, falls
through to the normal Claude Vision call.

Hash algorithm: average hash (aHash) — resize to 16×16 grayscale, threshold
at mean pixel value. Tolerates minor rendering differences such as anti-aliasing
and cursor blink. Two screenshots of the same static screen always match;
a screenshot after navigation (new content loaded) does not.

Dynamic screens (cart, order history, search results) are tagged is_dynamic=True
during App Explorer and are NEVER served from cache — they always go to Claude
for fresh element extraction because their content changes per session.
"""
import io
import json
from pathlib import Path
from PIL import Image


def compute_hash(image_bytes: bytes) -> str:
    """Compute an average hash (aHash) of the image as a 64-char hex string."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((16, 16), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return f"{int(bits, 2):064x}"


def _hamming(a: str, b: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(a, b))


def load_app_map(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def lookup_screen(image_bytes: bytes, app_map: dict | None, threshold: int = 8) -> dict | None:
    """
    Return a pre-built ScreenAnalysis dict if the image hash matches a known
    screen in app_map within `threshold` hamming distance.

    Returns None when:
      - app_map is None (Explorer hasn't run yet)
      - no screen hash is close enough (unknown screen / first visit)
      - the matched screen is tagged is_dynamic=True (fresh analysis required)
    """
    if not app_map:
        return None

    screens = app_map.get("screens") or {}
    if not screens:
        return None

    current_hash = compute_hash(image_bytes)

    best_screen_id = None
    best_distance = threshold + 1

    for screen_id, screen_data in screens.items():
        stored_hash = screen_data.get("screen_hash")
        if not stored_hash:
            continue
        dist = _hamming(current_hash, stored_hash)
        if dist < best_distance:
            best_distance = dist
            best_screen_id = screen_id

    if best_screen_id is None:
        return None

    screen_data = screens[best_screen_id]

    if screen_data.get("is_dynamic"):
        print(f"  [CACHE] '{best_screen_id}' matched (d={best_distance}) but is_dynamic — re-analyzing")
        return None

    print(f"  [CACHE HIT] '{best_screen_id}' (hamming={best_distance}) — skipping LLM analyze_screen")
    return {
        "screen_id": best_screen_id,
        "description": screen_data.get("description", ""),
        "elements":    screen_data.get("elements", []),
    }
