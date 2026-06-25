"""
parse_steps — send the raw test case text to Claude; receive a structured
planned_steps list (["tap: X", "type: Y", ...]) ready for the vision agent.
"""
import json
from langchain_core.messages import HumanMessage
from vision_agent.llm import get_llm
from app_map.store import prompt_summary
from test_runner.state import TestRunnerState
from test_runner.prompts import PARSE_TEST_CASE


def parse_steps(state: TestRunnerState) -> dict:
    tc   = state["current_tc"]
    creds = state.get("credentials") or {}
    valid   = creds.get("valid",   {})
    invalid = creds.get("invalid", {})

    prompt = PARSE_TEST_CASE.format(
        test_id=tc["test_id"],
        summary=tc["summary"],
        steps_raw=tc["steps_raw"],
        expected_results_raw=tc["expected_results_raw"],
        app_map_summary=prompt_summary(state.get("app_map")),
        valid_email=valid.get("email",    "tester@kiosk.local"),
        valid_password=valid.get("password", "Password123"),
        invalid_email=invalid.get("email",  "baduser@example.com"),
        invalid_password=invalid.get("password", "WrongPass!"),
    )

    llm = get_llm()
    raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)

    planned_steps: list[str]  = data.get("planned_steps") or []
    credential_scenario: str  = data.get("credential_scenario", "valid")

    print(f"\n  [PARSE] {len(planned_steps)} steps planned  (credentials: {credential_scenario})")
    for i, s in enumerate(planned_steps, 1):
        print(f"    {i:>2}. {s}")

    return {
        "planned_steps":       planned_steps,
        "credential_scenario": credential_scenario,
    }
