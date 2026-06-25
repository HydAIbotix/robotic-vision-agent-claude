from typing import Literal, Optional
from typing_extensions import TypedDict


class ScreenElement(TypedDict):
    id: str            # snake_case identifier, unique within the screen
    type: str          # button | input | text | link | dropdown | stepper
    label: str         # visible text or placeholder
    description: str   # what happens when you interact with this element
    bbox: list[int]    # [x1, y1, x2, y2] pixel coordinates (top-left origin)
    center: list[int]  # [cx, cy] — exact robot arm tap point
    confidence: float  # 0.0 – 1.0


class ScreenAnalysis(TypedDict):
    screen_id: str              # login | products | cart | payment | success | unknown
    description: str            # one-line summary of this screen's purpose
    elements: list[ScreenElement]


class StepResult(TypedDict):
    step_instruction: str   # e.g. "tap: sign_in_button"
    action_type: str        # tap | type | verify
    target: str             # element id or text value
    coordinates: list[int]  # [x, y] actually commanded to robot
    success: bool
    screenshot_before: str  # local path or S3 key
    screenshot_after: str
    observation: str        # Claude's one-line description of what changed
    error: Optional[str]


class VisionAgentState(TypedDict):
    # ── Input ──────────────────────────────────────────────────────────────────
    task_description: str   # natural-language task, e.g. "log in as tester@kiosk.local"
    image_path: str         # current screen image (local path or S3 key)

    # ── Analysis ───────────────────────────────────────────────────────────────
    screen_analysis: Optional[ScreenAnalysis]

    # ── Plan ───────────────────────────────────────────────────────────────────
    planned_steps: list[str]   # ["tap: email_input", "type: foo@bar.com", ...]
    current_step_idx: int

    # ── Execution ──────────────────────────────────────────────────────────────
    step_results: list[StepResult]
    retry_count: int

    # ── Navigation memory ──────────────────────────────────────────────────────
    # Ordered list of screen_ids visited during this task
    screen_history: list[str]
    # {screen_id: {step_instruction: resulting_screen_id}} — built automatically
    decision_tree: dict

    # ── Output ─────────────────────────────────────────────────────────────────
    outcome: Literal["running", "passed", "failed", "error"]
    summary: str
    error_message: Optional[str]
