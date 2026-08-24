"""DIAGNOSE node — the ONE Claude call. Ask Opus 4.8 for a single minimal find/replace patch.

Delegates to `propose_patch` (which also carries the deterministic demo fallback).
"""
from dataclasses import asdict

from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import propose_patch
from repair_agent.state import RepairAgentState


def diagnose_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    emit(rid, "diagnose", "running")
    patch = propose_patch(state["failure"], state.get("context", ""))
    patch_dict = asdict(patch)
    emit(rid, "diagnose", "done", patch=patch_dict)
    stages = {**state.get("stages", {}), "diagnose": {"status": "done", "patch": patch_dict}}
    return {"patch": patch_dict, "stages": stages}
