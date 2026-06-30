"""
run_vision_step — execute a test case against the live kiosk.

Tier 1 / 2  (structured_plan is set):
  Execute steps directly from the plan's stored pixel coordinates.
  Screen verification via DOM get_dom_screen_id() — zero LLM calls.
  Falls through to Tier 3 only when a step fails at runtime.

Tier 3  (structured_plan is None, or Tier 1/2 runtime failure):
  Legacy VisionAgent path — capture screenshot → Claude vision → coordinates.
  Also used in demo mode where DOM screen detection is unavailable.
"""
import time
from pathlib import Path
from vision_agent import robot
from vision_agent.config import settings
from test_runner.state import TestRunnerState, TestResult
from test_runner import broadcaster


# ── Tier 1/2: structured plan execution ──────────────────────────────────────

def _resolve_credentials(value: str, credential_scenario: str, credentials: dict) -> str:
    """Substitute any remaining credential placeholders (belt-and-suspenders)."""
    creds   = credentials.get(credential_scenario, credentials.get("valid", {}))
    valid   = credentials.get("valid",   {})
    invalid = credentials.get("invalid", {})
    return (
        value
        .replace("{valid_email}",      valid.get("email",       ""))
        .replace("{valid_password}",   valid.get("password",    ""))
        .replace("{invalid_email}",    invalid.get("email",     ""))
        .replace("{invalid_password}", invalid.get("password",  ""))
    )


def _load_device_map() -> dict[str, dict]:
    """Load device positions from DB keyed by alias (e.g. 'TVM'). Returns {} on error."""
    try:
        from api.database import SessionLocal
        from api import models as _models
        db = SessionLocal()
        try:
            devices = db.query(_models.DeviceConfig).all()
            return {d.alias: {"pos_x": d.pos_x, "pos_y": d.pos_y, "pos_theta": d.pos_theta}
                    for d in devices}
        finally:
            db.close()
    except Exception:
        return {}


def _execute_structured_plan(plan: dict, credentials: dict, run_id: str = "", test_id: str = "") -> tuple[list[dict], str]:
    """
    Execute every step in the structured plan using stored pixel coordinates.

    Returns (step_results, outcome).
    outcome is "passed" when all steps succeed, "failed" on the first failure.

    Screen verification uses DOM get_dom_screen_id() — no screenshot or LLM call.
    If the DOM is unavailable (demo/real backend), verify steps are skipped with a warning.
    """
    step_results: list[dict] = []
    scenario = plan.get("credential_scenario", "valid")
    device_map  = _load_device_map()
    current_dev: str | None = None

    for i, step in enumerate(plan.get("steps") or [], 1):
        # ── device routing — move robot before first step on each new device ──
        step_dev = step.get("device")
        if step_dev and step_dev != current_dev:
            dev_cfg = device_map.get(step_dev)
            if dev_cfg:
                print(f"    [ROBOT] Moving to device '{step_dev}' @ ({dev_cfg['pos_x']}, {dev_cfg['pos_y']}, {dev_cfg['pos_theta']}°)")
                robot.move_to_position(dev_cfg["pos_x"], dev_cfg["pos_y"], dev_cfg["pos_theta"])
                time.sleep(0.8)  # allow robot/camera to settle
            else:
                print(f"    [ROBOT] Device '{step_dev}' not in device map — skipping move")
            current_dev = step_dev
        action = step.get("action", "")

        # ── verify ────────────────────────────────────────────────────────────
        if action == "verify":
            expected = step.get("expected_screen", "")
            desc     = step.get("description", "")
            if expected:
                actual = robot.get_dom_screen_id()
                if not actual:
                    # expected_screen is set but DOM detection returned nothing —
                    # treat as FAIL so false positives are caught (auto-pass masked real failures before)
                    print(f"    {i:>2}. verify  expected={expected!r}  [DOM detection returned empty — FAIL]")
                    sr = {"step": f"verify: {desc}", "success": False, "note": f"DOM screen detection unavailable (expected: {expected})", "method": "dom", "expected_screen": expected, "actual_screen": ""}
                    step_results.append(sr)
                    if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                    return step_results, "failed"
                success = (actual == expected)
                status  = "PASS" if success else "FAIL"
                print(f"    {i:>2}. verify  expected={expected!r}  actual={actual!r}  [{status}]")
                sr = {"step": f"verify: {desc}", "success": success, "expected_screen": expected, "actual_screen": actual, "method": "dom"}
                step_results.append(sr)
                if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                if not success:
                    return step_results, "failed"
            continue

        # ── tap ───────────────────────────────────────────────────────────────
        if action == "tap":
            px  = step.get("px", 0)
            py  = step.get("py", 0)
            eid = step.get("element_id", "")
            sid = step.get("screen_id", "")
            print(f"    {i:>2}. tap   {eid!r} @ ({px},{py})  [{sid}]")
            robot.tap(px, py)
            time.sleep(0.5)
            sr = {"step": f"tap: {eid} @ ({px},{py})", "success": True, "method": "app_map", "screen_id": sid, "element_id": eid}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── type ──────────────────────────────────────────────────────────────
        if action == "type":
            value = _resolve_credentials(step.get("value", ""), scenario, credentials)
            print(f"    {i:>2}. type  {value!r}")
            robot.type_text(value, clear_first=True)
            time.sleep(0.3)
            sr = {"step": f"type: {value[:30]}", "success": True, "method": "app_map"}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── unknown ───────────────────────────────────────────────────────────
        print(f"    {i:>2}. [UNKNOWN action={action!r}] — skipped")

    return step_results, "passed"


# ── Demo mode: pre-built screenshot sequence (legacy) ────────────────────────

_NAV: list[tuple[str, object]] = [
    ("sign_in_button",              ("products", "login")),
    ("sign_in",                     ("products", "login")),
    ("add_to_cart",                 "product_added_to_cart"),
    ("cart_checkout",               "cart"),
    ("checkout_button",             "cart"),
    ("cart_icon",                   "cart"),
    ("proceed_to_card_payment",     "payment"),
    ("proceed_to_payment",          "payment"),
    ("use_mock_approval",           "success"),
    ("mock_approval",               "success"),
    ("view_order_history",          "order_history"),
    ("order_history_button",        "order_history"),
    ("go_to_home",                  "products"),
    ("home_button",                 "products"),
    ("sign_out",                    "login"),
    ("logout",                      "login"),
]


def _demo_sequence(
    planned_steps: list[str],
    demo_screens: dict,
    start_screen: str,
    credential_scenario: str,
) -> list[str]:
    current = start_screen
    seq: list[str] = []
    for step in planned_steps:
        action, _, target = step.partition(": ")
        if action.strip() == "tap":
            t = target.strip().lower()
            for fragment, dest in _NAV:
                if fragment in t or t in fragment:
                    if isinstance(dest, tuple):
                        next_screen = dest[0] if credential_scenario == "valid" else dest[1]
                    else:
                        next_screen = dest
                    if next_screen in demo_screens:
                        current = next_screen
                    break
        seq.append(demo_screens.get(current, ""))
    return seq


# ── Tier 3: legacy VisionAgent path ──────────────────────────────────────────

def _run_tier3(state: TestRunnerState, tc: dict) -> dict:
    """Legacy path — hands off to the full VisionAgent (screenshot + Claude vision per step)."""
    from vision_agent.agent import create_agent
    from vision_agent.state import VisionAgentState

    planned_steps       = state["planned_steps"]
    demo_screens        = state.get("demo_screens") or {}
    credential_scenario = state.get("credential_scenario", "valid")
    app_map             = state.get("app_map")
    start_screen        = (app_map or {}).get("entry_screen", "login")

    if settings.robot_backend == "playwright":
        robot.reset_to_entry()
        save_path  = str(Path(settings.screenshots_dir) / f"start_{tc['test_id']}_{int(time.time())}.png")
        Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
        start_image = robot.capture_screen(save_path)["image_path"]
        print(f"  [TIER-3] Initial screenshot: {start_image}")
    else:
        start_image = demo_screens.get(start_screen, state.get("start_image", ""))
        demo_seq    = _demo_sequence(planned_steps, demo_screens, start_screen, credential_scenario)
        robot.set_demo_screens(demo_seq)

    agent = create_agent()
    initial: VisionAgentState = {
        "task_description":  tc["summary"],
        "image_path":        start_image,
        "screen_analysis":   None,
        "planned_steps":     planned_steps,
        "current_step_idx":  0,
        "step_results":      [],
        "retry_count":       0,
        "screen_history":    [],
        "decision_tree":     {},
        "outcome":           "running",
        "summary":           "",
        "error_message":     None,
    }
    return agent.invoke(initial)


# ── Node entry point ─────────────────────────────────────────────────────────

def run_vision_step(state: TestRunnerState) -> dict:
    tc                  = state["current_tc"]
    structured_plan     = state.get("structured_plan")
    credentials         = state.get("credentials") or {}
    credential_scenario = state.get("credential_scenario", "valid")
    run_id              = state.get("run_id", "")

    print(f"\n  {'─'*55}")
    print(f"  [RUN] {tc['test_id']}  —  {tc['summary']}")

    # Broadcast test-case start so the UI can show which TC is currently running
    if run_id:
        broadcaster.emit(run_id, {
            "event":    "test_started",
            "run_id":   run_id,
            "test_id":  tc["test_id"],
            "summary":  tc["summary"],
            "total_steps": len((structured_plan or {}).get("steps") or []),
        })

    # ── Tier 1 / 2: execute from structured plan (app_map coordinates) ────────
    if structured_plan and settings.robot_backend == "playwright":
        print(f"  [RUN] Tier-1/2 — executing {len(structured_plan.get('steps') or [])} steps from app_map (0 LLM calls)")

        # Reset to app entry and wait for the SPA to finish rendering
        # before the first DOM screen check (verify step).
        robot.reset_to_entry()
        time.sleep(1.2)

        step_results, outcome = _execute_structured_plan(structured_plan, credentials, run_id=run_id, test_id=tc["test_id"])
        passed = sum(1 for r in step_results if r["success"])

        # On failure: optionally fall through to Tier 3 for diagnosis
        # For now: record failure and move on (keeps execution fast).
        print(f"\n  [RESULT] {tc['test_id']}  {outcome.upper()}  ({passed}/{len(step_results)} steps passed)")

        test_result: TestResult = {
            "test_id":        tc["test_id"],
            "summary":        tc["summary"],
            "outcome":        outcome,
            "step_results":   step_results,
            "vision_summary": f"Executed via app_map ({outcome})",
        }
        return {"test_results": [*(state.get("test_results") or []), test_result]}

    # ── Tier 3: VisionAgent (screenshot + Claude vision per step) ─────────────
    tier = "Tier-3 vision-agent"
    if structured_plan and settings.robot_backend != "playwright":
        tier = "Tier-3 vision-agent (structured plan not used — non-playwright backend)"
    print(f"  [RUN] {tier}")

    result = _run_tier3(state, tc)

    step_results = result.get("step_results") or []
    passed  = sum(1 for r in step_results if r["success"])
    outcome = result.get("outcome", "failed")
    print(f"\n  [RESULT] {tc['test_id']}  {outcome.upper()}  ({passed}/{len(step_results)} steps passed)")
    if result.get("screen_history"):
        print(f"           Journey: {' -> '.join(result['screen_history'])}")

    test_result = {
        "test_id":        tc["test_id"],
        "summary":        tc["summary"],
        "outcome":        outcome,
        "step_results":   step_results,
        "vision_summary": result.get("summary", ""),
    }
    return {"test_results": [*(state.get("test_results") or []), test_result]}
