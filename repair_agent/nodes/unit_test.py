"""TEST node — the 'unit test' gate (TypeScript type-check; the app ships no unit runner)."""
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import CODEBASE_DIR, _unit_test
from repair_agent.state import RepairAgentState


def unit_test_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    emit(rid, "test", "running")
    test = _unit_test(CODEBASE_DIR)
    status = "done" if test["ok"] else "warn"   # build below is the real gate; a tsc miss is a warn
    emit(rid, "test", status, **test)
    stages = {**state.get("stages", {}), "test": {"status": status, **test}}
    return {"stages": stages}
