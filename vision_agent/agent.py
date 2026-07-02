"""
LangGraph state machine for the vision agent.

Graph flow:
                    ┌─────────────────────────────────┐
                    ↓                                 │ (retry)
  analyze_screen → plan_steps → execute_step → validate_step
                                                      │
                              ┌───────────────────────┤
                              ↓ (step passed)         ↓ (step failed, retries left)
                         advance_step           handle_retry → analyze_screen
                              │
                              ↓ (more steps)           ↓ (no more steps)
                         analyze_screen ──────────► finalize → END
"""
from langgraph.graph import StateGraph, END
from vision_agent.state import VisionAgentState
from vision_agent.nodes.analyze import analyze_screen
from vision_agent.nodes.plan import plan_steps
from vision_agent.nodes.execute import execute_step
from vision_agent.nodes.validate import validate_step
from vision_agent.nodes.retry import handle_retry
from vision_agent.nodes.finalize import finalize
from vision_agent.config import settings


# ── Routing functions ──────────────────────────────────────────────────────────

def _route_after_validation(state: VisionAgentState) -> str:
    last = (state.get("step_results") or [{}])[-1]
    idx = state.get("current_step_idx", 0)
    steps = state.get("planned_steps") or []

    # Absolute safety cap on total executed steps (across all re-plans).  A hard stop
    # against runaway loops regardless of retry accounting.
    if len(state.get("step_results") or []) >= 30:
        print("  [GUARD] Reached 30 total steps — finalizing to prevent an execution loop")
        return "finalize"

    if not last.get("success"):
        # retry_count is a GLOBAL re-plan budget (see _advance_step) — bounded, never reset.
        if (state.get("retry_count") or 0) < settings.max_retries:
            return "handle_retry"
        return "finalize"  # exhausted re-plan budget

    if idx + 1 < len(steps):
        return "advance_step"
    return "finalize"  # all steps done


def _advance_step(state: VisionAgentState) -> dict:
    """Move to the next planned step.

    retry_count is deliberately NOT reset here.  handle_retry re-plans on every failure
    (it clears planned_steps), so retry_count serves as a GLOBAL re-plan budget.  Resetting
    it on advance would let a re-plan whose early steps happen to succeed (e.g. re-typing a
    login) reset the budget and loop forever — the "continuous login" bug.
    """
    return {"current_step_idx": (state.get("current_step_idx") or 0) + 1}


def _route_after_advance(state: VisionAgentState) -> str:
    """
    Skip re-analysis after type/verify steps — the screen doesn't change, so
    reusing the current screen_analysis halves LLM calls for input-heavy tests.
    Always re-analyze before tap steps so coordinates are fresh after navigation.
    """
    idx   = state.get("current_step_idx", 0)
    steps = state.get("planned_steps") or []
    if idx < len(steps):
        action = steps[idx].partition(":")[0].strip()
        if action in ("type", "verify"):
            return "execute_step"
    return "analyze_screen"


# ── Graph assembly ─────────────────────────────────────────────────────────────

def create_agent():
    graph = StateGraph(VisionAgentState)

    graph.add_node("analyze_screen", analyze_screen)
    graph.add_node("plan_steps", plan_steps)
    graph.add_node("execute_step", execute_step)
    graph.add_node("validate_step", validate_step)
    graph.add_node("handle_retry", handle_retry)
    graph.add_node("advance_step", _advance_step)
    graph.add_node("finalize", finalize)

    graph.set_entry_point("analyze_screen")

    graph.add_edge("analyze_screen", "plan_steps")
    graph.add_edge("plan_steps", "execute_step")
    graph.add_edge("execute_step", "validate_step")

    graph.add_conditional_edges(
        "validate_step",
        _route_after_validation,
        {
            "handle_retry": "handle_retry",
            "advance_step": "advance_step",
            "finalize": "finalize",
        },
    )

    # After retry: re-analyze (screen may have changed) then re-execute same step
    graph.add_edge("handle_retry", "analyze_screen")

    # After advancing: re-analyze only before tap steps; skip for type/verify
    graph.add_conditional_edges(
        "advance_step",
        _route_after_advance,
        {"analyze_screen": "analyze_screen", "execute_step": "execute_step"},
    )

    graph.add_edge("finalize", END)

    return graph.compile()
