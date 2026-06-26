"""
Robot backend dispatcher.  Reads ROBOT_BACKEND from .env and re-exports the
matching implementation's functions so all agent code uses:

    from vision_agent import robot
    robot.tap(x, y)
    robot.capture_screen(save_path)
    robot.type_text(text)

No agent file needs to know which backend is active.
"""
from vision_agent.config import settings as _s

if _s.robot_backend == "playwright":
    from vision_agent.robot.playwright_stubs import (   # noqa: F401
        capture_screen, tap, type_text, swipe,
        set_demo_screens, set_keyboard_map, reset_to_entry, stop,
        scroll_page, get_page_scroll_info,
    )
elif _s.robot_backend == "real":
    from vision_agent.robot.real_robot import (         # noqa: F401  (created when hardware arrives)
        capture_screen, tap, type_text, swipe,
    )
    def set_demo_screens(paths: list) -> None: pass     # noqa: E704
    def set_keyboard_map(kmap: dict) -> None: pass      # noqa: E704
    def reset_to_entry() -> None: pass                  # noqa: E704
    def scroll_page(x: int, y: int, delta_y: int) -> dict: return {"success": True}  # noqa: E704
    def get_page_scroll_info() -> dict: return {"scrollTop": 0, "scrollHeight": 900, "viewportHeight": 900, "scrollLeft": 0, "scrollWidth": 1400, "viewportWidth": 1400}  # noqa: E704
else:  # "demo" (default)
    from vision_agent.robot.stubs import (              # noqa: F401
        capture_screen, tap, type_text, swipe,
        set_demo_screens, set_keyboard_map,
        scroll_page, get_page_scroll_info,
    )
    def reset_to_entry() -> None: pass                  # noqa: E704
