"""
Orchestration port — how a run is launched and driven (the Step-Functions analogue).

  inprocess (default) — submit the run through the task queue (ports/queue.py). With TASK_QUEUE=inline
                        this is the MVP (in-process thread); with TASK_QUEUE=redis it's the API/worker
                        split. This is the wired, default path.
  temporal            — run the suite as a durable Temporal workflow/activity (survives restarts,
                        automatic retries, visibility). Optional; requires the `temporalio` SDK and a
                        Temporal server. Lazy-imported, so nothing is pulled in until selected.

submit_run(payload, handler) is what api.main.start_run calls; the backend decides how it actually
runs. Keeping this a seam means swapping to Temporal (or Step Functions on AWS) is a config change.
"""
from __future__ import annotations

from typing import Callable

from vision_agent.config import settings

Handler = Callable[[dict], None]


def submit_run(payload: dict, handler: Handler) -> None:
    """Launch a run. Default path enqueues via ports/queue; temporal starts a workflow."""
    if settings.orchestrator_backend == "temporal":
        _submit_temporal(payload)
        return
    from ports.queue import enqueue_run
    enqueue_run(payload, handler)


def _submit_temporal(payload: dict) -> None:
    """Start (fire-and-forget) a Temporal workflow for this run. Lazy so temporalio is optional.

    The workflow + activity live in orchestration/temporal_app.py and simply wrap the SAME
    api.main._run_job, so the run logic is shared with the in-process path — Temporal only adds
    durability/retry/visibility around it. Requires `pip install temporalio` and a running Temporal
    server (temporal_host); the worker side is started by `python -m worker --temporal`.
    """
    import asyncio

    from temporalio.client import Client  # lazy import (optional dependency)
    from orchestration.temporal_app import RunSuiteWorkflow

    async def _go():
        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        await client.start_workflow(
            RunSuiteWorkflow.run,
            payload,
            id=f"run-{payload.get('run_id')}",
            task_queue=settings.temporal_task_queue,
        )

    asyncio.run(_go())
