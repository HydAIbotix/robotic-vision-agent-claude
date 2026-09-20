"""DIAGNOSE node — the ONE Claude call. Ask Opus 4.8 for a single minimal find/replace patch.

Delegates to `propose_patch` (which also carries the deterministic demo fallback).
"""
from dataclasses import asdict

from vision_agent.config import settings
from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import propose_patch, diagnose_tool_label
from repair_agent.state import RepairAgentState


def diagnose_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    tool = diagnose_tool_label()
    emit(rid, "diagnose", "running", tool=tool, note="starting…")

    # Stream progress for the slow (local) model so the UI shows elapsed time, not a frozen 'working…'.
    def _progress(label, elapsed, budget):
        emit(rid, "diagnose", "running", tool=tool, note=f"{tool} diagnosing… {elapsed}s / {budget}s")

    patch = propose_patch(
        state["failure"], state.get("context", ""),
        timeout=settings.repair_diagnose_timeout_s,
        cancel_check=lambda: canceller.is_cancelled(rid),
        on_progress=_progress,
        hits=state.get("hits"),   # P0b: lets DIAGNOSE verify the patch targets the failed symptom
    )
    patch_dict = asdict(patch)
    # Report the tool that ACTUALLY produced the patch (source may differ from the selected primary
    # if a backup ran, or be the deterministic demo rule).
    done_tool = {
        "local": f"Llama · {patch_dict.get('model', '')}",
        "claude": f"Claude · {patch_dict.get('model', '')}",
        "demo-fallback": "demo fallback rule",
    }.get(patch_dict.get("source", ""), tool)
    emit(rid, "diagnose", "done", patch=patch_dict, tool=done_tool)
    stages = {**state.get("stages", {}), "diagnose": {"status": "done", "patch": patch_dict, "tool": done_tool}}
    return {"patch": patch_dict, "stages": stages}
