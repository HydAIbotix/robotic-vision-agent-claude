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
from test_runner.plan_normalize import normalize_captured_reuse
from test_runner.state import TestRunnerState
from test_runner.prompts import PLAN_FROM_MAP, PARSE_TEST_CASE


def _apply_captured_reuse_norm(plan: dict, app_map: dict | None, where: str) -> dict:
    """Deterministic net: canonicalise reuse-of-captured-value payments to [deterministic completion
    tap] + [enter+complete vision] (tap the charted completion button FIRST, then live-vision enters
    the captured value and confirms — matches the proven TC-E2E-001 order). See plan_normalize.
    Idempotent — safe to run on generated AND cached plans (also FLIPS a prior wrong-order plan).
    Logs each canonicalisation so a developer can see it in the run output."""
    try:
        plan, notes = normalize_captured_reuse(plan, app_map)
        for n in notes:
            print(f"  [PLAN] ⚠ captured-value reuse fix ({where}): {n}")
    except Exception as exc:   # never let a normalisation bug break planning
        print(f"  [PLAN] captured-value normalisation skipped ({where}): {exc}")
    return plan


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

    # ── Scope the map to THIS test's kiosk(s) ─────────────────────────────────
    # The planner must see ONLY the app(s) this test touches, or it plans against the wrong screens.
    #   • single-kiosk test → one id  → a VPS test never gets RPS's login screen
    #   • cross-kiosk E2E   → several → BOTH apps are visible so the RPS flow can be planned instead
    #                                    of collapsing into one un-plannable vision_required step
    # Scope here — the one planning choke point — so it's correct for every run shape, including mixed
    # multi-kiosk runs where state carries the full combined map.  No-op for a legacy single-app map.
    # tc["kiosk_ids"] is stamped by _execute_run (falls back to [kiosk_id]).
    app_ids = tc.get("kiosk_ids") or ([tc["kiosk_id"]] if tc.get("kiosk_id") else [])
    if app_map and app_ids:
        app_map = app_map_store.scoped_to_apps(app_map, app_ids)

    _scope_note = f" (scoped to kiosk(s) {app_ids})" if app_ids else ""
    print(f"\n  [PLAN] {tc['test_id']}: selecting a plan tier{_scope_note}")

    # ── Tier 1: cache lookup ──────────────────────────────────────────────────
    # Attempt even when app_map is None: a pure AGV-movement test (move/wait/check_state, no
    # screens) has a valid cached plan that needs no app_map. version_hash(None) → "no_map",
    # matching what /tc-plan computed, and is_valid trusts a plan when there is no map to check.
    map_version = app_map_store.version_hash(app_map)
    cached = plan_cache.load(tc["test_id"], tc["steps_raw"], map_version, tc.get("expected_results_raw", ""))
    if cached and plan_cache.is_valid(cached, app_map):
        n = len(cached.get("steps") or [])
        print(f"  [PLAN] TIER-1 (plan cache): HIT — reusing cached {n}-step plan (0 LLM calls)")
        # Apply the captured-reuse net even on a cache HIT so an OLD cached plan that bypassed a
        # reused value (e.g. paid with a charted mock-card button instead of entering the issued
        # card) is corrected at run time without forcing a regenerate. Idempotent on good plans.
        cached = _apply_captured_reuse_norm(cached, app_map, "cache")
        _print_plan(cached)
        return {
            "structured_plan":   cached,
            "planned_steps":     [],   # not used in Tier-1/2 path
            "credential_scenario": cached.get("credential_scenario", "valid"),
        }
    if cached:
        print(f"  [PLAN] TIER-1 (plan cache): STALE — cached plan references elements no longer in "
              f"the app map → moving to TIER-2 (re-plan)")
    else:
        print(f"  [PLAN] TIER-1 (plan cache): MISS — no cached plan → moving to TIER-2 (re-plan)")

    # ── Tier 2: re-plan from element inventory (text-only LLM call) ───────────
    if app_map:
        print(f"  [PLAN] TIER-2 (re-plan from app map): generating plan from element inventory (1 text LLM call)…")
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
                print(f"  [PLAN] ⚠ {_n_converted} empty-element tap(s) converted → vision_required (Tier-3 will handle them)")

            # Deterministic net: a reuse-of-captured-value completion tap that never enters the value
            # (and whose screen has no charted input for it) → vision_required so live vision enters it.
            plan = _apply_captured_reuse_norm(plan, app_map, "tier2")

            n = len(plan.get("steps") or [])
            print(f"  [PLAN] TIER-2: SUCCESS — produced {n}-step plan (cached for reuse)")
            _print_plan(plan)
            # Cache for future runs
            plan_cache.save(plan, tc["test_id"], tc["steps_raw"], map_version, tc.get("expected_results_raw", ""))
            return {
                "structured_plan":   plan,
                "planned_steps":     [],
                "credential_scenario": plan.get("credential_scenario", "valid"),
            }
        print(f"  [PLAN] TIER-2: FAILED — Claude returned no usable plan → falling back to TIER-3 (legacy vision)")
    else:
        print(f"  [PLAN] No app map available → skipping TIER-1/TIER-2, using TIER-3 (legacy vision)")

    # ── Tier 3: legacy format (no app_map, or Tier-2 failed) ─────────────────
    print(f"  [PLAN] TIER-3 (legacy vision agent): planning from screenshots (1 text call + per-step image calls at run time)")
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
        elif action == "capture":
            print(f"    {i:>2}. capture {step.get('capture_as', step.get('element_id',''))} ← {step.get('element_id','')} [{step.get('screen_id','')}]")
        elif action in ("move", "navigate", "move_base"):
            print(f"    {i:>2}. move  AGV → {step.get('target', step.get('device',''))}")
        elif action == "wait":
            print(f"    {i:>2}. wait  {step.get('seconds', step.get('duration_s', 0))}s")
        elif action in ("check_state", "state"):
            print(f"    {i:>2}. check_state {step.get('target','agv')} == {step.get('expected_state','(any)')}")
        elif action == "vision_required":
            print(f"    {i:>2}. [VISION REQUIRED] {step.get('description','')}")
        else:
            print(f"    {i:>2}. {action}  {step.get('description','')}")
