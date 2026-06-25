"""
execute_step node — dispatches the current planned step to the robot arm
(tap / type / swipe) then captures the resulting screen.
"""
import time
from pathlib import Path
from vision_agent.state import VisionAgentState, StepResult
from vision_agent import robot
from vision_agent.config import settings


def _find_element(analysis, target: str) -> dict | None:
    """Match by element id first, then by label (case-insensitive)."""
    for el in analysis["elements"]:
        if el["id"] == target or el["label"].strip().lower() == target.lower():
            return el
    return None


def execute_step(state: VisionAgentState) -> dict:
    step = state["planned_steps"][state["current_step_idx"]]
    analysis = state["screen_analysis"]
    screenshot_before = state["image_path"]

    action_type, _, target = step.partition(": ")
    action_type = action_type.strip()
    target = target.strip()

    coordinates: list[int] = []
    error: str | None = None

    print(f"\n  [STEP {state['current_step_idx'] + 1}] {step}")

    if action_type == "tap":
        el = _find_element(analysis, target)
        if el is None:
            error = f"Element '{target}' not found on current screen"
            print(f"    [ERROR] {error}")
        else:
            coordinates = el["center"]
            robot.tap(*coordinates)

    elif action_type == "type":
        robot.type_text(target)

    elif action_type == "verify":
        pass  # no robot action; validate_step will check the screen

    else:
        error = f"Unknown action type: '{action_type}'"

    # Capture the screen after the action (stub returns next demo frame in demo mode)
    ts = int(time.time() * 1000)
    save_path = str(Path(settings.screenshots_dir) / f"after_{ts}.png")
    capture = robot.capture_screen(save_path)
    after_path = capture["image_path"]

    result: StepResult = {
        "step_instruction": step,
        "action_type": action_type,
        "target": target,
        "coordinates": coordinates,
        "success": error is None,
        "screenshot_before": screenshot_before,
        "screenshot_after": after_path,
        "observation": "",
        "error": error,
    }

    return {
        "step_results": [*(state.get("step_results") or []), result],
        "image_path": after_path,
    }
