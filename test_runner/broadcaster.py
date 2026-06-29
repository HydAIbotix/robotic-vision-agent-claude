"""
broadcaster — thread-safe event emitter for live test-step streaming.

During a test run the background thread calls emit() after every step.
main.py registers the WebSocket broadcast function before runner.invoke()
and unregisters it when the run finishes.
"""
import threading
from typing import Callable

_lock: threading.Lock = threading.Lock()
_handlers: dict[str, Callable] = {}


def register(run_id: str, fn: Callable) -> None:
    with _lock:
        _handlers[run_id] = fn


def unregister(run_id: str) -> None:
    with _lock:
        _handlers.pop(run_id, None)


def emit(run_id: str, data: dict) -> None:
    with _lock:
        fn = _handlers.get(run_id)
    if fn:
        try:
            fn(data)
        except Exception:
            pass
