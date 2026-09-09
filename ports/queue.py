"""
Task queue port — decouple the API tier from the heavy agent work (the AgentCore-Runtime analogue).

MVP: the API runs each suite in an in-process background thread. To scale horizontally you want the
API to ENQUEUE a job and a separate SERVICE_ROLE=worker process to CONSUME it, so exploration/
execution load scales independently of HTTP traffic. This port abstracts that:

  inline (default) — enqueue == run now, in a same-process daemon thread (byte-identical to the MVP).
  redis            — enqueue == RPUSH onto a Redis list; a worker BLPOPs and runs it.

Composes with the Redis event bus: a worker publishes run/step events to the bus and the API replica
holding the client's WebSocket delivers them — so the API/worker split works end-to-end. Selected by
TASK_QUEUE_BACKEND; the job handler is provided by the caller (api.main._run_job).
"""
from __future__ import annotations

import json
import threading
from typing import Callable

from vision_agent.config import settings

Handler = Callable[[dict], None]


def _redis():
    import redis  # lazy: only when TASK_QUEUE_BACKEND=redis
    if not settings.redis_url:
        raise RuntimeError("TASK_QUEUE_BACKEND=redis but REDIS_URL is empty")
    return redis.Redis.from_url(settings.redis_url)


def enqueue_run(payload: dict, handler: Handler) -> None:
    """Submit a run job. inline → run now in a daemon thread (the MVP path); redis → push for a
    worker. `handler` is used only by the inline path (the worker supplies its own via consume_runs)."""
    if settings.task_queue_backend == "redis":
        _redis().rpush(settings.task_queue_key, json.dumps(payload))
    else:
        threading.Thread(target=handler, args=(payload,), daemon=True).start()


def consume_runs(handler: Handler) -> None:
    """Worker loop (redis backend): block-pop run jobs and hand each to handler(payload). Blocks
    forever; a failing job is logged and the loop continues so one bad job never kills the worker."""
    r = _redis()
    print(f"[worker] consuming run jobs from '{settings.task_queue_key}' …")
    while True:
        item = r.blpop(settings.task_queue_key, timeout=5)
        if not item:
            continue
        try:
            handler(json.loads(item[1]))
        except Exception as exc:  # pragma: no cover - worker resilience
            print(f"[worker] run job failed: {exc}")
