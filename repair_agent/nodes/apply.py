"""GUARD + APPLY nodes.

`guard_node` refuses to touch a repo that is mid-merge / mid-rebase / has conflicts (so a fix branch
can never sweep in a whole merge). `apply_node` writes the single-occurrence patch to the live app.
"""
import os

from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import (
    CODEBASE_DIR, RepairPatch, apply_patch, _repo_blocked_reason,
)
from repair_agent.state import RepairAgentState


def guard_node(state: RepairAgentState) -> dict:
    """Set state['blocked'] (+ a failed apply stage) when the repo isn't safe to edit."""
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    blocked = _repo_blocked_reason()
    if blocked:
        msg = (f"Repository is not in a clean state: {blocked}. Skipped applying the fix and the PR "
               f"so the branch can't be corrupted. Clean the repo (see the message), then re-run.")
        emit(rid, "apply", "failed", reason=blocked, observation=msg)
        stages = {**state.get("stages", {}),
                  "apply": {"status": "failed", "reason": blocked, "observation": msg}}
        return {"blocked": blocked, "error": msg, "stages": stages}
    return {"blocked": ""}


def apply_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    emit(rid, "apply", "running")
    patch = RepairPatch(**state["patch"])
    try:
        target = apply_patch(patch)
    except Exception as exc:
        # apply_patch raises when the fix can't be applied (e.g. "Patch find-text was not found in the
        # target file", or an ambiguous multi-file match). Mark the stage FAILED so the UI doesn't show
        # "Apply Fix" stuck on Working; then re-raise so run_repair records the repair as incomplete.
        emit(rid, "apply", "failed", observation=str(exc))
        raise
    rel = os.path.relpath(str(target), str(CODEBASE_DIR)).replace("\\", "/")
    emit(rid, "apply", "done", file=rel)
    stages = {**state.get("stages", {}), "apply": {"status": "done", "file": rel}}
    return {"target": str(target), "stages": stages}
