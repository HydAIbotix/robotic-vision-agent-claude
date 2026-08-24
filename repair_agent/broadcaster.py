"""Progress broadcaster for the Auto-Repair LangGraph.

Maps repair_id → a progress callback so nodes can stream stage updates to the API/UI without
putting a callable into graph state (the same convention as test_runner/broadcaster.py). The
callback receives {"stage", "status", **extra} dicts — identical to the old _emit payload — so the
api/main.py `_repair_stage` sink is unchanged.
"""
from typing import Callable, Optional

_sinks: dict[str, Callable[[dict], None]] = {}


def register(repair_id: str, cb: Optional[Callable[[dict], None]]) -> None:
    if repair_id and cb:
        _sinks[repair_id] = cb


def unregister(repair_id: str) -> None:
    _sinks.pop(repair_id, None)


def emit(repair_id: str, stage: str, status: str, **extra) -> None:
    """Send one {stage, status, ...} update to the registered sink, if any. Never raises."""
    cb = _sinks.get(repair_id)
    if cb:
        try:
            cb({"stage": stage, "status": status, **extra})
        except Exception:
            pass
