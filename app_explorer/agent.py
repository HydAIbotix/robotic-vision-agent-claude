"""
AppExplorer LangGraph — one-time app understanding phase.

Flow:
  explore_screen ──► check_queue ──► execute_action ──► identify_result
        ▲                 │                                     │
        │ (new screen)    │ (queue empty)                       │ (new screen)
        └─────────────────┼─────────────────────────────────────┘
                          ▼
                     validate_map ──► finalize_map ──► END

explore_screen  : analyze current screenshot; add screen + elements to AppMap;
                  queue all explorable actions Claude identifies on this screen.
check_queue     : routing only — send to execute_action or validate_map.
execute_action  : pop next queued action; execute its steps; resolve result screenshot.
identify_result : ask Claude what screen the result is; record transition in AppMap.
                  If new screen → explore_screen to analyze and queue its actions.
                  If known screen → check_queue for next pending action.
validate_map    : self-correction pass — dedup screens, re-validate DOM coordinates,
                  flag out-of-bounds elements. Runs once after the queue empties.
finalize_map    : persist the completed AppMap to disk.
"""
from langgraph.graph import StateGraph, END
from app_explorer.state import ExplorerState
from app_explorer.nodes.explore_screen import explore_screen
from app_explorer.nodes.execute_action import execute_action
from app_explorer.nodes.identify_result import identify_result
from app_explorer.nodes.validate_map import validate_map
from app_explorer.nodes.finalize_map import finalize_map


def _check_queue(state: ExplorerState) -> str:
    if state.get("exploration_queue"):
        return "execute_action"
    return "validate_map"


def _route_after_identify(state: ExplorerState) -> str:
    if state.get("last_result_is_new"):
        return "explore_screen"
    return "check_queue"


def create_explorer():
    g = StateGraph(ExplorerState)

    g.add_node("explore_screen",  explore_screen)
    g.add_node("execute_action",  execute_action)
    g.add_node("identify_result", identify_result)
    g.add_node("validate_map",    validate_map)
    g.add_node("finalize_map",    finalize_map)

    # Entry point — always start by exploring the first screen
    g.set_entry_point("explore_screen")

    # After explore_screen: decide whether to execute or wrap up
    g.add_conditional_edges(
        "explore_screen",
        _check_queue,
        {"execute_action": "execute_action", "validate_map": "validate_map"},
    )

    # After execution: always identify what happened
    g.add_edge("execute_action", "identify_result")

    # After identifying: go back to explore_screen if new, else re-check queue
    g.add_conditional_edges(
        "identify_result",
        _route_after_identify,
        {"explore_screen": "explore_screen", "check_queue": "check_queue"},
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
