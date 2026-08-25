"""Cooperative cancellation for the Auto-Repair LangGraph.

Maps repair_id → a threading.Event that the API sets when the user clicks "Cancel". Nodes call
`bail_if_cancelled()` at their boundaries and the DIAGNOSE call polls `is_cancelled()` while it
waits, so a running repair stops promptly WITHOUT force-killing a thread (Python can't do that
safely). Kept separate from graph state — the same convention as the progress broadcaster.

The API owns the Event object (so its /cancel endpoint can set it) and registers it here under the
graph's internal per-invocation id; readers only ever check `.is_set()`.
"""
import threading
from typing import Optional


class RepairCancelled(Exception):
    """Raised inside a node / the diagnose wait when the user cancelled the repair."""


_events: dict[str, threading.Event] = {}


def register(rid: str, event: Optional[threading.Event]) -> None:
    if rid and event is not None:
        _events[rid] = event


def unregister(rid: str) -> None:
    _events.pop(rid, None)


def is_cancelled(rid: str) -> bool:
    ev = _events.get(rid)
    return bool(ev is not None and ev.is_set())


def bail_if_cancelled(rid: str) -> None:
    """Raise RepairCancelled if this repair has been cancelled — call at each node boundary."""
    if is_cancelled(rid):
        raise RepairCancelled()
