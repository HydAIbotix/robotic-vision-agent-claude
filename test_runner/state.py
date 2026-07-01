from typing import Optional
from typing_extensions import TypedDict
from app_map.store import AppMap


class TestCase(TypedDict):
    test_id: str
    summary: str
    description: str
    preconditions: str
    steps_raw: str
    expected_results_raw: str


class TestResult(TypedDict):
    test_id: str
    summary: str
    outcome: str          # "passed" | "failed" | "error"
    step_results: list    # list[StepResult] from vision_agent.state
    vision_summary: str   # the agent's own summary string
    conclusive_verdict: dict  # verdict from conclusive_verdict node; {} if not yet run


class TestRunnerState(TypedDict):
    # ── Run identity (used for live event broadcasting) ───────────────────────
    run_id: str

    # ── Static config (set once at start) ────────────────────────────────────
    test_cases: list[TestCase]
    app_map: Optional[dict]   # None → agent navigates dynamically; set if AppMap available
    credentials: dict          # {"valid": {"email": ..., "password": ...}, "invalid": {...}}
    # demo_screens: screen_id → absolute screenshot path (used instead of robot camera)
    demo_screens: dict

    # ── Iteration pointers ────────────────────────────────────────────────────
    current_tc_idx: int

    # ── Per-test-case working state ───────────────────────────────────────────
    current_tc: Optional[TestCase]
    # structured_plan: Tier-1/2 plan with pixel coords — executed without LLM calls.
    # None → fall through to Tier-3 (legacy vision-agent path).
    structured_plan: Optional[dict]
    # planned_steps: Tier-3 fallback format ["tap: X", "type: Y", "verify: Z"]
    planned_steps: list[str]
    credential_scenario: str   # "valid" | "invalid"
    # start_image: the screenshot to use as the entry point for this test case
    start_image: str

    # ── Accumulated results ───────────────────────────────────────────────────
    test_results: list[TestResult]

    # ── Final output ──────────────────────────────────────────────────────────
    summary: str
