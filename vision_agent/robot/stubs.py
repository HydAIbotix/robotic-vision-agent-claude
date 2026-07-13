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


def verify_current_screen(expected_screen_id: str, app_map: dict, save_path: str = "") -> dict:
    """Demo mode: verification always passes — demo screens follow a pre-scripted path."""
    return {"actual_screen": expected_screen_id, "match": True, "method": "demo"}


def get_dom_element_centers() -> list[dict]:
    """Not applicable to real robot arm — DOM not accessible via camera."""
    return []


def navigate_to_screen(screen_id: str) -> bool:
    """Not applicable to real robot arm — sidebar nav must be tapped physically."""
    return False


def move_to_position(x: float, y: float, theta: float, target=None) -> dict:
    """Drive the robot base before interacting with a device (simulated — no physical base here).

    `target` (destination kiosk_id or "home") mirrors real_robot's target-driven /base/goto; it is
    ignored in this simulated backend but kept in the signature for backend parity."""
    print(f"    [ROBOT] move_to_position(target={target!r}, x={x}, y={y}, θ={theta}°) — simulated")
    return {"success": True, "x": x, "y": y, "theta": theta, "target": target, "simulated": True}


# ── AGV / base + state (no-op fallbacks; the real backend overrides these) ────────
# Present here so playwright / demo runs of AGV-movement test cases don't raise — they
# simulate a healthy idle base. In real-robot mode these are provided by real_robot.py
# and hit the physical /base/* and /*/state APIs instead.

def navigate_to_kiosk(kiosk_id: str, timeout_s=None) -> dict:
    """Simulated AGV move (no physical base in demo/playwright)."""
    print(f"    [ROBOT] navigate_to_kiosk({kiosk_id!r}) — simulated (no base in this backend)")
    return {"success": True, "kiosk_id": kiosk_id, "state": "idle", "simulated": True}


def get_base_state() -> dict:
    """Simulated AGV base state."""
    return {"state": "idle", "simulated": True}


def get_arm_state() -> dict:
    """Simulated arm state."""
    return {"state": "idle", "simulated": True}


def get_events(since_idx: int = 0) -> list:
    """No robot-API telemetry in demo/playwright (no physical robot calls)."""
    return []


def set_event_sink(sink) -> None:
    """No-op for parity with real_robot: demo/playwright have no physical base to stream live AGV
    status from, so there are no real-time ticks to push."""
    pass


def update_explorer_progress(explored: int, total: int, current_action: str = "") -> None:
    """No-op in demo/real modes — progress HUD only renders in playwright mode."""
    pass


def get_aria_snapshot() -> dict:
    """Not applicable to demo/real modes — no DOM available via camera."""
    return {}


def text_is_present(text: str, exact: bool = False) -> bool:
    """Demo mode: always returns True (content validation skipped)."""
    return True


def query_element_text(selector: str) -> str:
    """Not applicable to demo/real modes — no DOM available."""
    return ""


def get_element_bounding_box(selector: str) -> dict | None:
    """Not applicable to demo/real modes — no DOM available."""
    return None
