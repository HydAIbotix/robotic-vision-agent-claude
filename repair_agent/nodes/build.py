"""BUILD node — `npm run build` (tsc -b + vite build). The real validation gate."""
from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import CODEBASE_DIR, _npm_cmd, _run
from repair_agent.state import RepairAgentState


def build_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    emit(rid, "build", "running")
    build = _run([_npm_cmd(), "run", "build"], CODEBASE_DIR)
    status = "done" if build["ok"] else "failed"
    emit(rid, "build", status, **build)
    stages = {**state.get("stages", {}), "build": {"status": status, **build}}
    return {"success": bool(build["ok"]), "build_ok": bool(build["ok"]), "stages": stages}
