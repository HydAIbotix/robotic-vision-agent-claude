"""
run_vision_step — invoke the VisionAgent for one test case.

Playwright mode  (ROBOT_BACKEND=playwright):
  Browser is already open at the kiosk URL.  Reset to entry, take an initial
  screenshot, then let the agent interact with the live page.  No demo mapping needed.

Demo mode  (ROBOT_BACKEND=demo):
  Derive a per-step screenshot sequence from planned_steps + DEMO_SCREENS using
  element-ID heuristics.  No AppMap or pre-exploration needed.
"""
import time
from pathlib import Path
from vision_agent.agent import create_agent
from vision_agent.state import VisionAgentState
from vision_agent import robot
from vision_agent.config import settings
from test_runner.state import TestRunnerState, TestResult


# ── Navigation heuristics for demo mode ──────────────────────────────────────
# Map element ID fragments to resulting screen_ids.
# Credentials-aware entries are tuples: (valid_result, invalid_result)
_NAV: list[tuple[str, object]] = [
    ("sign_in_button",              ("products", "login")),   # valid/invalid creds
    ("sign_in",                     ("products", "login")),
    ("add_to_cart",                 "product_added_to_cart"),
    ("cart_checkout",               "cart"),
    ("checkout_button",             "cart"),
    ("cart_icon",                   "cart"),
    ("proceed_to_card_payment",     "payment"),
    ("proceed_to_payment",          "payment"),
    ("use_mock_approval",           "success"),
    ("mock_approval",               "success"),
    ("view_order_history",          "order_history"),
    ("order_history_button",        "order_history"),
    ("go_to_home",                  "products"),
    ("home_button",                 "products"),
    ("sign_out",                    "login"),
    ("logout",                      "login"),
]


def _demo_sequence(
    planned_steps: list[str],
    demo_screens: dict,
    start_screen: str,
    credential_scenario: str,
) -> list[str]:
    """
    Return one screenshot path per planned step — what the robot camera returns
    AFTER each action executes.  Uses element-ID pattern matching; unknown taps
    stay on the current screen.
    """
    current = start_screen
    seq: list[str] = []

    for step in planned_steps:
        action, _, target = step.partition(": ")
        if action.strip() == "tap":
            t = target.strip().lower()
            for fragment, dest in _NAV:
                if fragment in t or t in fragment:
                    if isinstance(dest, tuple):
                        next_screen = dest[0] if credential_scenario == "valid" else dest[1]
                    else:
                        next_screen = dest
                    if next_screen in demo_screens:
                        current = next_screen
                    break
        # type / verify / unmatched tap → stay on current screen
        seq.append(demo_screens.get(current, ""))

    return seq


def run_vision_step(state: TestRunnerState) -> dict:
    tc            = state["current_tc"]
    planned_steps = state["planned_steps"]
    demo_screens  = state.get("demo_screens") or {}
    start_screen  = state["app_map"].get("entry_screen", "login") if state.get("app_map") else "login"
    credential_scenario = state.get("credential_scenario", "valid")

    if settings.robot_backend == "playwright":
        # ── Playwright mode: reset to entry, capture real initial screenshot ──
        robot.reset_to_entry()
        save_path = str(
            Path(settings.screenshots_dir)
            / f"start_{tc['test_id']}_{int(time.time())}.png"
        )
        Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
        start_image = robot.capture_screen(save_path)["image_path"]
        print(f"  [PLAYWRIGHT] Initial screenshot: {start_image}")
    else:
        # ── Demo mode: pre-load heuristic screenshot sequence ─────────────────
        start_image = demo_screens.get(start_screen, state.get("start_image", ""))
        demo_seq = _demo_sequence(planned_steps, demo_screens, start_screen, credential_scenario)
        robot.set_demo_screens(demo_seq)

    # ── Run the VisionAgent ───────────────────────────────────────────────────
    agent = create_agent()
    initial: VisionAgentState = {
        "task_description": tc["summary"],
        "image_path":       start_image,
        "screen_analysis":  None,
        "planned_steps":    planned_steps,
        "current_step_idx": 0,
        "step_results":     [],
        "retry_count":      0,
        "screen_history":   [],
        "decision_tree":    {},
        "outcome":          "running",
        "summary":          "",
        "error_message":    None,
    }
    result = agent.invoke(initial)

    step_results = result.get("step_results") or []
    passed  = sum(1 for r in step_results if r["success"])
    outcome = result.get("outcome", "failed")
    print(f"\n  [RESULT] {tc['test_id']}  {outcome.upper()}  ({passed}/{len(step_results)} steps passed)")
    print(f"           Journey: {' -> '.join(result.get('screen_history') or [])}")

    test_result: TestResult = {
        "test_id":        tc["test_id"],
        "summary":        tc["summary"],
        "outcome":        outcome,
        "step_results":   step_results,
        "vision_summary": result.get("summary", ""),
    }
    return {"test_results": [*(state.get("test_results") or []), test_result]}
