"""
TestRunner LangGraph — end-to-end test execution pipeline.

Flow per test case:
  load_test_case → parse_steps → route_by_type → run_vision_step  ──┐
                                                  run_backend_step ──┤
                                                                      ▼
                                               check_next ──► load_test_case (loop)
                                                         └──► finalize_tests  ──► END

load_test_case    : copy current test case into working state; set start screenshot.
parse_steps       : Claude converts raw test case text → planned_steps list.
route_by_type     : decide vision vs backend based on step content.
run_vision_step   : invoke the VisionAgent with pre-parsed steps + demo screen sequence.
run_backend_step  : stub — API / DB validation (future).
check_next        : route to next test case or finalize.
finalize_tests    : compute suite outcome; write results JSON.
"""
from langgraph.graph import StateGraph, END
from test_runner.state import TestRunnerState
from test_runner.nodes.load_test_case import load_test_case
from test_runner.nodes.parse_steps import parse_steps
from test_runner.nodes.run_vision_step import run_vision_step
from test_runner.nodes.run_backend_step import run_backend_step
from test_runner.nodes.finalize_tests import finalize_tests


def _route_by_type(state: TestRunnerState) -> str:
    """Route based on whether the test case has backend-only steps."""
    tc = state.get("current_tc") or {}
    summary = (tc.get("summary") or "").lower()
    # For now: route to backend only if summary explicitly mentions "backend"
    if "backend" in summary or "api" in summary or "database" in summary:
        return "run_backend_step"
    return "run_vision_step"


def _check_next(state: TestRunnerState) -> str:
    idx   = (state.get("current_tc_idx") or 0) + 1
    total = len(state.get("test_cases") or [])
    if idx < total:
        return "advance"
    return "finalize_tests"


def _advance(state: TestRunnerState) -> dict:
    return {"current_tc_idx": (state.get("current_tc_idx") or 0) + 1}


def create_test_runner():
    g = StateGraph(TestRunnerState)

    g.add_node("load_test_case",   load_test_case)
    g.add_node("parse_steps",      parse_steps)
    g.add_node("run_vision_step",  run_vision_step)
    g.add_node("run_backend_step", run_backend_step)
    g.add_node("advance",          _advance)
    g.add_node("finalize_tests",   finalize_tests)

    g.set_entry_point("load_test_case")
    g.add_edge("load_test_case", "parse_steps")

    g.add_conditional_edges(
        "parse_steps",
        _route_by_type,
        {"run_vision_step": "run_vision_step", "run_backend_step": "run_backend_step"},
    )

    # After execution: check if there are more test cases
    for node in ("run_vision_step", "run_backend_step"):
        g.add_node(f"check_next_after_{node}", lambda s: {})
        g.add_edge(node, f"check_next_after_{node}")
        g.add_conditional_edges(
            f"check_next_after_{node}",
            _check_next,
            {"advance": "advance", "finalize_tests": "finalize_tests"},
        )

    g.add_edge("advance", "load_test_case")
    g.add_edge("finalize_tests", END)

    return g.compile()
