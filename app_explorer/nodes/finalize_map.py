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

    _index_screens_in_memory(state["app_map"])

    return {"complete": True}


def _index_screens_in_memory(app_map: dict) -> None:
    """Record each charted screen in the semantic memory port (AgentCore-Memory analogue), so an
    agent can later recall similar screens across apps by meaning. No-op unless MEMORY_BACKEND is
    set (default none) — so the MVP explorer is completely unaffected."""
    try:
        from ports import memory
        if not memory.enabled():
            return
        app_id = app_map.get("app_name") or app_map.get("entry_screen") or "app"
        for sid, sc in (app_map.get("screens") or {}).items():
            desc = sc.get("description") or ""
            labels = " ".join(str(e.get("label") or "") for e in (sc.get("elements") or []))
            text = f"[{app_id}] screen '{sid}': {desc}\nelements: {labels}".strip()
            memory.remember(text, metadata={"app_id": app_id, "screen_id": sid})
    except Exception as exc:  # never let memory indexing break exploration
        print(f"  [memory] screen indexing skipped ({exc})")
