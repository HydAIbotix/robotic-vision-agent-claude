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
        verify_current_screen,
        get_aria_snapshot, text_is_present, query_element_text, get_element_bounding_box,
    )
    from vision_agent.robot.stubs import move_to_position  # noqa: F401  (playwright has no base movement)

elif _s.robot_backend == "real":
    from vision_agent.robot.real_robot import (         # noqa: F401
        capture_screen, tap, type_text, swipe,
        set_demo_screens, set_keyboard_map, reset_to_entry,
        scroll_page, get_page_scroll_info, get_dom_screen_id, get_dom_element_centers,
        navigate_to_screen, update_explorer_progress,
        verify_current_screen,
        # Real-robot-only extras (imported directly in scripts that need them)
        setup, navigate_to_kiosk, calibrate,
        card_pick, card_tap, card_replace,
        get_events, get_status, get_base_pose, get_arm_state,
    )
    from vision_agent.robot.stubs import (              # noqa: F401
        get_aria_snapshot, text_is_present, query_element_text, get_element_bounding_box,
    )
    # move_to_position must be implemented in real_robot.py when hardware arrives
    try:
        from vision_agent.robot.real_robot import move_to_position  # noqa: F401
    except ImportError:
        from vision_agent.robot.stubs import move_to_position        # noqa: F401
    def stop() -> None: pass                            # noqa: E704

else:  # "demo" (default)
    from vision_agent.robot.stubs import (              # noqa: F401
        capture_screen, tap, type_text, swipe,
        set_demo_screens, set_keyboard_map, move_to_position,
        scroll_page, get_page_scroll_info, get_dom_screen_id, get_dom_element_centers,
        navigate_to_screen, update_explorer_progress,
        verify_current_screen,
        get_aria_snapshot, text_is_present, query_element_text, get_element_bounding_box,
    )
    def reset_to_entry() -> None: pass                  # noqa: E704
    def stop() -> None: pass                            # noqa: E704
