"""RCA node — the ROOT-CAUSE ANALYSIS agent (the FIRST of the two Auto-Repair agents).

A SEPARATE agent from the code-fixing agent. It reads ONLY the design/requirements docs + the test-case
workbook (never source code) and decides the root-cause CATEGORY:
  • code_bug     → the code is at fault; localise the suspect area + search terms and hand them to the fixer.
  • spec_bug     → the design/requirements are wrong; STOP (don't patch code to satisfy a broken spec).
  • test_invalid → the test itself is wrong; STOP (don't patch code to satisfy a wrong test).

The gate is CONSERVATIVE (only a HIGH-confidence spec/test verdict stops), and any error/timeout degrades
to a code_bug passthrough, so the code-fixing agent still runs exactly as before — no regression for the
default Claude + Chroma path. Screenshots of the failed steps are attached when the model is Claude.
"""
from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import run_rca, diagnose_tool_label
from repair_agent.state import RepairAgentState
from vision_agent.config import settings


def rca_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)

    # RCA disabled → cheap passthrough (the pipeline goes straight to the code-fixing agent, as before).
    if not settings.repair_rca_phase:
        emit(rid, "rca", "done", note="RCA agent disabled — proceeding to the code-fixing agent.",
             verdict="skipped")
        stages = {**state.get("stages", {}), "rca": {"status": "done", "verdict": "skipped"}}
        return {"rca": {"ran": False, "verdict": "skipped"}, "rca_query": "", "rca_stop": False, "stages": stages}

    tool = diagnose_tool_label()
    emit(rid, "rca", "running", tool=tool, note="RCA agent reading design docs + test case…")

    def _progress(label, elapsed, budget):
        emit(rid, "rca", "running", tool=tool, note=f"RCA agent analysing… {elapsed}s / {budget}s")

    rca = run_rca(
        state["failure"],
        images=state.get("images"),
        cancel_check=lambda: canceller.is_cancelled(rid),
        on_progress=_progress,
    )

    # A stop verdict (spec/test bug) is a legitimate, useful outcome — surface it clearly.
    note = {
        "code_bug": f"Code bug — localised to {rca.get('suspect') or 'the app code'}; handing to the code-fixing agent.",
        "spec_bug": "Spec/requirements bug — STOPPING (fixing code cannot satisfy a broken spec).",
        "test_invalid": "Test case is invalid — STOPPING (the test contradicts the design).",
        "environment": "Environment/infrastructure issue (network, page load, service, config) — STOPPING "
                       "(a code patch cannot fix it; fix the environment and re-run).",
        "unknown": "Root cause is unclear from the evidence — proceeding to the code-fixing agent to verify against the source.",
        "skipped": "RCA agent produced no verdict — proceeding to the code-fixing agent.",
    }.get(rca.get("verdict", "code_bug"), "Proceeding to the code-fixing agent.")

    emit(rid, "rca", "done", tool=tool, verdict=rca.get("verdict"), confidence=rca.get("confidence"),
         rationale=rca.get("rationale"), suspect=rca.get("suspect"), stop=rca.get("stop"),
         hits=rca.get("hits"), note=note)
    stages = {**state.get("stages", {}), "rca": {
        "status": "done", "verdict": rca.get("verdict"), "confidence": rca.get("confidence"),
        "rationale": rca.get("rationale"), "suspect": rca.get("suspect"), "stop": rca.get("stop"),
        "hits": rca.get("hits"), "tool": tool,
    }}

    out = {"rca": rca, "rca_query": rca.get("rca_query", ""), "rca_stop": bool(rca.get("stop")), "stages": stages}
    if rca.get("stop"):
        # Record a terminal, human-readable result so the dashboard shows WHY no patch was produced.
        out["success"] = False
        out["error"] = (f"RCA agent halted the repair: {rca.get('verdict')} "
                        f"({rca.get('confidence')} confidence). {rca.get('rationale', '')}").strip()
    return out
