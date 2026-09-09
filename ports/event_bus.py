"""
Event bus port — cloud-agnostic realtime fan-out.

The MVP streams live run / step / repair events over an in-process FastAPI WebSocket, which
works only within ONE server process. To scale horizontally (N API replicas behind a load
balancer), an event published by the worker handling a run must reach whichever replica holds
the client's WebSocket. This port abstracts that:

  InMemoryEventBus — single process, byte-identical to the MVP (default).
  RedisEventBus    — Redis pub/sub, so every replica sees every event (horizontal scale-out).

This is the agnostic analogue of the AWS "API Gateway WebSocket + DynamoDB Streams → Lambda
push" stack, with none of the lock-in. The adapter is chosen at call time from
settings.event_bus_backend, so memory<->redis is a config switch, not a code change.

Usage (drop-in alongside the existing in-process broadcaster):
    from ports.event_bus import get_event_bus
    bus = get_event_bus()
    unsubscribe = bus.subscribe(f"run:{run_id}", on_event)   # in the WebSocket handler
    bus.publish(f"run:{run_id}", {"type": "step_result", ...})  # in the worker
"""
from __future__ import annotations

import json
import threading
from typing import Callable

from vision_agent.config import settings

Handler = Callable[[dict], None]


class EventBus:
    """Minimal pub/sub contract. Channels are opaque strings (e.g. "run:<id>")."""

    def publish(self, channel: str, message: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def subscribe(self, channel: str, handler: Handler) -> Callable[[], None]:  # pragma: no cover
        """Register handler for `channel`; returns a callable that unsubscribes it."""
        raise NotImplementedError


class InMemoryEventBus(EventBus):
    """In-process fan-out. Identical semantics to the MVP's single-server behaviour."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = {}
        self._lock = threading.Lock()

    def publish(self, channel: str, message: dict) -> None:
        with self._lock:
            handlers = list(self._subs.get(channel, ()))
        for h in handlers:
            try:
                h(message)
            except Exception:  # a slow/broken subscriber must never break the publisher
                pass

    def subscribe(self, channel: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._subs.setdefault(channel, []).append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                lst = self._subs.get(channel)
                if lst and handler in lst:
                    lst.remove(handler)
                    if not lst:
                        self._subs.pop(channel, None)

        return _unsubscribe


class RedisEventBus(EventBus):
    """Redis pub/sub fan-out across replicas. `redis` is imported lazily so it is not a hard
    dependency until this backend is actually selected."""

    def __init__(self, url: str) -> None:
        import redis  # lazy: only needed when EVENT_BUS_BACKEND=redis

        self._redis = redis.Redis.from_url(url)
        self._threads: list[threading.Thread] = []

    def publish(self, channel: str, message: dict) -> None:
        self._redis.publish(channel, json.dumps(message))

    def subscribe(self, channel: str, handler: Handler) -> Callable[[], None]:
        pubsub = self._redis.pubsub()
        pubsub.subscribe(channel)
        stop = threading.Event()

        def _listen() -> None:
            for item in pubsub.listen():
                if stop.is_set():
                    break
                if item.get("type") != "message":
                    continue
                try:
                    handler(json.loads(item["data"]))
                except Exception:
                    pass

        t = threading.Thread(target=_listen, name=f"redis-sub-{channel}", daemon=True)
        t.start()
        self._threads.append(t)

        def _unsubscribe() -> None:
            stop.set()
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception:
                pass

        return _unsubscribe


_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """Return the process-wide event bus, built once from config.

    A singleton so every publisher/subscriber in the process shares one bus (the in-memory
    adapter is only useful shared). Thread-safe. Redis errors on build surface immediately so
    a misconfiguration is caught at startup rather than silently dropping events.
    """
    global _bus
    if _bus is not None:
        return _bus
    with _bus_lock:
        if _bus is None:
            if settings.event_bus_backend == "redis":
                if not settings.redis_url:
                    raise RuntimeError("EVENT_BUS_BACKEND=redis but REDIS_URL is empty")
                _bus = RedisEventBus(settings.redis_url)
            else:
                _bus = InMemoryEventBus()
    return _bus


def reset_event_bus() -> None:
    """Test hook — drop the cached bus so the next get_event_bus() rebuilds from current config."""
    global _bus
    _bus = None
