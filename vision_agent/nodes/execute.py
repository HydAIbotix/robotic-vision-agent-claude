"""
execute_step node — dispatches the current planned step to the robot arm
(tap / type / swipe) then captures the resulting screen.
"""
import time
from pathlib import Path
from vision_agent.state import VisionAgentState, StepResult
from vision_agent import robot
from vision_agent.config import settings


import re as _re

_STOP_WORDS = frozenset({
    "button", "input", "link", "field", "the", "a", "an", "is", "on",
    "for", "to", "in", "of", "and", "or", "use", "tap", "click",
})


def _find_element(analysis: dict, target: str) -> dict | None:
    """
    Match a planned-step target string to a screen element.

    Tries in priority order so the best match wins:
      1. Exact element id
      2. Exact label (case-insensitive)
      3. Either string fully contains the other (case-insensitive)
      4. Normalised alphanumeric match (strips punctuation/spaces)
      5. Significant-keyword overlap — words in the target that appear in
         the element's label or id (after removing stop words)

    This allows planning steps like "tap: use_mock_approval_button" to match
    an element whose label is "Use Mock Approval / Complete Order", or
    "tap: sign_in_button" to match label "Sign In", across any app.
    """
    elements = analysis.get("elements") or []
    if not elements:
        return None

    tgt_lower = target.strip().lower()
    tgt_norm  = _re.sub(r"[^a-z0-9]", "", tgt_lower)

    # 1. Exact id
    for el in elements:
        if el.get("id") == target:
            return el

    # 2. Exact label (case-insensitive)
    for el in elements:
        if el.get("label", "").strip().lower() == tgt_lower:
            return el

    # 3. Full containment either way
    for el in elements:
        el_lower = el.get("label", "").strip().lower()
        if el_lower and (tgt_lower in el_lower or el_lower in tgt_lower):
            return el

    # 4. Normalised alphanumeric containment (ignores / - spaces punctuation)
    for el in elements:
        el_norm = _re.sub(r"[^a-z0-9]", "", el.get("label", "").strip().lower())
        if tgt_norm and el_norm and (tgt_norm in el_norm or el_norm in tgt_norm):
            return el

    # 5. Keyword overlap — split target on _ and spaces; remove stop words
    tgt_words = {
        w for w in _re.split(r"[_\s]+", tgt_lower) if w and w not in _STOP_WORDS
    }
    if tgt_words:
        best_el, best_score = None, 0
        for el in elements:
            el_text = (el.get("label", "") + " " + el.get("id", "")).lower()
            el_words = set(_re.split(r"[_\s/,.\-]+", el_text))
            overlap  = len(tgt_words & el_words)
            if overlap > best_score:
                best_score, best_el = overlap, el
        if best_el:
            matched_label = best_el.get("label", "")
            print(f"    [MATCH] '{target}' → '{matched_label}' (keyword overlap={best_score})")
            return best_el

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
