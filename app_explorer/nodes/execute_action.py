"""
execute_action — pop the next queued action, execute its steps via the robot,
then resolve the resulting screenshot from the demo_navigation map (or robot camera).
"""
from vision_agent.robot import stubs as robot
from app_explorer.state import ExplorerState, ExplorationAction


def _resolve(value: str, credentials: dict) -> str:
    if not value:
        return value
    valid = credentials.get("valid", {})
    invalid = credentials.get("invalid", {})
    return (
        value
        .replace("{{valid_email}}",    valid.get("email",    "tester@kiosk.local"))
        .replace("{{valid_password}}", valid.get("password", "Password123"))
        .replace("{{invalid_email}}",  invalid.get("email",  "baduser@example.com"))
        .replace("{{invalid_password}}", invalid.get("password", "WrongPass!"))
    )


def execute_action(state: ExplorerState) -> dict:
    queue    = list(state["exploration_queue"])
    action: ExplorationAction = queue.pop(0)
    explored = list(state.get("explored_action_keys") or [])
    credentials = state.get("credentials") or {}
    demo_nav    = state.get("demo_navigation") or {}

    full_key = f"{action['screen_id']}::{action['action_key']}"
    print(f"\n  [ACTION] {full_key}")
    print(f"           {action['description']}")

    # Execute each step in the action sequence
    for step in action.get("steps") or []:
        act = step["action_type"]
        eid = step.get("element_id", "")
        val = _resolve(step.get("value") or "", credentials)

        if act == "type":
            print(f"    type  element={eid!r}  value={val!r}")
            robot.type_text(val)
        elif act == "tap":
            print(f"    tap   element={eid!r}")
            # Coordinates are 0,0 in exploration mode — the demo nav resolves
            # the result screenshot; live mode will use real coordinates
            robot.tap(0, 0)

    # Resolve result screenshot: demo nav map takes precedence over robot camera
    result_image = demo_nav.get(full_key)
    if result_image:
        print(f"    demo result: {result_image}")
    else:
        import time
        from pathlib import Path
        from vision_agent.config import settings
        ts = int(time.time() * 1000)
        save_path = str(Path(settings.screenshots_dir) / f"explore_{ts}.png")
        capture = robot.capture_screen(save_path)
        result_image = capture["image_path"]
        print(f"    robot capture: {result_image}")

    explored.append(full_key)

    return {
        "exploration_queue":    queue,
        "explored_action_keys": explored,
        "current_image_path":   result_image,
        "last_executed_action": action,
    }
