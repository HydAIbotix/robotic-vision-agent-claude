"""PR node — local branch + pathspec commit + diff; auto-push/open when auto_pr and the build is green."""
from pathlib import Path

from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import RepairPatch, open_pull_request, prepare_pr
from repair_agent.state import RepairAgentState


def prepare_pr_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    emit(rid, "pr", "running")
    patch = RepairPatch(**state["patch"])
    target = Path(state["target"])
    pr = prepare_pr(patch, target, state["failure"], state.get("test_id", ""),
                    branch_suffix=state.get("branch_suffix", ""))
    if state.get("auto_pr") and pr.get("prepared") and state.get("build_ok"):
        pr["opened"] = open_pull_request(pr["branch"], pr["base"], pr["title"], pr["body"])
    status = "done" if pr.get("prepared") else "warn"
    emit(rid, "pr", status, **pr)
    stages = {**state.get("stages", {}), "pr": {"status": status, **pr}}
    return {"stages": stages}
