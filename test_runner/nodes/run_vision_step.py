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


def _execute_structured_plan(plan: dict, credentials: dict, run_id: str = "", test_id: str = "", app_map: dict = None) -> tuple[list[dict], str]:
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
    last_screenshot: str = ""  # updated after every tap; used by subsequent verify steps

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
            expected      = step.get("expected_screen", "")
            expected_text = step.get("expected_text")   # optional content check
            desc          = step.get("description", "")
            if not expected and not expected_text:
                # Nothing to verify — skip with a warning.
                print(f"    {i:>2}. verify  [no expected_screen/text — SKIPPED]  {desc!r}")
                sr = {"step": f"verify: {desc}", "success": True, "note": "no expected_screen in plan — check skipped", "method": "skipped"}
                step_results.append(sr)
                if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                continue

            # Build save path for camera-based backends (playwright ignores it)
            verify_save = ""
            if settings.robot_backend != "playwright":
                ts = int(time.time() * 1000)
                verify_save = str(Path(settings.screenshots_dir) / f"verify_{test_id}_{ts}.png")
                Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)

            # Use the 4-node validation pipeline (screen + text + Claude fallback).
            # Retry up to 2 times with 1 s delay when screen doesn't match — SPA
            # route changes can finish slightly after the first poll.
            from vision_agent.nodes.validate_pipeline import run_validate_pipeline
            vr = run_validate_pipeline(
                expected_screen=expected,
                expected_text=expected_text,
                step_description=desc,
                image_path=last_screenshot,
                app_map=app_map or {},
                backend=settings.robot_backend,
                save_path=verify_save,
            )

            _retry = 0
            while vr.get("success") is False and vr.get("method") == "screen_id" and _retry < 2:
                _retry += 1
                print(f"    {i:>2}. verify  retrying ({_retry}/2) — waiting 1 s for navigation to settle …")
                time.sleep(1.0)
                if settings.robot_backend != "playwright" and last_screenshot:
                    ts2 = int(time.time() * 1000)
                    verify_save = str(Path(settings.screenshots_dir) / f"verify_{test_id}_{ts2}.png")
                    robot.capture_screen(verify_save)
                    last_screenshot = verify_save
                vr = run_validate_pipeline(
                    expected_screen=expected,
                    expected_text=expected_text,
                    step_description=desc,
                    image_path=last_screenshot,
                    app_map=app_map or {},
                    backend=settings.robot_backend,
                    save_path=verify_save,
                )

            success = vr["success"]
            method  = vr.get("method", "unknown")
            actual  = vr.get("actual_screen", "")
            observation = vr.get("observation", "")
            note    = vr.get("note", "")

            # Verification gap — unknown screen, not a hard failure
            if success is None:
                print(f"    {i:>2}. verify  expected={expected!r}  [VERIFICATION GAP — {observation}]")
                sr = {
                    "step": f"verify: {desc}",
                    "success": True,     # don't fail the test over an uncharted screen
                    "note": note or observation,
                    "method": "verification_gap",
                    "expected_screen": expected,
                    "actual_screen": actual,
                    "verification_gap": True,
                }
                step_results.append(sr)
                if run_id: broadcaster.emit(run_id, {
                    "event": "step_result", "run_id": run_id, "test_id": test_id,
                    "step_index": i, **sr,
                })
                continue

            status = "PASS" if success else "FAIL"
            text_tag = f"  text='{expected_text}'" if expected_text else ""
            print(f"    {i:>2}. verify  expected={expected!r}{text_tag}  actual={actual!r}  [{status}]  [{method}]")
            if not success:
                print(f"             {observation}")

            sr = {
                "step": f"verify: {desc}",
                "success": success,
                "expected_screen": expected,
                "actual_screen": actual,
                "expected_text": expected_text,
                "method": method,
                "observation": observation,
            }
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {
                "event": "step_result", "run_id": run_id, "test_id": test_id,
                "step_index": i, **sr,
            })
            if not success:
                return step_results, "failed"
            continue

        # ── tap ───────────────────────────────────────────────────────────────
        if action == "tap":
            px  = step.get("px", 0)
            py  = step.get("py", 0)
            eid = step.get("element_id", "")
            sid = step.get("screen_id", "")

            # (0,0) means the planner found no stored coordinates — the screen was
            # not in the app_map when the plan was generated.  Mark as failure so
            # Tier 3 takes over with Claude vision instead of tapping a dead pixel.
            if px == 0 and py == 0:
                print(f"    {i:>2}. tap   {eid!r} — no coordinates (screen not yet in app_map) → Tier-3 fallback")
                sr = {
                    "step": f"tap: {eid} @ (0,0)",
                    "success": False,
                    "method": "no_coordinates",
                    "note": f"No stored coordinates for '{eid}' — screen not explored; Tier-3 will retry with vision",
                }
                step_results.append(sr)
                if run_id:
                    broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                return step_results, "failed"

            print(f"    {i:>2}. tap   {eid!r} @ ({px},{py})  [{sid}]")
            robot.tap(px, py)
            time.sleep(0.5)
            # Capture screenshot after tap so verify steps have a fresh image
            if settings.robot_backend != "playwright":
                ts = int(time.time() * 1000)
                snap_path = str(Path(settings.screenshots_dir) / f"tap_{test_id}_{ts}.png")
                Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
                robot.capture_screen(snap_path)
                last_screenshot = snap_path
                # Navigation prediction from app_map transitions
                sc_data  = (app_map or {}).get("screens", {}).get(sid, {})
                predicted = (sc_data.get("transitions") or {}).get(eid)
                if predicted:
                    print(f"         → nav prediction: '{predicted}' [app_map transitions]")
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

        # ── vision_required sentinel — planner found an uncharted screen ──────
        if action == "vision_required":
            desc = step.get("description", "complete remaining test steps")
            print(f"    {i:>2}. [VISION REQUIRED] {desc}")
            sr = {
                "step": f"vision_required: {desc}",
                "success": None,
                "method": "vision_required",
                "description": desc,
            }
            step_results.append(sr)
            if run_id:
                broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            return step_results, "vision_required"

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


# ── Tier 3 (resume): continue from current screen without resetting ───────────

def _run_tier3_continue(
    state: TestRunnerState,
    tc: dict,
    completed_steps: list[dict],
    start_step_idx: int,
) -> dict:
    """
    Resume execution from wherever the browser is NOW using Claude vision.

    Called when Tier 1/2 stalls mid-test because a screen is not in the app_map
    (detected by a 0,0 tap).  Does NOT call reset_to_entry — takes a fresh
    screenshot of the current screen and runs only the remaining steps.

    The agent receives full context: what steps already passed, what remains,
    and an explicit instruction not to navigate back to login.

    Returns a result dict with step_results = completed_steps + Tier-3 steps,
    so the final audit trail covers the entire test.
    """
    from vision_agent.agent import create_agent
    from vision_agent.state import VisionAgentState

    structured_plan   = state.get("structured_plan") or {}
    plan_steps_all    = structured_plan.get("steps") or []
    n_remaining       = max(0, len(plan_steps_all) - start_step_idx)

    # Capture current screen — no reset
    save_path = str(
        Path(settings.screenshots_dir)
        / f"tier3_resume_{tc['test_id']}_{int(time.time())}.png"
    )
    Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
    current_image = robot.capture_screen(save_path)["image_path"]
    print(f"  [TIER-3] Handoff screenshot (no reset): {current_image}")

    # ── Task description: high-level goal only, NO step-by-step enumeration ────
    # The structured plan steps beyond this point were generated WITHOUT seeing
    # the actual screen (screen was not in the app_map), so they may be
    # hallucinated (e.g. "Start Card Reader Session" button that doesn't exist).
    # Give Claude the test OBJECTIVE and let it reason from the actual screenshot.
    # The plan_steps node will analyze the real screen elements and generate
    # correctly-formatted "tap: <label>" / "verify: <desc>" strings.
    task_description = (
        f"Test objective: {tc['summary']}\n\n"
        f"Context: The first {len(completed_steps)} of {len(plan_steps_all)} planned "
        f"steps have already been executed using stored UI coordinates. "
        f"The screenshot shows the CURRENT screen — you are mid-test with "
        f"approximately {n_remaining} step(s) remaining.\n\n"
        f"Your task: examine the screen visible in the screenshot and execute "
        f"whatever actions are needed to complete the test objective stated above.\n"
        f"IMPORTANT: identify buttons and elements from what you ACTUALLY SEE on "
        f"screen — do not assume button labels that might not exist. "
        f"Do NOT navigate back to login. Do NOT restart from the beginning. "
        f"Continue from the current screen."
    )

    # Pass planned_steps=[] so the plan_steps node runs and generates fresh steps
    # from the REAL screenshot.  Pre-populating from the structured plan would
    # inject hallucinated element names (wrong format + wrong labels) that cause
    # "Unknown action type" failures in execute.py.
    agent = create_agent()
    initial: VisionAgentState = {
        "task_description": task_description,
        "image_path":       current_image,
        "screen_analysis":  None,
        "planned_steps":    [],     # empty → plan_steps generates from real screenshot
        "current_step_idx": 0,
        "step_results":     [],
        "retry_count":      0,
        "screen_history":   [],
        "decision_tree":    {},
        "outcome":          "running",
        "summary":          "",
        "error_message":    None,
    }
    t3_result = agent.invoke(initial)

    # Merge Tier 1/2 completed steps with Tier 3 steps for a full audit trail
    t3_steps  = t3_result.get("step_results") or []
    all_steps = list(completed_steps) + t3_steps
    return {**t3_result, "step_results": all_steps}


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

    app_map = state.get("app_map") or {}

    # ── Tier 1 / 2: execute from structured plan (app_map coordinates) ────────
    # Works for ALL backends:
    #   playwright → DOM-based verify (get_dom_screen_id, no image/LLM)
    #   real robot → camera-based verify (reference screenshot comparison, no LLM)
    #   demo       → verify always passes (scripted demo path)
    if structured_plan:
        backend = settings.robot_backend
        print(f"  [RUN] Tier-1/2 [{backend}] — executing {len(structured_plan.get('steps') or [])} steps from app_map (0 LLM calls)")

        # Reset to app entry and wait for the SPA / camera to settle before
        # the first verify step.
        robot.reset_to_entry()
        time.sleep(1.2)

        step_results, outcome = _execute_structured_plan(
            structured_plan, credentials,
            run_id=run_id, test_id=tc["test_id"], app_map=app_map,
        )
        passed = sum(1 for r in step_results if r["success"])

        # On failure: route to the right Tier-3 mode based on failure reason.
        if outcome in ("failed", "vision_required"):
            last_sr = step_results[-1] if step_results else {}

            if last_sr.get("method") in ("no_coordinates", "vision_required"):
                # app_map coverage gap or vision sentinel — browser is already on the
                # correct screen; resume from here without resetting to entry.
                failed_idx  = len(step_results) - 1   # 0-indexed plan position
                completed   = step_results[:-1]        # drop the sentinel artifact
                t3_mode     = "resume"
                print(
                    f"\n  [RUN] app_map incomplete at step {failed_idx + 1} "
                    f"— resuming Tier-3 from current screen (no reset)"
                )
                t3_result = _run_tier3_continue(state, tc, completed, failed_idx)
            else:
                # Genuine failure (wrong screen, bad state) — restart from entry.
                t3_mode = "restart"
                print(
                    f"\n  [RUN] Tier-1/2 failed at step {len(step_results)} "
                    f"— restarting with Tier-3 VisionAgent"
                )
                t3_result = _run_tier3(state, tc)

            t3_steps   = t3_result.get("step_results") or []
            t3_outcome = t3_result.get("outcome", "failed")
            t3_passed  = sum(1 for r in t3_steps if r.get("success"))
            print(f"\n  [TIER-3/{t3_mode}] {tc['test_id']}  {t3_outcome.upper()}  ({t3_passed}/{len(t3_steps)} steps passed)")
            if t3_result.get("screen_history"):
                print(f"           Journey: {' -> '.join(t3_result['screen_history'])}")
            test_result: TestResult = {
                "test_id":        tc["test_id"],
                "summary":        tc["summary"],
                "outcome":        t3_outcome,
                "step_results":   t3_steps,
                "vision_summary": (
                    f"Tier-1/2 → Tier-3/{t3_mode} [{backend}] ({t3_outcome}): "
                    f"{t3_result.get('summary', '')}"
                ),
            }
            return {"test_results": [*(state.get("test_results") or []), test_result]}

        print(f"\n  [RESULT] {tc['test_id']}  {outcome.upper()}  ({passed}/{len(step_results)} steps passed)")

        test_result: TestResult = {
            "test_id":        tc["test_id"],
            "summary":        tc["summary"],
            "outcome":        outcome,
            "step_results":   step_results,
            "vision_summary": f"Executed via app_map [{backend}] ({outcome})",
        }
        return {"test_results": [*(state.get("test_results") or []), test_result]}

    # ── Tier 3: VisionAgent (screenshot + Claude vision per step) ─────────────
    print(f"  [RUN] Tier-3 vision-agent")

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
