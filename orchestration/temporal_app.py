"""
Temporal workflow + activities for running a test suite durably (optional).

Two shapes, chosen by the payload:
  • Single suite  — one `run_suite` activity runs the whole filter (the default; matches every other
    backend exactly).
  • Fan-out       — when `payload["fanout_test_ids"]` is a non-empty list, the workflow runs ONE
    `run_test` activity PER test id, IN PARALLEL (the Step-Functions "fan-out N Executors" shape).
    Each shard is an INDEPENDENT sub-run (its own run_id `<parent>::<test_id>`), so parallel shards
    never race on the shared run state / global settings — the isolation Temporal expects when it
    distributes activities across worker replicas.

Both wrap the SAME run handler (api.main._run_job), so Temporal only adds durability / retries /
visibility around unchanged run logic. Import requires the optional `temporalio` SDK.

Run the worker:  python -m worker --temporal   (hosts these on TEMPORAL_TASK_QUEUE)

⚠️ Fan-out shards write per-test results under their own sub-run. Run the shards on SEPARATE worker
replicas (the normal Temporal deployment) or with activity concurrency = 1 on a single worker, since
the suite executor mutates some process-global settings while running.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow


@activity.defn(name="run_suite")
async def run_suite_activity(payload: dict) -> None:
    """Execute the whole suite (one filter). Delegates to the shared handler in a thread so it does
    not block the activity's event loop."""
    from api.main import _run_job
    await asyncio.to_thread(_run_job, payload)


@activity.defn(name="run_test")
async def run_test_activity(payload: dict, test_id: str) -> None:
    """Execute ONE test as an independent sub-run. Narrows the request to this test id and gives it a
    unique run_id so parallel shards don't collide."""
    from api.main import _run_job

    shard = dict(payload)
    shard["run_id"] = f"{payload.get('run_id')}::{test_id}"
    req = dict(payload.get("req") or {})
    req["filter_tc"] = test_id
    shard["req"] = req
    await asyncio.to_thread(_run_job, shard)


@workflow.defn
class RunSuiteWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> None:
        fanout = payload.get("fanout_test_ids") or []
        if fanout:
            # Fan-out: one activity per test, in parallel. Temporal schedules them across workers.
            await asyncio.gather(*[
                workflow.execute_activity(
                    run_test_activity,
                    args=[payload, tid],
                    start_to_close_timeout=timedelta(minutes=30),
                )
                for tid in fanout
            ])
        else:
            await workflow.execute_activity(
                run_suite_activity,
                payload,
                start_to_close_timeout=timedelta(hours=2),
            )
