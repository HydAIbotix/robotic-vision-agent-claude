"""
AppExplorer LangGraph — one-time app understanding phase.

Flow:
  choose_explore ──► explore_screen     ──► check_queue ──► execute_action ──► identify_result
        ▲        └──► explore_screen_aria       │                                     │
        │                                       │ (queue empty)                       │ (new screen)
        └───────────────────────────────────────┼─────────────────────────────────────┘
                                                ▼
                                          validate_map ──► finalize_map ──► END

choose_explore      : route to explore_screen or explore_screen_aria based on config.
explore_screen      : Claude vision — screenshot → elements (default; all backends).
explore_screen_aria : ARIA tree → elements (playwright backend only; 0 image-LLM calls).
check_queue         : routing only — send to execute_action or validate_map.
execute_action      : pop next queued action; execute its steps; resolve result screenshot.
identify_result     : ask Claude what screen the result is; record transition in AppMap.
                      If new screen → choose_explore to analyze and queue its actions.
                      If known screen → check_queue for next pending action.
validate_map        : self-correction pass — dedup screens, re-validate DOM coordinates,
                      flag out-of-bounds elements. Runs once after the queue empties.
finalize_map        : persist the completed AppMap to disk.
"""
from langgraph.graph import StateGraph, END
from app_explorer.state import ExplorerState
from app_explorer.nodes.explore_screen import explore_screen
from app_explorer.nodes.explore_screen_aria import explore_screen_aria
from app_explorer.nodes.execute_action import execute_action
from app_explorer.nodes.identify_result import identify_result
from app_explorer.nodes.validate_map import validate_map
from app_explorer.nodes.finalize_map import finalize_map
from vision_agent.config import settings


def _check_queue(state: ExplorerState) -> str:
    if state.get("exploration_queue"):
        return "execute_action"
    return "validate_map"


def _route_explore(state: ExplorerState) -> str:
    """Route to ARIA mode only when explicitly configured and on playwright backend."""
    mode    = state.get("exploration_mode") or settings.exploration_mode
    backend = settings.robot_backend
    if mode == "playwright_aria" and backend == "playwright":
        return "explore_screen_aria"
    return "explore_screen"


def _route_after_identify(state: ExplorerState) -> str:
    if state.get("last_result_is_new"):
        return "choose_explore"
    return "check_queue"


def create_explorer():
    g = StateGraph(ExplorerState)

    g.add_node("choose_explore",      lambda s: {})
    g.add_node("explore_screen",      explore_screen)
    g.add_node("explore_screen_aria", explore_screen_aria)
    g.add_node("execute_action",      execute_action)
    g.add_node("identify_result",     identify_result)
    g.add_node("validate_map",        validate_map)
    g.add_node("finalize_map",        finalize_map)

    # Entry point — dispatch to Claude vision or ARIA explorer
    g.set_entry_point("choose_explore")
    g.add_conditional_edges(
        "choose_explore",
        _route_explore,
        {"explore_screen": "explore_screen", "explore_screen_aria": "explore_screen_aria"},
    )

    # After either explore node: decide whether to execute or wrap up
    for node in ("explore_screen", "explore_screen_aria"):
        g.add_conditional_edges(
            node,
            _check_queue,
            {"execute_action": "execute_action", "validate_map": "validate_map"},
        )

    # After execution: always identify what happened
    g.add_edge("execute_action", "identify_result")

    # After identifying: go back to choose_explore if new, else re-check queue
    g.add_conditional_edges(
        "identify_result",
        _route_after_identify,
        {"choose_explore": "choose_explore", "check_queue": "check_queue"},
    )

    # check_queue is a routing node — implemented as a conditional edge from a
    # pass-through node so the graph stays valid
    g.add_node("check_queue", lambda s: {})
    g.add_conditional_edges(
        "check_queue",
        _check_queue,
        {"execute_action": "execute_action", "validate_map": "validate_map"},
    )

    # validate_map feeds into finalize_map exactly once, at the very end
    g.add_edge("validate_map", "finalize_map")
    g.add_edge("finalize_map", END)

    return g.compile()
