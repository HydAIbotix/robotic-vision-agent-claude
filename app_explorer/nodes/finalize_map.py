"""
finalize_map — persist the completed AppMap to disk and mark exploration done.
"""
from app_explorer.state import ExplorerState
from app_map import store as app_map_store


def finalize_map(state: ExplorerState) -> dict:
    path = state.get("app_map_path", "app_map.json")
    app_map_store.save(state["app_map"], path)

    screen_count = len(state["app_map"].get("screens") or {})
    transition_count = sum(
        len(sc.get("transitions") or {})
        for sc in (state["app_map"].get("screens") or {}).values()
    )
    print(f"\n  [DONE] Exploration complete.")
    print(f"         Screens: {screen_count}   Transitions: {transition_count}")
    print(f"         App map saved -> {path}")

    return {"complete": True}
