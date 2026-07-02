"""
parse_steps — build a structured execution plan for a test case.

Three-tier strategy (tried in order, stops at first success):

  Tier 1 — Cache hit (0 LLM calls)
    Load test_plans/<test_id>_<hash>.json.
    Validate that every element in the plan still exists in the current app_map.
    If valid → use it directly.

  Tier 2 — Re-plan from app_map (1 text LLM call, no image)
    Build the element inventory from app_map and ask Claude to produce a
    structured plan [{action, screen_id, element_id, px, py, value, ...}].
    Cache the result.  Active when: cache miss, stale plan, or app_map changed.

  Tier 3 — Fallback to legacy vision-agent format (1 text LLM call + N image calls later)
    Used when app_map is not available.  Produces the old ["tap: X", "type: Y"]
    planned_steps list consumed by run_vision_step's legacy path.
"""
import json
from langchain_core.messages import HumanMessage
from vision_agent.llm import get_llm, invoke_json
from app_map import store as app_map_store
from test_runner import plan_cache
from test_runner.state import TestRunnerState
from test_runner.prompts import PLAN_FROM_MAP, PARSE_TEST_CASE


def _resolve(value: str, credential_scenario: str, credentials: dict) -> str:
    creds = credentials.get(credential_scenario, credentials.get("valid", {}))
    return (
        value
        .replace("{valid_email}",      credentials.get("valid",   {}).get("email",    ""))
        .replace("{valid_password}",   credentials.get("valid",   {}).get("password", ""))
        .replace("{invalid_email}",    credentials.get("invalid", {}).get("email",    ""))
        .replace("{invalid_password}", credentials.get("invalid", {}).get("password", ""))
    )


def _tier2_plan(tc: dict, app_map: dict, credentials: dict) -> dict | None:
    """Ask Claude to generate a structured plan from the element inventory (text-only)."""
    valid   = credentials.get("valid",   {})
    invalid = credentials.get("invalid", {})
    inventory = app_map_store.element_inventory_for_prompt(app_map)

    prompt = PLAN_FROM_MAP.format(
        test_id=tc["test_id"],
        summary=tc["summary"],
        description=tc.get("description", "") or "(none provided)",
        preconditions=tc.get("preconditions", "") or "(none provided — derive every step's prerequisites yourself from the app_map and observed prerequisites)",
        steps_raw=tc["steps_raw"],
        expected_results_raw=tc["expected_results_raw"],
        element_inventory=inventory,
        valid_email=valid.get("email",       ""),
        valid_password=valid.get("password", ""),
        invalid_email=invalid.get("email",       ""),
        invalid_password=invalid.get("password", ""),
    )

    plan = invoke_json(get_llm(), [HumanMessage(content=prompt)], default=None, label="plan_from_map")
    if not plan or not isinstance(plan, dict):
        return None

    # Surface the planner's precondition reasoning (why steps were expanded the way they were).
    reasoning = (plan.get("reasoning") or "").strip()
    if reasoning:
        print("  [PARSE] Planner reasoning:")
        for line in reasoning.splitlines():
            print(f"         {line}")

    # Resolve any credential placeholders that slipped through into type steps.
    scenario = plan.get("credential_scenario", "valid")
    for step in plan.get("steps") or []:
        if step.get("action") == "type" and step.get("value"):
            step["value"] = _resolve(step["value"], scenario, credentials)

    return plan


def _tier3_plan(tc: dict, app_map: dict | None, credentials: dict) -> tuple[list[str], str]:
    """Legacy vision-agent planned_steps format — used when app_map is absent."""
    valid   = credentials.get("valid",   {})
    invalid = credentials.get("invalid", {})

    prompt = PARSE_TEST_CASE.format(
        test_id=tc["test_id"],
        summary=tc["summary"],
        steps_raw=tc["steps_raw"],
        expected_results_raw=tc["expected_results_raw"],
        app_map_summary=app_map_store.prompt_summary(app_map),
        valid_email=valid.get("email",       ""),
        valid_password=valid.get("password", ""),
        invalid_email=invalid.get("email",       ""),
        invalid_password=invalid.get("password", ""),
    )

    llm = get_llm()
    raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)
    return data.get("planned_steps") or [], data.get("credential_scenario", "valid")


def parse_steps(state: TestRunnerState) -> dict:
    tc          = state["current_tc"]
    app_map     = state.get("app_map")
    credentials = state.get("credentials") or {}

    # ── Tier 1: cache lookup ──────────────────────────────────────────────────
    if app_map:
        map_version = app_map_store.version_hash(app_map)
        cached = plan_cache.load(tc["test_id"], tc["steps_raw"], map_version)
        if cached and plan_cache.is_valid(cached, app_map):
            n = len(cached.get("steps") or [])
            print(f"\n  [PARSE] Tier-1 cache hit — {n} steps  (0 LLM calls)")
            _print_plan(cached)
            return {
                "structured_plan":   cached,
                "planned_steps":     [],   # not used in Tier-1/2 path
                "credential_scenario": cached.get("credential_scenario", "valid"),
            }
        if cached:
            print(f"\n  [PARSE] Cache stale (element missing from current app_map) — re-planning")
        else:
            print(f"\n  [PARSE] No cache entry — generating plan")

    # ── Tier 2: re-plan from element inventory (text-only LLM call) ───────────
    if app_map:
        print(f"  [PARSE] Tier-2 — planning from app_map element inventory (1 text call)")
        plan = _tier2_plan(tc, app_map, credentials)
        if plan:
            # Safety net: any tap with an empty element_id is a hallucinated step —
            # convert it to a vision_required sentinel so Tier-3 picks up from there.
            _n_converted = 0
            for step in plan.get("steps") or []:
                if step.get("action") == "tap" and not step.get("element_id"):
                    step["action"]      = "vision_required"
                    step["description"] = step.get("description") or "complete remaining test steps via vision"
                    _n_converted += 1
            if _n_converted:
                print(f"  [PARSE] ⚠ {_n_converted} empty-element tap(s) converted → vision_required")

            n = len(plan.get("steps") or [])
            print(f"  [PARSE] Tier-2 plan: {n} steps")
            _print_plan(plan)
            # Cache for future runs
            plan_cache.save(plan, tc["test_id"], tc["steps_raw"], map_version)
            return {
                "structured_plan":   plan,
                "planned_steps":     [],
                "credential_scenario": plan.get("credential_scenario", "valid"),
            }
        print(f"  [PARSE] Tier-2 failed — falling back to Tier-3")

    # ── Tier 3: legacy format (no app_map, or Tier-2 failed) ─────────────────
    print(f"  [PARSE] Tier-3 — legacy vision-agent format (1 text call + image calls during execution)")
    planned_steps, scenario = _tier3_plan(tc, app_map, credentials)
    print(f"  [PARSE] {len(planned_steps)} steps (credential_scenario={scenario!r})")
    for i, s in enumerate(planned_steps, 1):
        print(f"    {i:>2}. {s}")
    return {
        "structured_plan":   None,   # signals run_vision_step to use legacy path
        "planned_steps":     planned_steps,
        "credential_scenario": scenario,
    }


def _print_plan(plan: dict) -> None:
    for i, step in enumerate(plan.get("steps") or [], 1):
        action = step.get("action", "?")
        if action == "tap":
            print(f"    {i:>2}. tap   {step.get('element_id','')} @ ({step.get('px',0)},{step.get('py',0)})  [{step.get('screen_id','')}]")
        elif action == "type":
            val = step.get("value", "")
            print(f"    {i:>2}. type  {val[:40]!r}")
        elif action == "verify":
            print(f"    {i:>2}. verify screen={step.get('expected_screen','')}  — {step.get('description','')}")
        elif action == "vision_required":
            print(f"    {i:>2}. [VISION REQUIRED] {step.get('description','')}")
