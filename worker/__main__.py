"""
Run-job worker process (the SERVICE_ROLE=worker tier).

Consumes run jobs the API enqueued and executes them — decoupling heavy exploration/execution from
HTTP traffic. It shares the run logic (api.main._run_job → _execute_run) and the DB/object store with
the API, and PUBLISHES live events to the Redis event bus so the API replica holding a client's
WebSocket delivers them (see api.main._broadcast). Scale by running N of these.

Usage:
    python -m worker              # Redis-queue consumer (TASK_QUEUE_BACKEND=redis)
    python -m worker --temporal   # Temporal worker (ORCHESTRATOR_BACKEND=temporal; needs temporalio)

Both need the same env as the API (DB_URL, S3_*, REDIS_URL, ANTHROPIC_API_KEY, …).
"""
import sys

from api.database import init_db
from api.main import _run_job
from vision_agent.config import settings


def _run_redis_worker() -> None:
    from ports.queue import consume_runs
    if settings.task_queue_backend != "redis":
        print("[worker] WARNING: TASK_QUEUE_BACKEND is not 'redis'; nothing will be enqueued to consume.")
    consume_runs(_run_job)


def _run_temporal_worker() -> None:
    import asyncio

    from temporalio.client import Client
    from temporalio.worker import Worker
    from orchestration.temporal_app import RunSuiteWorkflow, run_suite_activity, run_test_activity

    async def _go():
        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[RunSuiteWorkflow],
            activities=[run_suite_activity, run_test_activity],
        )
        print(f"[worker] Temporal worker on task_queue '{settings.temporal_task_queue}' …")
        await worker.run()

    asyncio.run(_go())


def main() -> None:
    init_db()
    if "--temporal" in sys.argv[1:]:
        _run_temporal_worker()
    else:
        _run_redis_worker()


if __name__ == "__main__":
    main()
