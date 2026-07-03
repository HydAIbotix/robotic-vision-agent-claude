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
    last_screenshot: str = ""  # updated after every step; used by subsequent verify steps

    def _cap(tag: str, idx: int) -> str:
        """Capture the current screen into the run's screenshot folder. Returns the
        path, or '' on failure.  Works on every backend (playwright, real, demo)."""
        try:
            Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
            p = str(Path(settings.screenshots_dir) / f"step{idx:02d}_{tag}_{test_id}_{int(time.time()*1000)}.png")
            robot.capture_screen(p)
            return p
        except Exception as exc:
            print(f"         [screenshot capture failed: {exc}]")
            return ""

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
            # playwright only: ensure a current screenshot exists even for a leading
            # verify step (no preceding tap) so the UI has evidence and a text-mismatch
            # message can read the actual on-screen value via Claude.
            if settings.robot_backend == "playwright" and not last_screenshot:
                last_screenshot = _cap("verify", i)
            expected      = step.get("expected_screen", "")
            # Accept both field names: PLAN_FROM_MAP emits "expected_text", the UI planner
            # (_TC_PLAN_PROMPT) emits "expected_value".  Reading only one silently skipped the
            # content check (e.g. an order total), letting wrong-amount tests pass.
            expected_text = step.get("expected_text") or step.get("expected_value")
            # Optional anchor: the specific app_map element that holds the asserted value.
            # When present, validation reads THAT element's live text (field-exact, no LLM).
            value_element_id = step.get("value_element_id", "")
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
                value_element_id=value_element_id,
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
                    value_element_id=value_element_id,
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

            _vshot = last_screenshot if settings.robot_backend == "playwright" else ""
            sr = {
                "step": f"verify: {desc}",
                "success": success,
                "expected_screen": expected,
                "actual_screen": actual,
                "expected_text": expected_text,
                "method": method,
                "observation": observation,
                "screenshot_after": _vshot,
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
            tap_result = robot.tap(px, py)
            time.sleep(0.5)
            # Fresh post-tap image for the next verify step.
            #   playwright → cheap browser screenshot; also saved as step evidence (UI).
            #   real robot → the /screen/click completion already returns the camera frame
            #                (tap_result["image_path"]); reuse it — no extra /capture cycle.
            #                Not surfaced as UI step evidence for real-robot runs.
            #   demo       → verify always passes; no image needed.
            after_shot = ""
            if settings.robot_backend == "playwright":
                after_shot = _cap("after", i)
                if after_shot:
                    last_screenshot = after_shot
            else:
                tap_img = (tap_result or {}).get("image_path", "")
                if not tap_img and settings.robot_backend != "demo":
                    # Real backend returned no frame — fall back to an explicit capture.
                    ts = int(time.time() * 1000)
                    snap_path = str(Path(settings.screenshots_dir) / f"tap_{test_id}_{ts}.png")
                    Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
                    robot.capture_screen(snap_path)
                    tap_img = snap_path
                if tap_img:
                    last_screenshot = tap_img
            # Navigation prediction from app_map transitions
            sc_data  = (app_map or {}).get("screens", {}).get(sid, {})
            predicted = (sc_data.get("transitions") or {}).get(eid)
            if predicted:
                print(f"         → nav prediction: '{predicted}' [app_map transitions]")
            sr = {"step": f"tap: {eid} @ ({px},{py})", "success": True, "method": "app_map",
                  "screen_id": sid, "element_id": eid, "screenshot_after": after_shot}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── type ──────────────────────────────────────────────────────────────
        if action == "type":
            value = _resolve_credentials(step.get("value", ""), scenario, credentials)
            print(f"    {i:>2}. type  {value!r}")
            robot.type_text(value, clear_first=True)
            time.sleep(0.3)
            after_shot = ""
            if settings.robot_backend == "playwright":
                after_shot = _cap("after", i)
                if after_shot:
                    last_screenshot = after_shot
            sr = {"step": f"type: {value[:30]}", "success": True, "method": "app_map",
                  "screenshot_after": after_shot}
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

    # Credentials strictly from test configuration (never hard-coded) — appended so a
    # re-plan during recovery uses the real values instead of inventing an email.
    _creds = state.get("credentials") or {}
    _sc    = _creds.get(credential_scenario) or _creds.get("valid") or {}
    if _sc.get("email") or _sc.get("password"):
        _cred_hint = (
            f"\n\nCredentials (from test configuration) — use these EXACT values for any "
            f"login; never invent or guess:\n  email: {_sc.get('email','')}\n  password: {_sc.get('password','')}"
        )
    else:
        _cred_hint = "\n\nNo login credentials are configured — do NOT attempt to log in."

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
        "task_description":  tc["summary"] + _cred_hint,
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

    Called for ANY Tier 1/2 failure (coverage gap, vision sentinel, or a failed
    verify/tap).  Deliberately does NOT call reset_to_entry — resetting would drop the
    browser back on the login screen and throw away the authenticated session.  Instead
    it takes a fresh screenshot of the CURRENT screen and continues toward the objective.

    The agent receives: the objective, what already ran, an instruction not to navigate
    back to login, and the configured credentials (used only if a login is unavoidable).

    Returns a result dict with step_results = completed_steps + Tier-3 steps,
    so the final audit trail covers the entire test.
    """
    from vision_agent.agent import create_agent
    from vision_agent.state import VisionAgentState

    structured_plan   = state.get("structured_plan") or {}
    plan_steps_all    = structured_plan.get("steps") or []
    n_remaining       = max(0, len(plan_steps_all) - start_step_idx)

    # Credentials come from the test configuration (intake payload / global config) via
    # state — never hard-coded here.  They are handed to the agent so that IF a login is
    # genuinely unavoidable it uses the real configured values instead of inventing one.
    _creds    = state.get("credentials") or {}
    _scenario = state.get("credential_scenario", "valid")
    _sc       = _creds.get(_scenario) or _creds.get("valid") or {}
    _c_email  = _sc.get("email", "")
    _c_pw     = _sc.get("password", "")
    if _c_email or _c_pw:
        cred_hint = (
            f"\n\nCredentials (from test configuration) — use these EXACT values, and only "
            f"if a login screen is unavoidable; never invent, guess, or use an example:\n"
            f"  email: {_c_email}\n  password: {_c_pw}"
        )
    else:
        cred_hint = "\n\nNo login credentials are configured — do NOT attempt to log in."

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
        f"Continue from the current screen.\n\n"
        f"CHECK PRECONDITIONS before acting: a previous step may have failed because a "
        f"precondition was not met. If you intend to proceed to checkout/payment but the "
        f"cart shows 0 items, you must FIRST add an item — if the product has a quantity "
        f"'+'/increment control, tap it to set quantity to at least 1, then tap Add to Cart, "
        f"and only then proceed. If a form's required fields are empty, fill them first. "
        f"Never re-tap a button that just failed without first changing the state that "
        f"caused it to fail."
    )
    task_description += cred_hint

    # ── Objective screen: the last verify target in the plan (e.g. "order_result") ──
    # Used to know when the multi-screen flow is actually complete.
    goal_screen = ""
    for st in reversed(plan_steps_all):
        if st.get("action") == "verify" and st.get("expected_screen"):
            goal_screen = st["expected_screen"]
            break

    def _dom() -> str:
        try:
            return robot.get_dom_screen_id() or ""
        except Exception:
            return ""

    # ── Advance across screens until the objective is reached ──────────────────
    # One VisionAgent invocation plans+executes a single screen-cluster (it stops when it
    # needs an element it cannot yet see).  A purchase spans several screens
    # (cart → payment → tap-card → result), so we loop: run the agent, and if it made
    # progress (the DOM screen changed) but the objective screen is not yet reached, run it
    # again from the new screen.  Bounded so a stuck flow cannot loop forever.
    #
    # Pass planned_steps=[] each time so plan_steps generates fresh, correctly-formatted
    # steps from the REAL screen (pre-populating from the structured plan would inject
    # hallucinated element names).
    agent = create_agent()
    all_t3_steps: list[dict] = []
    t3_result: dict = {}
    MAX_ITERS = 5
    for _it in range(MAX_ITERS):
        before_dom = _dom()
        if goal_screen and before_dom == goal_screen:
            print(f"  [TIER-3] Objective screen '{goal_screen}' reached — flow complete")
            break

        cap = str(Path(settings.screenshots_dir) / f"tier3_resume_{tc['test_id']}_{_it}_{int(time.time())}.png")
        screen_img = robot.capture_screen(cap)["image_path"]
        print(f"  [TIER-3] step-through {_it + 1}/{MAX_ITERS} — on '{before_dom or '?'}', planning from current screen")

        initial: VisionAgentState = {
            "task_description": task_description,
            "image_path":       screen_img,
            "screen_analysis":  None,
            "planned_steps":    [],
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
        all_t3_steps.extend(t3_result.get("step_results") or [])

        after_dom = _dom()
        if goal_screen and after_dom == goal_screen:
            print(f"  [TIER-3] step-through {_it + 1}: reached objective '{goal_screen}'")
            break
        if after_dom == before_dom:
            print(f"  [TIER-3] step-through {_it + 1}: no screen change (still '{after_dom or '?'}') — stopping")
            break
        print(f"  [TIER-3] step-through {_it + 1}: advanced '{before_dom or '?'}' -> '{after_dom or '?'}' — continuing")

    # Merge Tier 1/2 completed steps with all Tier-3 steps for a full audit trail
    all_steps = list(completed_steps) + all_t3_steps
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

        # On failure: hand off to Tier-3, ALWAYS resuming from the current screen.
        if outcome in ("failed", "vision_required"):
            last_sr = step_results[-1] if step_results else {}

            # Never reset to the entry/login screen mid-test.  A reset discards the
            # already-authenticated session and drops the browser back on login, forcing
            # the VisionAgent to re-authenticate — which caused the "logged out, then looped
            # on login" failure.  Whatever went wrong, the intelligent recovery is to look at
            # the screen we are ACTUALLY on and continue toward the objective from there.
            failed_idx = len(step_results) - 1
            if last_sr.get("method") in ("no_coordinates", "vision_required"):
                completed = step_results[:-1]   # sentinel artifact — not a real executed step
            else:
                completed = step_results         # keep the failed verify/tap in the audit trail
            t3_mode = "resume"
            print(
                f"\n  [RUN] Tier-1/2 stopped at step {failed_idx + 1} "
                f"(method={last_sr.get('method', '?')}) — resuming Tier-3 from current screen (no reset)"
            )
            t3_result = _run_tier3_continue(state, tc, completed, failed_idx)

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
