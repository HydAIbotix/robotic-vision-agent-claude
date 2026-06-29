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


def set_keyboard_map(kmap: dict) -> None:
    """Store virtual keyboard coordinates — used by type_text to tap each key."""
    global _keyboard_map
    _keyboard_map = kmap.get("keys", kmap)


_keyboard_map: dict = {}


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


def type_text(text: str, clear_first: bool = False) -> dict:
    """Type text character-by-character via robot arm keyboard simulator.

    clear_first: when True the real robot arm should first send a "select-all"
    gesture to clear any pre-filled content.  No-op in demo/stub mode.
    """
    # TODO: send keystroke sequence to robot arm; if clear_first, precede with select-all gesture
    print(f"    [ROBOT] type({text!r})" + (" [clear_first]" if clear_first else ""))
    return {"success": True, "text": text}


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
    """Swipe gesture from (x1, y1) to (x2, y2)."""
    # TODO: send swipe command to robot arm
    print(f"    [ROBOT] swipe({x1},{y1}) → ({x2},{y2})")
    return {"success": True}


def scroll_page(x: int, y: int, delta_y: int) -> dict:
    """Scroll gesture — not applicable to a physical kiosk (stub only)."""
    print(f"    [ROBOT] scroll({x},{y}) delta_y={delta_y}")
    return {"success": True, "delta_y": delta_y}


def get_page_scroll_info() -> dict:
    """Return stub scroll info — assume single viewport, no overflow."""
    return {"scrollTop": 0, "scrollLeft": 0, "scrollHeight": 900, "scrollWidth": 1400,
            "viewportHeight": 900, "viewportWidth": 1400}


def get_dom_screen_id() -> str:
    """Not applicable to real robot arm — URL/DOM not accessible via camera."""
    return ""


def get_dom_element_centers() -> list[dict]:
    """Not applicable to real robot arm — DOM not accessible via camera."""
    return []


def navigate_to_screen(screen_id: str) -> bool:
    """Not applicable to real robot arm — sidebar nav must be tapped physically."""
    return False


def move_to_position(x: float, y: float, theta: float) -> dict:
    """Drive the robot base to (x, y) with heading theta (degrees) before interacting with a device."""
    # TODO: send navigation goal to robot base controller
    print(f"    [ROBOT] move_to_position(x={x}, y={y}, θ={theta}°)")
    return {"success": True, "x": x, "y": y, "theta": theta}


def update_explorer_progress(explored: int, total: int, current_action: str = "") -> None:
    """No-op in demo/real modes — progress HUD only renders in playwright mode."""
    pass
