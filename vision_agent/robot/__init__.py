"""
Robot backend dispatcher — DYNAMIC.

Usage is unchanged for callers:

    from vision_agent import robot
    robot.tap(x, y)
    robot.capture_screen(save_path)
    robot.type_text(text)

The active backend is resolved from settings.robot_backend AT CALL TIME (via PEP 562 module
__getattr__), NOT bound once at import.  This means switching the backend at runtime — e.g. from
the management UI's Robot Connection selector, or per-run in _execute_run — takes effect on the
very next call, with no process restart and no stale binding to a previous backend/URL.

Resolution order for an attribute:
  1. the primary module for the active backend (playwright_stubs / real_robot / stubs)
  2. stubs.py            — shared fallback (ARIA/text helpers, move_to_position, …)
  3. a harmless no-op    — for optional lifecycle hooks a backend doesn't implement
"""
import importlib
from vision_agent.config import settings

_PRIMARY = {
    "playwright": "vision_agent.robot.playwright_stubs",
    "real":       "vision_agent.robot.real_robot",
    "demo":       "vision_agent.robot.stubs",
}
_STUBS = "vision_agent.robot.stubs"

# Lifecycle hooks that not every backend implements — resolve to a no-op rather than error.
_OPTIONAL_NOOPS = frozenset({"stop", "reset_to_entry", "update_explorer_progress"})


def active_backend() -> str:
    """The currently-selected backend name ('playwright' | 'real' | 'demo')."""
    return settings.robot_backend if settings.robot_backend in _PRIMARY else "demo"


def _resolve(attr: str):
    primary = importlib.import_module(_PRIMARY[active_backend()])
    if hasattr(primary, attr):
        return getattr(primary, attr)
    stubs = importlib.import_module(_STUBS)
    if hasattr(stubs, attr):
        return getattr(stubs, attr)
    if attr in _OPTIONAL_NOOPS:
        def _noop(*_a, **_k):
            return None
        return _noop
    raise AttributeError(f"robot backend '{active_backend()}' has no attribute '{attr}'")


def __getattr__(attr: str):
    # PEP 562: invoked for any name not found as a real module global — i.e. every robot.<fn>.
    return _resolve(attr)
