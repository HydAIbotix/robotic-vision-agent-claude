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
        scroll_page, get_page_scroll_info, get_dom_screen_id, get_dom_element_centers,
        navigate_to_screen, update_explorer_progress,
    )

elif _s.robot_backend == "real":
    from vision_agent.robot.real_robot import (         # noqa: F401
        capture_screen, tap, type_text, swipe,
        set_demo_screens, set_keyboard_map, reset_to_entry,
        scroll_page, get_page_scroll_info, get_dom_screen_id, get_dom_element_centers,
        navigate_to_screen, update_explorer_progress,
        # Real-robot-only extras (imported directly in scripts that need them)
        setup, navigate_to_kiosk, calibrate,
        card_pick, card_tap, card_replace,
        get_events, get_status, get_base_pose, get_arm_state,
    )
    def stop() -> None: pass                            # noqa: E704

else:  # "demo" (default)
    from vision_agent.robot.stubs import (              # noqa: F401
        capture_screen, tap, type_text, swipe,
        set_demo_screens, set_keyboard_map,
        scroll_page, get_page_scroll_info, get_dom_screen_id, get_dom_element_centers,
        navigate_to_screen, update_explorer_progress,
    )
    def reset_to_entry() -> None: pass                  # noqa: E704
    def stop() -> None: pass                            # noqa: E704
