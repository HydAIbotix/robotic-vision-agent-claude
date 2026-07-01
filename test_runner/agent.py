"""
TestRunner LangGraph — end-to-end test execution pipeline.

Flow per test case:
  load_test_case → parse_steps → route_by_type → run_vision_step  ──┐
                                                  run_backend_step ──┤
                                                                      ▼
                                               conclusive_verdict
                                                      │
                                               check_next ──► load_test_case (loop)
                                                        └──► finalize_tests  ──► END

load_test_case      : copy current test case into working state; set start screenshot.
parse_steps         : Claude converts raw test case text → planned_steps list.
route_by_type       : decide vision vs backend based on step content.
run_vision_step     : invoke the VisionAgent with pre-parsed steps + demo screen sequence.
run_backend_step    : stub — API / DB validation (future).
conclusive_verdict  : reason about test intent vs actual outcome; upgrade FAIL→PASS when
                      objective was met via alternative path; flag AMBIGUOUS for human review.
check_next          : route to next test case or finalize.
finalize_tests      : compute suite outcome; write results JSON.
"""
from langgraph.graph import StateGraph, END
from test_runner.state import TestRunnerState
from test_runner.nodes.load_test_case import load_test_case
from test_runner.nodes.parse_steps import parse_steps
from test_runner.nodes.run_vision_step import run_vision_step
from test_runner.nodes.run_backend_step import run_backend_step
from test_runner.nodes.conclusive_verdict import conclusive_verdict
from test_runner.nodes.finalize_tests import finalize_tests


def _route_by_type(state: TestRunnerState) -> str:
    """Route based on whether the test case has backend-only steps."""
    tc = state.get("current_tc") or {}
    summary = (tc.get("summary") or "").lower()
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

    g.add_node("load_test_case",    load_test_case)
    g.add_node("parse_steps",       parse_steps)
    g.add_node("run_vision_step",   run_vision_step)
    g.add_node("run_backend_step",  run_backend_step)
    g.add_node("conclusive_verdict", conclusive_verdict)
    g.add_node("advance",           _advance)
    g.add_node("finalize_tests",    finalize_tests)

    g.set_entry_point("load_test_case")
    g.add_edge("load_test_case", "parse_steps")

    g.add_conditional_edges(
        "parse_steps",
        _route_by_type,
        {"run_vision_step": "run_vision_step", "run_backend_step": "run_backend_step"},
    )

    # After execution: run conclusive_verdict, then check whether more tests remain
    for node in ("run_vision_step", "run_backend_step"):
        g.add_edge(node, "conclusive_verdict")

    g.add_node("check_next", lambda s: {})
    g.add_edge("conclusive_verdict", "check_next")
    g.add_conditional_edges(
        "check_next",
        _check_next,
        {"advance": "advance", "finalize_tests": "finalize_tests"},
    )

    g.add_edge("advance", "load_test_case")
    g.add_edge("finalize_tests", END)

    return g.compile()
