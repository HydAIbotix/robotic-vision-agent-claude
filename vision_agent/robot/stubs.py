"""
Robot arm stubs — every function mirrors the real arm's API exactly.
Replace each function BODY when hardware arrives; names and return shapes stay identical.
"""
import time
from pathlib import Path

# ── Demo / test hook ──────────────────────────────────────────────────────────
# Populate this list before running demos or unit tests.
# Each call to capture_screen() pops the next path from it.
# Leave empty in production (real camera will be used instead).
_demo_screens: list[str] = []
_demo_idx: int = 0


def set_demo_screens(paths: list[str]) -> None:
    """Pre-load a sequence of screenshot paths for demo/test runs."""
    global _demo_screens, _demo_idx
    _demo_screens = paths
    _demo_idx = 0


# ── Robot API ─────────────────────────────────────────────────────────────────

def capture_screen(save_path: str) -> dict:
    """Capture kiosk screen via robot arm camera. Returns path to saved image."""
    global _demo_idx
    if _demo_screens:
        path = _demo_screens[_demo_idx % len(_demo_screens)]
        _demo_idx += 1
        return {"success": True, "image_path": path, "timestamp": time.time()}
    # TODO: trigger camera on robot arm and save frame to save_path
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    return {"success": True, "image_path": save_path, "timestamp": time.time()}


def tap(x: int, y: int) -> dict:
    """Physically tap the kiosk touchscreen at pixel coordinate (x, y)."""
    # TODO: send tap command to robot arm controller
    print(f"    [ROBOT] tap({x}, {y})")
    return {"success": True, "x": x, "y": y}


def type_text(text: str) -> dict:
    """Type text character-by-character via robot arm keyboard simulator."""
    # TODO: send keystroke sequence to robot arm
    print(f"    [ROBOT] type({text!r})")
    return {"success": True, "text": text}


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
    """Swipe gesture from (x1, y1) to (x2, y2)."""
    # TODO: send swipe command to robot arm
    print(f"    [ROBOT] swipe({x1},{y1}) → ({x2},{y2})")
    return {"success": True}
