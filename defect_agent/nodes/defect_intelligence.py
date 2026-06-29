"""
Defect Intelligence Node — generate fully structured defect records for each
evaluated failure, including a concise title, reproduction steps, probable fix,
and a placeholder JIRA key/URL.

Each defect is a self-contained dict ready to be persisted by the publisher.
"""
import json
import hashlib
from langchain_core.messages import HumanMessage
from vision_agent.llm import get_llm
from defect_agent.state import DefectAgentState

_DEFECT_PROMPT = """You are a senior QA engineer writing a defect report for a kiosk
touchscreen automation failure.

Test case context:
{tc_context}

Evaluation:
  Root cause    : {root_cause}
  Affected area : {affected_area}
  Severity      : {severity}

Write a professional defect record with these exact JSON keys:
  "title"              : concise one-line defect title (max 80 chars)
  "description"        : 2-4 sentence description of the defect and its impact
  "steps_to_reproduce" : numbered list as a single string (use \\n between steps)
  "probable_fix"       : 1-2 sentence actionable suggestion for the developer

Respond with a single JSON object only — no markdown, no commentary.
"""


def _make_jira_key(run_id: str, test_id: str, idx: int) -> tuple[str, str]:
    """Generate a deterministic dummy JIRA key and URL for the defect."""
    short_hash = hashlib.md5(f"{run_id}:{test_id}".encode()).hexdigest()[:4].upper()
    key = f"KIOSK-{short_hash}{idx + 1}"
    url = f"https://jira.example.com/browse/{key}"
    return key, url


def defect_intelligence_node(state: DefectAgentState) -> dict:
    evaluations   = state.get("evaluations") or []
    failed        = {r["test_id"]: r for r in state["failed_results"]}
    run_id        = state["run_id"]
    defects: list[dict] = []

    for idx, ev in enumerate(evaluations):
        test_id  = ev.get("test_id", "")
        result   = failed.get(test_id, {})
        steps_raw = result.get("step_results") or []

        tc_context = json.dumps({
            "test_id":       test_id,
            "summary":       result.get("summary", ""),
            "vision_summary": result.get("vision_summary", ""),
            "step_results":  steps_raw,
        }, indent=2)

        prompt = _DEFECT_PROMPT.format(
            tc_context=tc_context,
            root_cause=ev.get("root_cause", ""),
            affected_area=ev.get("affected_area", ""),
            severity=ev.get("severity", "medium"),
        )

        llm = get_llm()
        raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()

        try:
            detail = json.loads(raw)
        except Exception as e:
            print(f"  [DEFECT:INTEL] JSON parse error for {test_id}: {e}")
            detail = {
                "title":              f"Test failure: {test_id}",
                "description":        result.get("vision_summary", "Test case failed."),
                "steps_to_reproduce": "1. Run the test case\n2. Observe the failure",
                "probable_fix":       "Investigate the failed steps and fix the underlying issue.",
            }

        jira_key, jira_url = _make_jira_key(run_id, test_id, idx)
        evidence = [
            s.get("screenshot") for s in steps_raw
            if s.get("screenshot") and not s.get("success", True)
        ]

        defect = {
            "run_id":             run_id,
            "test_id":            test_id,
            "title":              detail.get("title", f"Failure: {test_id}"),
            "description":        detail.get("description", ""),
            "steps_to_reproduce": detail.get("steps_to_reproduce", ""),
            "root_cause":         ev.get("root_cause", ""),
            "probable_fix":       detail.get("probable_fix", ""),
            "severity":           ev.get("severity", "medium"),
            "priority":           ev.get("priority", "P3"),
            "jira_key":           jira_key,
            "jira_url":           jira_url,
            "evidence":           evidence,
        }
        defects.append(defect)
        print(f"  [DEFECT:INTEL] {jira_key} — {defect['title'][:60]}")

    return {"defects": defects}
