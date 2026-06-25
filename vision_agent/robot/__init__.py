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
        set_demo_screens, reset_to_entry, stop,
    )
elif _s.robot_backend == "real":
    from vision_agent.robot.real_robot import (         # noqa: F401  (created when hardware arrives)
        capture_screen, tap, type_text, swipe,
    )
    def set_demo_screens(paths: list) -> None: pass     # noqa: E704
    def reset_to_entry() -> None: pass                  # noqa: E704
else:  # "demo" (default)
    from vision_agent.robot.stubs import (              # noqa: F401
        capture_screen, tap, type_text, swipe, set_demo_screens,
    )
    def reset_to_entry() -> None: pass                  # noqa: E704
