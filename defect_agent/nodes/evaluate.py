"""
Evaluation Node — analyse each failed test case and produce a structured
assessment: severity, affected area, root cause summary.

One Claude call covers ALL failures in a single prompt so the model can
detect cross-cutting patterns (e.g. "login fails on every TC that touches
the payment flow") and assign severity with that context in mind.
"""
import json
from langchain_core.messages import HumanMessage
from vision_agent.llm import get_llm
from defect_agent.state import DefectAgentState

_EVAL_PROMPT = """You are a QA defect analyst reviewing kiosk test failures.

Run ID : {run_id}
Kiosk  : {kiosk_id}

Failed test cases:
{failures_json}

For EACH failed test case produce a JSON object with these exact keys:
  "test_id"       : the test ID from the input
  "severity"      : one of critical | high | medium | low
                    critical  = kiosk unusable / blocking core flow
                    high      = major feature broken, workaround unlikely
                    medium    = feature partially broken or edge-case failure
                    low       = cosmetic / minor inconvenience
  "priority"      : P1 (critical) | P2 (high) | P3 (medium) | P4 (low)
  "root_cause"    : 1-2 sentence technical root cause derived from the failed steps
  "affected_area" : the UI screen / feature area affected (e.g. "Login", "Payment")

Respond with a JSON array only — no markdown, no commentary.
"""


def evaluation_node(state: DefectAgentState) -> dict:
    failed = state["failed_results"]
    if not failed:
        return {"evaluations": []}

    failures_text = json.dumps(
        [
            {
                "test_id":       r.get("test_id"),
                "summary":       r.get("summary"),
                "vision_summary": r.get("vision_summary", ""),
                "failed_steps":  [
                    s for s in (r.get("step_results") or [])
                    if not s.get("success", True)
                ],
            }
            for r in failed
        ],
        indent=2,
    )

    prompt = _EVAL_PROMPT.format(
        run_id=state["run_id"],
        kiosk_id=state["kiosk_id"],
        failures_json=failures_text,
    )

    llm  = get_llm()
    raw  = llm.invoke([HumanMessage(content=prompt)]).content.strip()

    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        evaluations = json.loads(raw)
        if not isinstance(evaluations, list):
            evaluations = [evaluations]
    except Exception as e:
        print(f"  [DEFECT:EVAL] JSON parse error: {e} — raw: {raw[:200]}")
        evaluations = []

    print(f"  [DEFECT:EVAL] {len(evaluations)} evaluations produced")
    return {"evaluations": evaluations}
