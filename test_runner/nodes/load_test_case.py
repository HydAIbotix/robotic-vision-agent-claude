"""
load_test_case — copy the current test case from the list into working state
and resolve the entry screenshot from the app's entry screen.
"""
from test_runner.state import TestRunnerState


def load_test_case(state: TestRunnerState) -> dict:
    idx = state.get("current_tc_idx", 0)
    tc  = state["test_cases"][idx]

    # The entry screen is always the app's entry screen (e.g. login)
    app_map      = state.get("app_map") or {}
    entry_screen = app_map.get("entry_screen", "login")
    start_image  = state.get("demo_screens", {}).get(entry_screen, "")

    print(f"\n{'='*60}")
    print(f"  TEST CASE [{idx+1}/{len(state['test_cases'])}]: {tc['test_id']}")
    print(f"  {tc['summary']}")
    print(f"{'='*60}")

    return {
        "current_tc":          tc,
        "structured_plan":     None,   # cleared per test case; parse_steps fills it
        "planned_steps":       [],
        "credential_scenario": "",
        "start_image":         start_image,
    }
