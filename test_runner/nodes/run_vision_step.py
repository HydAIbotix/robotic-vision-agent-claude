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


# Substrings that mark a genuine ROBOT/HARDWARE fault (as opposed to a plan/coordinate miss). When a
# real-robot action fails for one of these reasons the arm may be stuck / unable to return home, so
# Tier-3 (which would drive the SAME arm) is pointless — the run STOPS instead (see run_vision_step).
_ROBOT_FAULT_MARKERS = (
    "did not land", "descend", "hover", "move_action", "moveit", "motion",
    "error_code", "unreachable", "not responding", "connection", "refused",
    "timed out", "timeout", "ptp", "planning",
)


def _is_robot_fault(reason: str) -> bool:
    """True when a failure reason looks like a physical robot fault (motion planning / reachability /
    API unreachable) rather than a coordinate or data issue that Tier-3 vision could recover."""
    r = (reason or "").lower()
    return any(m in r for m in _ROBOT_FAULT_MARKERS)


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
    """Load devices from DB keyed by alias (e.g. 'TVM').  Each entry carries the robot
    position plus the linked Kiosk-ID and that kiosk's URL (so a playwright run can switch
    the browser to the right app when the robot moves between devices).  Returns {} on error."""
    try:
        from api.database import SessionLocal
        from api import models as _models
        db = SessionLocal()
        try:
            kiosks = {k.kiosk_id: k for k in db.query(_models.KioskConfig).all()}
            out: dict[str, dict] = {}
            for d in db.query(_models.DeviceConfig).all():
                kc = kiosks.get(d.kiosk_id) if d.kiosk_id else None
                cfg = {
                    "pos_x": d.pos_x, "pos_y": d.pos_y, "pos_theta": d.pos_theta,
                    "kiosk_id": d.kiosk_id or "",
                    # AGV map position name for /base/goto target (decoupled from the kiosk_id join
                    # key). getattr for safety on rows written before the column existed. Blank →
                    # _agv_target falls back to kiosk_id.
                    "position_name": (getattr(d, "position_name", "") or ""),
                    "url": (kc.url if kc else "") or "",
                }
                out[d.alias] = cfg
                # Also index by kiosk_id: a re-planned move/device tag sometimes carries the bare
                # kiosk_id ("kiosk-2") instead of the device alias ("RPS") — LLM re-planning isn't
                # deterministic between the two forms. Indexing both makes the cross-app browser
                # switch resolve either way (device_map.get(...) is used unchanged by the move
                # handler and the device-routing block). Alias wins if it collides with an id.
                if d.kiosk_id and d.kiosk_id not in out:
                    out[d.kiosk_id] = cfg
            # Kiosks that have a URL but no device row — still switchable by bare kiosk_id.
            for kid, kc in kiosks.items():
                if kid not in out and (getattr(kc, "url", "") or ""):
                    out[kid] = {"pos_x": 0.0, "pos_y": 0.0, "pos_theta": 0.0,
                                "kiosk_id": kid, "position_name": "", "url": kc.url or ""}
            return out
        finally:
            db.close()
    except Exception:
        return {}


def _agv_target(dev_cfg: dict, fallback: str = "") -> str:
    """The named position to send as the /base/goto `target` for a device.

    Prefers the device's AGV-map `position_name` (the robotics team's map name, e.g.
    'kiosk-2-Aug-14-G37') when set; otherwise falls back to the `kiosk_id` (the historical
    behaviour — kiosk_id doubled as the AGV target), and finally to the caller's `fallback`
    (usually the raw alias/kiosk_id). Keeps the AGV position name decoupled from the kiosk_id
    join key so renaming AGV positions never disturbs exploration/plan/template joins."""
    return (str(dev_cfg.get("position_name") or "").strip()
            or str(dev_cfg.get("kiosk_id") or "").strip()
            or str(fallback or "").strip())


import re as _re

# Explicit AGV/base-movement intent in a test's raw steps. Requires a movement VERB near a
# device/kiosk/AGV/home token, so a UI phrase like "go to the products page" does NOT trigger a
# physical base move (no device token), while "Move the AGV to Kiosk-1" / "Navigate to RPS" does.
_AGV_MOVE_RE = _re.compile(
    r"\b(move|navigate|drive|travel|go|goto|head|return|proceed)\b[^.\n]{0,40}?"
    r"\b(agv|base|kiosk|device|station|home|dock|vps|rps|tvm|k[-_]?\d+|kiosk[-_]?\d+)\b",
    _re.IGNORECASE,
)
# Also catch object-first phrasings: "AGV to VPS", "drive the base back home".
_AGV_MOVE_RE2 = _re.compile(
    r"\b(agv|base)\b[^.\n]{0,30}?\b(to|home|back|goto|move|navigate|kiosk|dock)\b",
    _re.IGNORECASE,
)


def _test_wants_agv_move(tc: dict) -> bool:
    """True when a test's steps explicitly ask to physically move the AGV/base to a device/kiosk/home.

    Used to gate per-test AGV positioning: unless a test says so (or the gate is disabled via
    settings.agv_move_requires_explicit_step=False), the robot is left where it is (assumed already
    parked at the target kiosk). Explicit `move` plan steps are handled separately and always drive."""
    text = (tc.get("steps_raw") or "")
    return bool(_AGV_MOVE_RE.search(text) or _AGV_MOVE_RE2.search(text))


def _position_for_test(tc: dict) -> None:
    """Ensure the robot/browser is at THIS test's kiosk before it runs.

    A suite can mix independent tests that run on DIFFERENT kiosks (e.g. an RPS test then a VPS
    test). Per-run setup positions only the FIRST kiosk, so without per-test positioning the 2nd test
    runs on the 1st's app/device — observed live in playwright: a VPS test launched RPS and logged in.
      • playwright → point the browser at the test's kiosk URL (reset_to_entry then lands there;
        it also clears localStorage/sessionStorage so no prior login carries over). settings.kiosk_url
        is set once per run to the primary kiosk, so this per-test override is what fixes the mix.
      • real robot → drive the AGV to the test's kiosk device (same parity as playwright's app switch).
      • demo       → no-op.
    No-op when the kiosk can't be resolved."""
    kid = tc.get("kiosk_id", "")
    if not kid:
        return
    if settings.robot_backend == "playwright":
        url = (_load_device_map().get(kid) or {}).get("url", "")
        if url and url != settings.kiosk_url:
            print(f"  [RUN] Per-test kiosk '{kid}' → {url}  (was {settings.kiosk_url})")
            settings.kiosk_url = url
    elif settings.robot_backend == "real":
        # Do NOT auto-drive the AGV unless the test explicitly asks to move (or the gate is disabled).
        # A single-kiosk test (e.g. a sign-in) leaves the robot where the operator parked it; only a
        # test whose steps say move/go/navigate to a device/home drives the base here. Explicit `move`
        # plan steps still drive during execution regardless.
        if settings.agv_move_requires_explicit_step and not _test_wants_agv_move(tc):
            print(f"  [RUN] Per-test: NOT moving AGV for kiosk '{kid}' — no explicit move/go/navigate "
                  f"step in this test (robot assumed already at the kiosk)")
            return
        try:
            if hasattr(robot, "navigate_to_kiosk"):
                _agv = _agv_target(_load_device_map().get(kid) or {}, kid)
                print(f"  [RUN] Per-test: driving AGV to kiosk '{kid}' (AGV target '{_agv}') before the "
                      f"test (explicit move intent)")
                robot.navigate_to_kiosk(_agv)
        except Exception as exc:
            print(f"  [RUN] Per-test AGV move to '{kid}' failed (continuing): {exc}")


def _element_testid(app_map: dict, screen_id: str, element_id: str) -> str:
    """Return the DOM data-testid stored for an app_map element, or "" if none.

    Used to focus a form field by stable identity (immune to stale/state-dependent pixel coords)
    before typing. The explorer records `testid` on each element via DOM-correction / backfill.
    """
    if not (app_map and screen_id and element_id):
        return ""
    sc = (app_map.get("screens") or {}).get(screen_id) or {}
    for el in sc.get("elements") or []:
        if el.get("id") == element_id:
            return el.get("testid") or ""
    return ""


def _element_charted(app_map: dict, screen_id: str, element_id: str) -> bool:
    """True when `element_id` is a REAL element the explorer charted on `screen_id`.

    A Tier-2 (text-only) plan can invent an element that isn't actually on the screen — e.g. the RPS
    payment screen charts "Use Mock Card"/"Start Card Reader Session" buttons but NOT the mock-card
    number input (revealed only after clicking "Use Mock Card"), so the planner guesses an
    `mock_card_number_input` with fabricated coords. Tapping/typing at those guessed coords focuses
    nothing → the value goes nowhere (Ctrl+A selects the whole page) yet the step "succeeds". Callers
    use this to route such steps to Tier-3 vision instead. Conservative: returns True (don't disrupt)
    when we can't tell — no app_map, no ids, or a screen with no charted element list — so it fires
    ONLY when the screen IS charted but this specific element is absent (the true planner-guess case).
    """
    if not (app_map and screen_id and element_id):
        return True
    sc = (app_map.get("screens") or {}).get(screen_id) or {}
    els = sc.get("elements")
    if not els:
        return True
    return any(el.get("id") == element_id for el in els)


def _looks_like_value(val: str) -> bool:
    """True when a captured string is already a clean, reusable token (a card number, code,
    id, amount) rather than empty or a label-noisy blob. Multi-line / long / label-containing
    text (e.g. "Smart Card Loaded  Card Number: 9013  Balance: …") → prefer the vision read,
    which isolates the raw value."""
    v = (val or "").strip()
    if not v:
        return False
    if "\n" in v or len(v) > 32 or ":" in v:
        return False
    return True


def _capture_value_via_vision(image_path: str, name: str, description: str) -> str:
    """Read a single runtime value (e.g. an issued card number) from the current screen via
    Claude. Used when the plan's capture element is empty/mischarted — the value the test must
    reuse lives in a transient result element the explorer never charted. Returns ONLY the raw
    value (digits/code/id), or '' on any failure. Best-effort, one fast-LLM call."""
    if not image_path:
        return ""
    import base64, json
    from langchain_core.messages import HumanMessage
    from vision_agent.llm import get_fast_llm, detect_image_media_type
    from vision_agent.storage import get_storage
    try:
        _img = get_storage().load(image_path)
        b64 = base64.standard_b64encode(_img).decode()
        media_type = detect_image_media_type(_img)
        prompt = (
            f"A test needs to capture the value named '{name}' from this screen so it can be "
            f"reused in a later step.\nContext: {description or name}\n\n"
            f"Find that value on screen and return ONLY the raw value itself — just the "
            f"number/code/id/text with NO label, prefix, or surrounding words (e.g. return "
            f"'9013', not 'Card Number: 9013'). If it is genuinely not visible, return empty.\n"
            f'Return ONLY valid JSON: {{ "value": "<raw value or empty>" }}'
        )
        llm = get_fast_llm()
        msg = HumanMessage(content=[
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64},
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt},
        ])
        raw = llm.invoke([msg]).content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        return str(json.loads(raw).get("value", "")).strip()
    except Exception as e:
        print(f"    [capture] vision read error: {e}")
        return ""


def _inline_vision_fast(desc: str, captured: dict, run_id: str, test_id: str,
                        step_index: int) -> tuple[list[dict], bool]:
    """FAST inline path: ONE Claude-vision call returns the concrete UI actions to complete the
    sub-task (type the known value(s) into their field, tap the confirm/pay button), then execute
    them directly. This replaces the full VisionAgent (analyze→plan→execute→validate per step, with
    a re-analyze+re-plan before every tap — ~6 slow Opus calls ≈ 100 s) for the common case where we
    already HAVE the data and just need to enter it and submit (the observed E2E step-19 stall).

    Two hard-won safeties (both backend-agnostic — playwright browser screenshot AND the real robot
    /screen/click camera frame, coordinates go straight to robot.tap()/type_text() via _norm_to_px):

      1. ENTER-BEFORE-SUBMIT GUARD.  A "type" action only focuses+enters when it has a valid field
         center; a controlled (React) input silently drops text typed into an UNFOCUSED field. If the
         vision model returns a "type" with no/zero center — or skips the type and returns only the
         submit tap — the value never lands, yet the submit still fires and the app shows "enter a
         value". So we NEVER tap the submit button unless the required value actually got entered;
         if it didn't, we re-plan the ENTRY once (asking for the field center + value) before submitting.

      2. STALE-ERROR TOLERANCE.  Some apps keep a prior attempt's error banner on screen until the
         corrected input is re-submitted. A pre-existing error is therefore NOT proof of failure — we
         enter the correct value and tap submit anyway, then judge by the RESULT after the submit
         (handed to the structured verify step). We never re-enter a value and just watch a stale error
         without clicking submit.

    Returns (step_results, advanced). advanced=False → caller falls back to the full VisionAgent."""
    import base64, io, json
    from PIL import Image
    from langchain_core.messages import HumanMessage
    from vision_agent.llm import get_fast_llm, detect_image_media_type
    from vision_agent.storage import get_storage
    from vision_agent.nodes.analyze import _norm_to_px

    # Vision-derived coords are in the CAPTURED IMAGE's pixel space (camera frame on the real backend),
    # so tap them via tap_image_point → the real backend sends them straight to the Click API instead of
    # re-scaling app_map viewport→camera (which would double-scale a camera-space point). Playwright's
    # image IS the viewport, so it clicks the same pixel. Falls back to tap() if a backend lacks it.
    def _tap_img(px: int, py: int):
        return getattr(robot, "tap_image_point", robot.tap)(px, py)

    def _dom() -> str:
        try:
            return robot.get_dom_screen_id() or ""
        except Exception:
            return ""

    def _emit(sr: dict) -> None:
        if run_id:
            broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id,
                                      "test_id": test_id, "step_index": step_index, **sr})

    provided_vals = {k: str(v) for k, v in (captured or {}).items() if str(v).strip()}
    desc_l = (desc or "").lower()
    # Does this sub-task involve ENTERING a value? True if the description asks to enter/type/fill and
    # we hold a captured value for it, or (decided per-attempt) if the model itself returns a type.
    desc_wants_entry = any(w in desc_l for w in ("enter", "type", "input", "fill", "re-enter", "reenter"))
    llm = get_fast_llm()
    all_steps: list[dict] = []
    correction_note = ""

    for attempt in range(1, 3):   # at most 2 vision attempts (re-plan the entry once)
        Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
        cap = str(Path(settings.screenshots_dir)
                  / f"inline_fast_{test_id}_{step_index}_{attempt}_{int(time.time())}.png")
        try:
            img_path = robot.capture_screen(cap)["image_path"]
        except Exception:
            return all_steps, False
        before = _dom()

        try:
            image_bytes = get_storage().load(img_path)
            img_w, img_h = Image.open(io.BytesIO(image_bytes)).size
            b64 = base64.standard_b64encode(image_bytes).decode()
            media_type = detect_image_media_type(image_bytes)
            vals = "\n".join(f"  {k} = {v}" for k, v in provided_vals.items())
            vals_block = ("\nValues to enter (use EXACTLY, never invent):\n" + vals) if vals else ""
            retry_block = (f"\nIMPORTANT — RETRY: {correction_note}\n") if correction_note else ""
            prompt = (
                "Complete this sub-task on the kiosk screen shown in the screenshot.\n"
                f"Sub-task: {desc}{vals_block}{retry_block}\n\n"
                "Return the concrete UI actions to complete it, IN ORDER, as JSON:\n"
                '{ "actions": [ {"kind":"type","label":"<field>","center":[x,y],"value":"<text>"}, '
                '{"kind":"tap","label":"<button>","center":[x,y]} ], "verified": false }\n'
                "Rules:\n"
                "- center = the element's location in THIS screenshot (pixels, or 0-1 normalized). For "
                "EVERY \"type\" action you MUST give the input field's center so it can be focused before "
                "typing — text typed into an unfocused field is silently lost.\n"
                "- To enter a value: emit a \"type\" action (field center AND the value) FOLLOWED BY the "
                "\"tap\" on the confirm/pay/submit button. NEVER return a submit tap without the preceding "
                "type that fills the required value.\n"
                "- Use the EXACT value(s) provided above; never invent a value.\n"
                "- STALE ERROR: an error/warning already visible may be LEFT OVER from a PREVIOUS failed "
                "attempt (some apps keep it until the corrected value is re-submitted). Do NOT treat a "
                "pre-existing error as failure and do NOT stop at it — enter the correct value and tap "
                "submit; the result is re-checked AFTER the submit.\n"
                "- ALREADY SATISFIED: if this sub-task is a VERIFICATION/observation and the information it "
                'asks to confirm is ALREADY visible on the current screen, return {"actions": [], '
                '"verified": true} — do NOT re-enter a value or re-tap a button that has already produced '
                "the result shown (e.g. the balance/transaction is already displayed).\n"
                '- If the field/button genuinely NEEDED for the sub-task is NOT visible, return {"actions": '
                '[], "verified": false}.\n'
                "Return ONLY the JSON."
            )
            msg = HumanMessage(content=[
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64},
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prompt},
            ])
            raw = llm.invoke([msg]).content.strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip()
            data = json.loads(raw) or {}
            actions = data.get("actions") or []
            already_verified = bool(data.get("verified"))
        except Exception as e:
            print(f"    [VISION-FAST] error → full agent fallback: {e}")
            return all_steps, False

        if not actions:
            # Distinguish "already satisfied" (a verification sub-task whose answer is already on screen)
            # from "can't do it here". The former must NOT re-enter values / re-tap and must NOT fall back
            # to the full agent (which would redundantly re-do the whole check — the observed VPS balance
            # being re-entered and re-checked after it was already displayed). Emit one clean verify step
            # and report done.
            if already_verified:
                print("    [VISION-FAST] sub-task already satisfied on the current screen — no action needed")
                sr = {"step": f"verify: {desc[:60]}", "success": True, "method": "vision_fast",
                      "observation": "Already satisfied on the current screen (no re-entry needed).",
                      "screenshot_after": img_path}
                all_steps.append(sr); _emit(sr)
                return all_steps, True
            print("    [VISION-FAST] no actions returned → full agent fallback")
            return all_steps, False

        type_actions   = [a for a in actions if (a.get("kind") or "").lower() == "type"]
        submit_actions = [a for a in actions if (a.get("kind") or "").lower() == "tap"]
        # An entry is REQUIRED if the model emitted a type, OR the sub-task text asks to enter a value
        # we actually hold. This catches the failure where the model skips the type and returns only
        # the submit tap (→ "clicked Pay without entering the card number").
        needs_value = bool(type_actions) or (bool(provided_vals) and desc_wants_entry)
        print(f"    [VISION-FAST] attempt {attempt}: {len(type_actions)} type + {len(submit_actions)} "
              f"tap action(s){' [entry required]' if needs_value else ''}")

        # ── 1) execute the type action(s), tracking whether the value actually LANDED ──────────────
        entered_ok = not needs_value          # nothing to enter → guard is satisfied
        entry_problem = "" if type_actions else "no type action was returned to enter the required value"
        batch: list[dict] = []
        entry_failed_hard = False
        for a in type_actions:
            value  = str(a.get("value", ""))
            center = a.get("center") or [0, 0]
            try:
                px, py = _norm_to_px([center[0], center[1]], img_w, img_h)
            except Exception:
                px, py = 0, 0
            label = a.get("label", "")
            if not value.strip():
                entry_problem = f"the type action for {label!r} had an empty value"
                continue
            if not (px and py):
                # No coordinates → we cannot reliably FOCUS the field, so the text would be dropped.
                # Do not type blindly; record the problem so the entry gets re-planned with a center.
                entry_problem = f"the type action for {label!r} had no field coordinates to focus"
                continue
            try:
                _tap_img(px, py); time.sleep(0.3)                     # focus the field first
                robot.type_text(value, clear_first=True); time.sleep(0.3)
            except Exception as exc:
                sr = {"step": f"type: {value[:30]} ({label})", "success": False, "method": "vision_fast",
                      "observation": f"{type(exc).__name__}: {exc}"}
                batch.append(sr); entry_failed_hard = True
                break
            entered_ok = True
            batch.append({"step": f"type: {value[:30]} ({label})", "success": True,
                          "method": "vision_fast", "screenshot_after": ""})

        for sr in batch:
            all_steps.append(sr); _emit(sr)
        if entry_failed_hard:
            return all_steps, False

        # ── 2) ENTER-BEFORE-SUBMIT GUARD ───────────────────────────────────────────────────────────
        # Never tap the submit button when the required value did not actually get entered. This is
        # the generic root-cause fix for "clicked Pay without entering the card number": submitting
        # an empty/unfocused field just yields the app's "enter a value" error. Re-plan the ENTRY once
        # (attempt 2), telling the model its entry didn't land and any on-screen error is stale.
        if needs_value and not entered_ok:
            print(f"    [VISION-FAST] entry did not land ({entry_problem or 'value not entered'}) "
                  f"— NOT tapping submit")
            if attempt == 1:
                correction_note = (
                    f"Your previous attempt did NOT enter the value into the field "
                    f"({entry_problem or 'the field was not focused'}), so the value is still missing and "
                    f"any error message on screen is STALE from that failed attempt. Return a \"type\" "
                    f"action WITH the field's pixel center AND the exact value, then the submit-button tap."
                )
                continue
            print("    [VISION-FAST] entry still did not land on retry → full agent fallback")
            return all_steps, False

        # ── 3) value entered (or none needed) → execute the submit tap(s) ──────────────────────────
        for a in submit_actions:
            center = a.get("center") or [0, 0]
            try:
                px, py = _norm_to_px([center[0], center[1]], img_w, img_h)
            except Exception:
                px, py = 0, 0
            label = a.get("label", "")
            try:
                _tap_img(px, py); time.sleep(0.6)
            except Exception as exc:
                sr = {"step": f"tap: {label}", "success": False, "method": "vision_fast",
                      "observation": f"{type(exc).__name__}: {exc}"}
                all_steps.append(sr); _emit(sr)
                return all_steps, False
            sr = {"step": f"tap: {label} @ ({px},{py})", "success": True, "method": "vision_fast",
                  "screenshot_after": ""}
            all_steps.append(sr); _emit(sr)

        time.sleep(0.8)
        after = _dom()
        # We entered the required value AND clicked submit. Whether the result is success (screen
        # advanced) or an in-place decline/error, the STRUCTURED verify step compares the result to
        # the EXPECTED outcome and concludes — i.e. it checks the result AFTER the click. So HAND
        # CONTROL BACK either way. Do NOT gate "done" on a forward transition: a valid terminal result
        # that stays in place (e.g. a payment DECLINE shows an error on the SAME screen) is not a
        # failure, and forcing a full-agent retry here makes the agent WANDER off the result screen and
        # never let the structured cross-app return trip run (observed: TC-E2E-002 drifted onto RPS
        # "Loyalty Rewards" and never switched back to VPS).
        if after and before and after != before:
            print(f"    [VISION-FAST] advanced {before} → {after} — resuming structured plan")
        else:
            print(f"    [VISION-FAST] entered value + submitted on '{after or before or '?'}' "
                  f"(in-place result, e.g. approved/declined) — resuming structured plan")
        return all_steps, True

    return all_steps, False


def _run_inline_vision(desc: str, captured: dict, credentials: dict, scenario: str,
                       app_map: dict, run_id: str, test_id: str, step_index: int) -> tuple[list[dict], bool]:
    """Run a BOUNDED Claude-vision segment for a single vision_required step, then hand control
    back to the structured executor.

    A vision_required step marks an UNCHARTED patch in the middle of an otherwise structured plan
    (e.g. the RPS payment screen has no card-number field in the app map). The old behaviour handed
    the ENTIRE remaining plan to Tier-3 — which then couldn't perform the later structured `move`
    back to the other kiosk, so a cross-kiosk E2E stalled on the wrong app after the purchase. This
    runs vision ONLY long enough to clear the uncharted patch (until the screen advances or stalls),
    then returns so the structured `move`/`verify`/balance-check steps run normally.

    Returns (segment_step_results, made_progress). made_progress=False → caller fails the step and
    falls through to the outer Tier-3 resume (the existing safety net)."""
    from vision_agent.agent import create_agent
    from vision_agent.state import VisionAgentState

    _sc = (credentials or {}).get(scenario) or (credentials or {}).get("valid") or {}
    if _sc.get("email") or _sc.get("password"):
        cred_hint = (f"\n\nOnly if a login is unavoidable, use these EXACT configured credentials — "
                     f"never invent:\n  email: {_sc.get('email','')}\n  password: {_sc.get('password','')}")
    else:
        cred_hint = "\n\nDo NOT attempt to log in."
    cap_hint = ""
    if captured:
        _cv = "\n".join(f"  {k} = {v}" for k, v in captured.items() if str(v).strip())
        if _cv:
            cap_hint = ("\n\nUse these EXACT values captured earlier in this test when a field needs "
                        "one (e.g. re-entering an issued card number); never invent one:\n" + _cv)
    task = (
        f"Sub-task on the CURRENT screen: {desc}\n\n"
        f"Identify buttons and fields from what you ACTUALLY SEE in the screenshot. Accomplish ONLY "
        f"this sub-task on the app currently shown, then stop. Do NOT navigate back to login, do NOT "
        f"restart the test, and do NOT begin a second/unrelated transaction." + cap_hint + cred_hint
    )

    def _dom() -> str:
        try:
            return robot.get_dom_screen_id() or ""
        except Exception:
            return ""

    seg_steps: list[dict] = []

    # ── FAST PATH: one vision call → type known value(s) + submit ──────────────
    # For the common "enter the captured value and complete the order" sub-task this is ~1 LLM call
    # instead of the full agent's ~6, fixing the observed multi-second stall on E2E step 19. Falls
    # back to the full agent only when it can't finish (playwright: screen didn't advance).
    fast_steps, fast_advanced = _inline_vision_fast(desc, captured, run_id, test_id, step_index)
    seg_steps.extend(fast_steps)
    if fast_advanced:
        return seg_steps, True

    agent = create_agent()
    made_progress = False
    MAX = 3
    for _it in range(MAX):
        before = _dom()
        cap = str(Path(settings.screenshots_dir) / f"inline_vis_{test_id}_{step_index}_{_it}_{int(time.time())}.png")
        Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
        img = robot.capture_screen(cap)["image_path"]
        print(f"    [VISION-INLINE] iter {_it+1}/{MAX} — on '{before or '?'}', running vision for sub-task")
        initial: VisionAgentState = {
            "task_description": task, "image_path": img, "screen_analysis": None,
            "planned_steps": [], "current_step_idx": 0, "step_results": [], "retry_count": 0,
            "screen_history": [], "decision_tree": {}, "outcome": "running", "summary": "",
            "error_message": None,
        }
        res = agent.invoke(initial)
        for s in (res.get("step_results") or []):
            seg_steps.append(s)
            if run_id:
                broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id,
                                          "test_id": test_id, "step_index": step_index, **s})
        after = _dom()
        # Stop as soon as the screen advances (uncharted patch cleared) — prevents the vision agent
        # from wandering into a second transaction while the structured `move` waits to run.
        if res.get("outcome") == "success" or (after and after != before):
            made_progress = True
            print(f"    [VISION-INLINE] progressed '{before or '?'}' → '{after or '?'}' — resuming structured plan")
            break
        if after == before:
            print(f"    [VISION-INLINE] no screen change (still '{after or '?'}') — ending segment")
            break
    return seg_steps, made_progress


def _make_robot_status_sink(run_id: str, test_id: str):
    """Build the real-time robot STATUS sink callback for a run.

    The real backend pushes live status ticks — AGV state + distance-remaining WHILE the base drives,
    arm state + elapsed WHILE the arm moves/types — to this callback DURING a blocking robot call, so
    the live monitor shows progress instead of a silent wait. Live-only (not in the robot event
    ring-buffer) so the post-step pull-flush never double-emits them. No-op when run_id is empty
    (CLI runs). Registered BEFORE the FIRST base move of a test — including the per-test positioning
    move (_position_for_test) — so the WHOLE drive streams every ~10s, not just the in-step moves."""
    def _sink(ev: dict) -> None:
        if not run_id:
            return
        st   = ev.get("state", "?")
        ep   = ev.get("endpoint", "")
        cmd  = ev.get("cmd_id", "")
        ts   = ev.get("response_time", "")
        ctrl = ev.get("controller", "")
        # Arm ticks carry an 'operation' label + elapsed; base ticks carry distance/nav feedback.
        if ep.startswith("/arm") or "operation" in ev:
            op = ev.get("operation") or ep
            el = ev.get("elapsed_s")
            msg = (f"[ROBOT ARM] {ts} {op}{(' (' + cmd + ')') if cmd else ''} @ {ctrl} — "
                   f"state='{st}'" + (f" ({el:.0f}s)" if isinstance(el, (int, float)) else ""))
        else:
            dist = ev.get("distance_remaining")
            msg = (f"[ROBOT AGV] {ts} {ep}{(' (' + cmd + ')') if cmd else ''} @ {ctrl} — "
                   f"state='{st}'" + (f", {dist:.2f}m remaining" if isinstance(dist, (int, float)) else ""))
        broadcaster.emit(run_id, {"event": "log", "run_id": run_id, "test_id": test_id,
                                  "message": msg, "robot_api": ev})
    return _sink


def _register_status_sink(run_id: str, test_id: str) -> None:
    """Register the live status sink on the active robot backend (real backend only implements
    set_event_sink; playwright/demo have a no-op). Safe to call repeatedly — idempotent re-registration
    of the same behaviour. Guarded so it can never break a run."""
    if run_id and hasattr(robot, "set_event_sink"):
        try:
            robot.set_event_sink(_make_robot_status_sink(run_id, test_id))
        except Exception:
            pass


def _execute_structured_plan(plan: dict, credentials: dict, run_id: str = "", test_id: str = "", app_map: dict = None, start_kiosk: str = "") -> tuple[list[dict], str, dict]:
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
    # Browser starts on the run's primary kiosk (run_vision_step ran reset_to_entry on its URL), so
    # seed current_kiosk with it — a single-kiosk run then never fires a redundant first-step switch,
    # and a cross-kiosk run only switches when a step targets the OTHER app.
    current_kiosk: str = start_kiosk or ""   # kiosk_id the browser is currently showing
    last_screenshot: str = ""  # updated after every step; used by subsequent verify steps
    captured: dict[str, str] = {}   # runtime values captured mid-test (e.g. issued card number)

    def _kid_of_dev(dev: str) -> str:
        """A device alias ('RPS') OR a bare kiosk_id ('kiosk-2') → its kiosk_id. device_map is
        indexed by BOTH forms (see _load_device_map), so this resolves either; an unknown key is
        assumed to already be a kiosk_id."""
        if not dev:
            return ""
        c = device_map.get(dev)
        return (c.get("kiosk_id") if c else "") or dev

    def _kid_of_screen(sid: str) -> str:
        """The kiosk_id that OWNS an app_map screen (screens are tagged app_id=kiosk_id). Lets a
        `verify` switch to the app that owns its expected_screen even when the plan didn't tag the
        verify with a device and placed it before the first tagged interaction step."""
        if not sid:
            return ""
        sc = ((app_map or {}).get("screens") or {}).get(sid) or {}
        return sc.get("app_id") or sc.get("kiosk_id") or ""

    def _sub_captured(val: str) -> str:
        """Substitute {{captured.NAME}} placeholders with values captured earlier in THIS test
        (e.g. a card number issued at VPS, reused at RPS). Unknown names are left untouched."""
        if not val or "{{captured." not in val:
            return val
        for name, cv in captured.items():
            val = val.replace(f"{{{{captured.{name}}}}}", str(cv))
        return val

    def _robot_fail(idx: int, step_label: str, exc: Exception) -> str:
        """Record a robot-action EXCEPTION (real-robot API timeout, unreachable, a click that
        did-not-land, a motion-planning failure) as a failed step and return the outcome string
        "robot_error". The caller returns that outcome so run_vision_step STOPS the run on a genuine
        robot fault (real backend) instead of handing off to Tier-3 — Tier-3 would just drive the
        same faulted arm, which may be stuck/unable to return home. Backend-agnostic recording; the
        stop decision itself is scoped to the real backend in run_vision_step."""
        reason = f"{type(exc).__name__}: {exc}"
        print(f"    {idx:>2}. ✗ robot action failed — {reason}")
        sr = {"step": step_label, "success": False, "method": "robot_error", "note": reason,
              "observation": f"Robot action failed: {reason}"}
        step_results.append(sr)
        if run_id:
            broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id,
                                      "test_id": test_id, "step_index": idx, **sr})
        return "robot_error"

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

    def _switch_browser_to(dev_cfg: dict, dev_alias: str, kid: str) -> None:
        """Playwright: point the browser at this device's app URL so ITS screens load.
        A physical robot just faces the device it drove to, so this is a no-op on the real
        backend. Used by BOTH the cross-kiosk device-routing hop (screen-interaction steps
        tagged with a different device) AND the explicit AGV `move` action — an AGV move to a
        device means the robot now faces that device's screen, so the browser must follow it
        (without this, `move: AGV → RPS` drove the simulated base but left the browser on VPS,
        and the next verify saw the wrong app's screen). Raises on a navigation error so the
        caller records a graceful failed step → Tier-3."""
        if settings.robot_backend != "playwright":
            return
        dev_url = (dev_cfg.get("url") or "").strip()
        if not dev_url:
            print(f"    [CROSS-KIOSK] ⚠ device '{dev_alias}' (kiosk '{kid}') has no URL configured "
                  f"— cannot switch apps; its screens will not load. Set its URL via "
                  f"App Explorer/Configuration.")
            return
        svc = (getattr(settings, "card_service_url", "") or "").strip()
        if svc:
            dev_url += ("&" if "?" in dev_url else "?") + f"cardServiceUrl={svc}"
        print(f"    [CROSS-KIOSK] Navigating browser to {dev_url}")
        robot.navigate_to_url(dev_url)
        time.sleep(0.8)

    # ── Robot-API telemetry → live monitor ────────────────────────────────────
    # Every real-robot REST call is recorded (endpoint, cmd_id, status, latency, request/response
    # time). Flush the new ones to the live monitor so a user watching a run sees each robot API
    # call with its timing and status. No-op for playwright/demo (no physical robot calls).
    _evt_seen = [0]
    def _flush_robot_events() -> None:
        try:
            evs = robot.get_events(_evt_seen[0]) if hasattr(robot, "get_events") else []
        except Exception:
            evs = []
        for ev in evs:
            msg = (f"[ROBOT API] {ev.get('event_type','')} {ev.get('endpoint','')}"
                   f"{(' (' + ev.get('cmd_id','') + ')') if ev.get('cmd_id') else ''} → "
                   f"{ev.get('http_status','?')} in {ev.get('latency_ms','?')}ms")
            if run_id:
                broadcaster.emit(run_id, {"event": "log", "run_id": run_id, "test_id": test_id,
                                          "message": msg, "robot_api": ev})
        _evt_seen[0] += len(evs)

    # Real-time AGV/arm status sink: the real backend pushes live status ticks (state + distance
    # remaining / elapsed) here WHILE the base is driving or the arm is moving (a blocking call that
    # can take minutes), so the monitor shows progress instead of a silent wait. These are live-only
    # (not in the robot event ring-buffer), so the post-step _flush_robot_events() pull never
    # double-emits them. The sink is USUALLY already registered by run_vision_step BEFORE the per-test
    # positioning move so that drive streams too; re-registering here (same behaviour) is idempotent.
    _register_status_sink(run_id, test_id)

    # Human-readable command ids restart at cmd-1 for each test (real backend only).
    if hasattr(robot, "reset_command_seq"):
        try:
            robot.reset_command_seq()
            _evt_seen[0] = len(robot.get_events()) if hasattr(robot, "get_events") else 0
        except Exception:
            pass

    for i, step in enumerate(plan.get("steps") or [], 1):
        _flush_robot_events()   # surface the previous step's robot API calls
        action = step.get("action", "")

        # ── cross-kiosk routing — put the browser on the app THIS step targets ──────────────
        # Ground-truth driven, so it survives every plan shape the LLM emits (explicit device tag,
        # bare kiosk_id, or an untagged `verify` for the next app):
        #   • a device/target tag  → that device's kiosk
        #   • a `verify`           → the kiosk that OWNS its expected_screen (app_map app_id)
        # so even a `verify login` placed BEFORE the first RPS-tagged interaction step still
        # switches to RPS first. Skipped for explicit AGV actions (move/wait/check_state) — the
        # move handler drives the base itself, and wait/check_state don't touch a device screen.
        # Any failure here is recorded as a failed step → normal Tier-3/fail path (never a silent abort).
        if action not in ("move", "navigate", "move_base", "wait", "check_state", "state"):
            _tag = step.get("device") or step.get("target")
            target_kid = _kid_of_dev(_tag) if _tag else (
                _kid_of_screen(step.get("expected_screen", "")) if action == "verify" else "")
            if target_kid and target_kid != current_kiosk:
                dev_cfg = device_map.get(target_kid) or {}
                if dev_cfg:
                    print(f"    [CROSS-KIOSK] Switch kiosk '{current_kiosk or '—'}' → '{target_kid}'")
                    try:
                        print(f"    [ROBOT] Moving to kiosk '{target_kid}' @ "
                              f"({dev_cfg.get('pos_x',0)}, {dev_cfg.get('pos_y',0)}, {dev_cfg.get('pos_theta',0)}°)")
                        # Pass the destination's AGV-map position name as `target` (position_name if
                        # set, else the kiosk_id — historical); x/y/theta ride along only as a raw-pose
                        # fallback. Real robot drives /base/goto by target; playwright/demo ignore it.
                        _agv = _agv_target(dev_cfg, target_kid)
                        robot.move_to_position(dev_cfg.get("pos_x", 0), dev_cfg.get("pos_y", 0),
                                               dev_cfg.get("pos_theta", 0), target=_agv)
                        time.sleep(0.8)  # allow robot/camera to settle
                        # Playwright: point the browser at this kiosk's app URL. (No-op on real.)
                        _switch_browser_to(dev_cfg, target_kid, target_kid)
                    except Exception as exc:
                        return step_results, _robot_fail(i, f"switch to kiosk {target_kid}", exc), captured
                    current_kiosk = target_kid
                else:
                    print(f"    [CROSS-KIOSK] ⚠ no device/URL for kiosk '{target_kid}' — cannot switch; "
                          f"its screens will not load (align the Device Map / kiosk URL).")
        action = step.get("action", "")

        # ── verify ────────────────────────────────────────────────────────────
        if action == "verify":
            # Ensure a CURRENT screenshot exists for the validation pipeline. A LEADING verify
            # (no preceding tap — e.g. TC-RPS-001 step 1 "the login screen is visible") has no
            # last_screenshot yet. On the REAL robot this is essential: the camera path validates
            # against last_screenshot, so without a capture here _match_by_phash gets NO image →
            # inconclusive → the Claude fallback also gets no image → spurious FAIL → needless Tier-3
            # on the very first step. This capture also localizes the screen (AprilTag) + calibrates
            # the camera scale on the real backend. Playwright likewise needs the frame for its
            # evidence + text-mismatch read. Demo is excluded: its pipeline is always-true and a
            # capture would consume a scripted demo screenshot.
            if not last_screenshot and settings.robot_backend != "demo":
                last_screenshot = _cap("verify", i)
                # Self-calibrate the vertical tap mapping from the login screen's OWN input boxes (real
                # backend, one-shot on the leading verify). The robot's rectified /capture crop varies
                # per arm/camera pose, so a static camera_calib_ay/by mis-places taps (email tap landed
                # ~2 cm below the field). Deriving (ay,by) from this freshly-captured login frame makes
                # taps land on element CENTERS for THIS pose; a low-confidence detect keeps the config.
                if settings.robot_backend == "real" and last_screenshot and app_map:
                    try:
                        from vision_agent.vision.screen_calibrate import find_login_anchor_fracs
                        _cal_sc = (app_map.get("screens") or {}).get(step.get("expected_screen", "")) or {}
                        _af = find_login_anchor_fracs(_cal_sc, settings.viewport_height)
                        if _af:
                            _vh = settings.viewport_height
                            robot.calibrate_vertical_from_login(
                                last_screenshot,
                                _af["email"] * _vh, _af["password"] * _vh,
                                signin_center_y=(_af["signin"] * _vh) if _af.get("signin") else None,
                                footer_center_y=(_af["footer"] * _vh) if _af.get("footer") else None)
                    except Exception as _ce:
                        print(f"    [ROBOT] auto-calibration skipped: {_ce}")
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

            # ── INTENT RESCUE for a stale/wrong expected_screen in the PLAN ─────────────────────────
            # A verify's real intent is its DESCRIPTION; `expected_screen` is a plan-time guess that can
            # be wrong — e.g. it names 'payment' for a "first order completed" check, but a successful
            # payment has already advanced to an UNCHARTED 'order_result' screen the planner could not
            # predict (the mock-card completion is handled by live vision, so that transition is not in
            # the app_map). When the ONLY failure is a screen-id mismatch (and no text assertion), ask
            # whether the CURRENT screen actually satisfies the described outcome; if it clearly does,
            # PASS and let the plan CONTINUE — instead of hard-failing, which desyncs into a Tier-3
            # resume that wanders (observed: it started checking the later VPS step). Strict judge → a
            # genuinely wrong screen won't satisfy a specific description, so real failures still fail.
            # Runs AFTER the nav-settle retries, so a transient mid-navigation screen isn't mistaken for
            # the result. Backend-agnostic (fresh screenshot works on playwright AND real robot).
            if success is False and method == "screen_id" and not expected_text:
                from vision_agent.nodes.validate_pipeline import verify_intent_satisfied
                intent_shot = _cap("verify_intent", i) or last_screenshot
                ok, intent_obs = verify_intent_satisfied(intent_shot, desc, expected, actual)
                if ok:
                    print(f"    {i:>2}. verify  expected={expected!r} actual={actual!r} — screen mismatch, "
                          f"but the step's described OUTCOME is satisfied → PASS [intent_vision]")
                    success = True
                    method  = "intent_vision"
                    observation = (f"Planned expected_screen '{expected}' did not match (screen is "
                                   f"'{actual}'), but the step's described outcome is satisfied: {intent_obs}")
                    note = "expected_screen in the plan was stale — validated by the step's described outcome (vision)"
                    if settings.robot_backend == "playwright" and intent_shot:
                        last_screenshot = intent_shot

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
            # Pass but flag for human review when the value matched only after ignoring number
            # formatting (e.g. expected 5000, screen shows 5,000) — value correct, format differs.
            if vr.get("human_review"):
                sr["human_review"] = True
                sr["note"] = vr.get("note", "")
            # Surface the intent-rescue note (stale expected_screen validated by described outcome).
            if method == "intent_vision" and note:
                sr["note"] = note
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {
                "event": "step_result", "run_id": run_id, "test_id": test_id,
                "step_index": i, **sr,
            })
            if not success:
                return step_results, "failed", captured
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
            uncharted = bool(eid and sid and not _element_charted(app_map, sid, eid))
            if (px == 0 and py == 0) or uncharted:
                why = ("no coordinates (screen not yet in app_map)" if (px == 0 and py == 0)
                       else "element not charted on this screen (planner guess → fabricated coords)")
                print(f"    {i:>2}. tap   {eid!r} — {why} → Tier-3 fallback")
                sr = {
                    "step": f"tap: {eid} @ ({px},{py})",
                    "success": False,
                    "method": "uncharted_element" if uncharted else "no_coordinates",
                    "note": f"'{eid}' not reliably locatable on '{sid}' — Tier-3 vision will locate it",
                }
                step_results.append(sr)
                if run_id:
                    broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                return step_results, "failed", captured

            # Prefer clicking by STABLE DOM identity (testid) when the app_map has one. A coord tap
            # snaps only within 80px, so a stale/state-shifted coord (the VPS top-up buttons drifted
            # ~112px) falls through to a RAW click on empty space — the button is silently MISSED yet
            # the step reports success. Clicking by testid (playwright) is immune; on the real arm /
            # demo tap_by_testid returns False and we fall back to the coordinate tap unchanged.
            testid = _element_testid(app_map, sid, eid)
            tapped = False
            if testid:
                try:
                    tapped = bool(robot.tap_by_testid(testid))
                except AttributeError:
                    tapped = False
            if tapped:
                print(f"    {i:>2}. tap   {eid!r} via testid={testid!r}  [{sid}]")
                tap_result = {}
            else:
                print(f"    {i:>2}. tap   {eid!r} @ ({px},{py})  [{sid}]")
                try:
                    tap_result = robot.tap(px, py)
                except Exception as exc:
                    # Robot fault (API timeout/unreachable, click did-not-land, motion-plan failure) →
                    # STOP on the real backend (no Tier-3); playwright/demo still fall through to Tier-3.
                    return step_results, _robot_fail(i, f"tap: {eid} @ ({px},{py})", exc), captured
            time.sleep(0.5)
            # Fresh post-tap image for the next verify step.
            #   playwright → cheap browser screenshot; also saved as step evidence (UI).
            #   real robot → the /screen/click completion already returns the camera frame
            #                (tap_result["image_path"]); reuse it — no extra /capture cycle.
            #                Not surfaced as UI step evidence for real-robot runs.
            #   demo       → verify always passes; no image needed.
            after_shot  = ""
            before_shot = ""
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
                # Real backend: surface the annotated BEFORE (crosshair at the tapped camera pixel)
                # and the AFTER (post-tap /screen/click frame) as this step's evidence, so an operator
                # can verify tap accuracy in the monitor/results. real_robot saved both into the run's
                # per-run screenshots folder.
                if settings.robot_backend == "real":
                    before_shot = (tap_result or {}).get("before_image_path", "") or ""
                    after_shot  = (tap_result or {}).get("after_image_path", "") or tap_img
            # Navigation prediction from app_map transitions
            sc_data  = (app_map or {}).get("screens", {}).get(sid, {})
            predicted = (sc_data.get("transitions") or {}).get(eid)
            if predicted:
                print(f"         → nav prediction: '{predicted}' [app_map transitions]")
            sr = {"step": f"tap: {eid} @ ({px},{py})", "success": True, "method": "app_map",
                  "screen_id": sid, "element_id": eid, "screenshot_after": after_shot}
            if before_shot:
                sr["screenshot_before"] = before_shot
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── type ──────────────────────────────────────────────────────────────
        if action == "type":
            value = _resolve_credentials(step.get("value", ""), scenario, credentials)
            value = _sub_captured(value)   # {{captured.card_number}} → the value captured earlier
            px  = step.get("px", 0)
            py  = step.get("py", 0)
            eid = step.get("element_id", "")
            sid = step.get("screen_id", "")
            if "{{captured." in value:
                print(f"    {i:>2}. ✗ type — unresolved capture placeholder {value!r} (nothing captured it) → fail")
                sr = {"step": f"type: {value[:30]}", "success": False, "method": "unresolved_capture",
                      "observation": f"Type value still contains an uncaptured placeholder: {value!r}"}
                step_results.append(sr)
                if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                return step_results, "failed", captured
            # If the target field is NOT charted on this screen, the plan's coords are a Tier-2 guess
            # (e.g. the RPS mock-card input revealed only after "Use Mock Card" — never charted). Focusing
            # those fabricated coords lands on nothing → type_text's Ctrl+A selects the whole page and the
            # value is dropped, yet the step would "succeed". Hand off to Tier-3 vision to locate the real
            # field and enter the value, instead of a silent mis-type. (Charted fields are unaffected.)
            if eid and sid and not _element_charted(app_map, sid, eid):
                print(f"    {i:>2}. type  {eid!r} — element not charted on '{sid}' (planner guess) → Tier-3 fallback")
                sr = {"step": f"type: {eid} (uncharted)", "success": False, "method": "uncharted_element",
                      "note": f"'{eid}' not charted on '{sid}' — Tier-3 vision will locate the field and enter the value"}
                step_results.append(sr)
                if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                return step_results, "failed", captured
            # Focus the target field FIRST when the plan supplies its coordinates. type_text() types
            # into whatever is currently focused and its clear (Ctrl+A) selects text in the focused
            # element — with NOTHING focused, Ctrl+A selects the whole PAGE and the value goes nowhere
            # (observed on VPS: every field highlighted, amount 300 never entered). Login-style plans
            # emit a separate tap step before the type (field already focused, no px/py on the type
            # step → skip). A standalone type step — "enter amount 300" — carries its own px/py and
            # NO preceding tap, so it must self-focus. This makes typing work for EVERY app, not just
            # login flows (previously it only worked when a prior tap happened to focus the field).
            try:
                if px and py:
                    # Prefer focusing by STABLE DOM identity (testid) over pixel coords. App_map
                    # coords can be stale / state-dependent (e.g. the VPS top-up panel shifts ~112px
                    # when a reader box opens), landing the focus — and thus the typed value — in the
                    # WRONG field. When the app_map element has a testid we focus by it (playwright);
                    # otherwise (or on the real arm) we fall back to the coordinate tap unchanged.
                    testid  = _element_testid(app_map, sid, eid)
                    focused = False
                    if testid:
                        try:
                            focused = bool(robot.focus_by_testid(testid))
                        except AttributeError:
                            focused = False
                    if focused:
                        print(f"    {i:>2}. focus {eid!r} via testid={testid!r}  (focus before type)")
                    else:
                        print(f"    {i:>2}. focus {eid!r} @ ({px},{py})  (focus before type)")
                        robot.tap(px, py)
                    time.sleep(0.3)
                else:
                    print(f"    {i:>2}. type  (no coords on step — relying on prior tap's focus)")
                print(f"    {i:>2}. type  {value!r}")
                type_res = robot.type_text(value, clear_first=True)
            except Exception as exc:
                return step_results, _robot_fail(i, f"type: {value[:30]}", exc), captured
            # type_text reports success=False when it could NOT actually enter the value (real robot:
            # no keyboard_map loaded, or none of the characters exist in the map).  Fail the step so
            # the run never falsely PASSES a login it never typed — it hands off to Tier-3 vision
            # instead of silently continuing to the next field with an empty input.  (playwright/demo
            # always return success=True, so this is a no-op for them.)
            if isinstance(type_res, dict) and type_res.get("success") is False:
                reason = type_res.get("error") or "type did not enter the value"
                # A partial type on the real robot fails via a click that did-not-land (DESCEND/HOVER
                # etc.) — that is a robot FAULT (stop, no Tier-3). A "keyboard_map not loaded"/"no
                # characters" reason is a DATA issue → keep the Tier-3 handoff so vision can try.
                fault  = _is_robot_fault(reason)
                method = "robot_error" if fault else "type_failed"
                tail   = "→ STOPPING (robot fault)" if fault else "→ failing step (hand off to Tier-3 vision)"
                print(f"    {i:>2}. ✗ type — {reason} {tail}")
                sr = {"step": f"type: {value[:30]}", "success": False, "method": method,
                      "note": reason if fault else "",
                      "observation": f"Could not type {value!r}: {reason}"}
                step_results.append(sr)
                if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
                return step_results, ("robot_error" if fault else "failed"), captured
            time.sleep(0.3)
            after_shot = ""
            if settings.robot_backend == "playwright":
                after_shot = _cap("after", i)
                if after_shot:
                    last_screenshot = after_shot
            sr = {"step": f"type: {value[:30]}", "success": True, "method": "app_map",
                  "screen_id": sid, "element_id": eid, "screenshot_after": after_shot}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── capture — read a runtime value now for reuse in a later step ──────
        # e.g. the card number issued at VPS, entered later at RPS via {{captured.card_number}}.
        if action == "capture":
            name = step.get("capture_as") or step.get("element_id") or "value"
            desc = step.get("description", "")
            eid  = step.get("element_id", "")
            sid  = step.get("screen_id", "")
            px   = step.get("px", 0); py = step.get("py", 0)
            el   = None
            if app_map and sid and eid:
                el = next((e for e in ((app_map.get("screens") or {}).get(sid, {}) or {}).get("elements", [])
                           if e.get("id") == eid), None)
            if (not px or not py) and el and el.get("center"):
                px, py = int(el["center"][0]), int(el["center"][1])
            direct = ""
            try:
                testid = (el or {}).get("testid")
                if testid and hasattr(robot, "query_element_text"):
                    direct = robot.query_element_text(f'[data-testid="{testid}"]') or ""
                if not direct and px and py and hasattr(robot, "text_at_point"):
                    direct = robot.text_at_point(int(px), int(py)) or ""
            except Exception as exc:
                print(f"    {i:>2}. capture {name!r} — read failed: {exc}")
            direct = (direct or "").strip()
            # VISION IS AUTHORITATIVE for capture. A capture reads an app-GENERATED, transient value
            # (an issued card number, a confirmation code) that the App Explorer usually cannot chart
            # reliably — so the plan's element_id is frequently WRONG or stale. Observed live: the plan
            # bound this capture to a card-service *status* element whose text ("Connected") is short,
            # colon-free and thus passed _looks_like_value(), so the old "direct read first, vision only
            # if it looks bad" logic accepted the status text and typed it into the card input → the
            # test failed. The direct element read therefore cannot be trusted for capture; read the
            # named value from the CURRENT screen via Claude and only fall back to the direct read when
            # vision genuinely can't see it. One LLM call on a rare step buys correctness even when the
            # plan binds the wrong element. (This is also why the same plan "worked hours ago": that
            # run's element read empty → vision fired; this run's read a plausible-looking status word.)
            #   • real robot → REUSE the post-tap /screen/click camera frame already in last_screenshot
            #     (no extra /capture arm cycle); fall back to a fresh camera capture.
            #   • playwright → take a fresh cheap browser screenshot (always current).
            if settings.robot_backend == "playwright":
                shot = _cap("capture", i) or last_screenshot
            else:
                shot = last_screenshot or _cap("capture", i)
            vis = _capture_value_via_vision(shot, name, desc)
            if vis:
                if direct and direct != vis:
                    print(f"    {i:>2}. capture {name!r} — direct read {direct!r} OVERRIDDEN by vision read {vis!r}")
                val = vis
            elif _looks_like_value(direct):
                val = direct   # vision couldn't see it; the direct read is a clean token — use it
            else:
                val = ""       # neither source produced a usable value → {{captured}} fails later → Tier-3
            captured[name] = val
            print(f"    {i:>2}. capture {name!r} = {val!r}  [{'ok' if val else 'EMPTY → later {{captured}} will fail → Tier-3'}]")
            sr = {"step": f"capture: {name}", "success": True, "method": "capture",
                  "note": f"{name}={val!r}", "screen_id": sid, "element_id": eid}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── move — drive the AGV base to a device / home ─────────────────────
        # "Move the AGV to VPS" / "Move back to home". The target device alias is resolved to its
        # kiosk_id (the join key) and passed as the robot-API target; "home" is a reserved target.
        # Real backend → /base/goto via robot.navigate_to_kiosk (invokes the AGV API, respects the
        # response timeout, fails gracefully on timeout). Playwright/demo → simulated no-op.
        if action in ("move", "navigate", "move_base"):
            target_alias = (step.get("target") or step.get("device") or "").strip()
            is_home = target_alias.lower() in ("home", "base", "dock")
            # Resolve the AGV-map position name for /base/goto's `target`:
            #   • home  → settings.agv_home_target (e.g. "home-Aug-14-G37"; default "home")
            #   • device→ its position_name (e.g. "kiosk-2-Aug-14-G37"), else kiosk_id, else the alias
            # position_name is decoupled from the kiosk_id join key, so renaming AGV positions in the
            # Device Map never disturbs exploration/plan/template joins.
            dev_cfg = device_map.get(target_alias) or {}
            target = (settings.agv_home_target or "home") if is_home else _agv_target(dev_cfg, target_alias)
            print(f"    {i:>2}. move  AGV → {target_alias or target!r} (robot target='{target}')")
            try:
                res = robot.navigate_to_kiosk(target) if hasattr(robot, "navigate_to_kiosk") else {"simulated": True}
                # Playwright: the AGV move means the robot now faces THIS device's screen — switch
                # the browser to its app URL so its screens load (a physical robot just faces the
                # device it drove to → no-op there). "home" is not a device, so nothing to switch.
                # Mark current_kiosk (the KIOSK_ID, not the AGV position name) so the following
                # cross-kiosk routing block compares correctly and doesn't switch again.
                if not is_home and target_alias:
                    _switch_browser_to(dev_cfg, target_alias, target)
                    current_kiosk = (dev_cfg.get("kiosk_id") or _kid_of_dev(target_alias) or target_alias)
            except Exception as exc:
                _flush_robot_events()
                return step_results, _robot_fail(i, f"move: AGV → {target_alias or target}", exc), captured
            _flush_robot_events()
            note = f"AGV moved to {target}" + (" (simulated)" if (res or {}).get("simulated") else "")
            sr = {"step": f"move: AGV → {target_alias or target}", "success": True, "method": "robot_base",
                  "device": target_alias, "note": note, "screenshot_after": ""}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── wait — sleep for N seconds (bounded) ─────────────────────────────
        if action == "wait":
            secs = step.get("seconds", step.get("duration_s", 0))
            try:
                secs = float(secs)
            except (TypeError, ValueError):
                secs = 0.0
            secs = max(0.0, min(secs, 120.0))   # cap so a bad plan can't hang the suite
            print(f"    {i:>2}. wait  {secs:g}s")
            time.sleep(secs)
            sr = {"step": f"wait: {secs:g}s", "success": True, "method": "wait", "screenshot_after": ""}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            continue

        # ── check_state — assert AGV/arm state via the status API ────────────
        # "Check the state of the AGV after reaching the device". No screenshot involved — reads
        # /base/state (or /arm/state) and compares to the expected state when the plan names one.
        if action in ("check_state", "state"):
            target = (step.get("target") or "agv").strip().lower()
            expected = (step.get("expected_state") or step.get("expected_value") or "").strip().lower()
            try:
                if target in ("arm", "robot") and hasattr(robot, "get_arm_state"):
                    st = robot.get_arm_state()
                elif hasattr(robot, "get_base_state"):
                    st = robot.get_base_state()
                else:
                    st = {}
            except Exception as exc:
                _flush_robot_events()
                return step_results, _robot_fail(i, f"check_state: {target}", exc), captured
            _flush_robot_events()
            actual = str((st or {}).get("state", "")).lower()
            # The AGV base reports "ready" when it has arrived and is holding position — that IS the
            # base's idle/at-rest state. So a test asserting the AGV is "idle" (or "ready"/"arrived")
            # is satisfied by any of those. Normalize both sides to a canonical "ready" for the base.
            _READY_EQUIV = {"ready", "idle", "arrived", "done", "reached"}
            is_base = target not in ("arm", "robot")
            if is_base and expected in _READY_EQUIV and actual in _READY_EQUIV:
                ok = True
            else:
                ok = True if not expected else (expected in actual or actual in expected)
            obs = f"{target} state = {actual or '?'}" + (f" (expected '{expected}')" if expected else "")
            print(f"    {i:>2}. check_state {target} → {actual or '?'}  [{'PASS' if ok else 'FAIL'}]")
            sr = {"step": f"check_state: {target}", "success": ok, "method": "robot_state",
                  "observation": obs, "screenshot_after": ""}
            step_results.append(sr)
            if run_id: broadcaster.emit(run_id, {"event": "step_result", "run_id": run_id, "test_id": test_id, "step_index": i, **sr})
            if not ok:
                return step_results, "failed", captured
            continue

        # ── vision_required sentinel — planner found an uncharted screen ──────
        # Handle it INLINE: run a bounded vision segment for just this step's objective, then
        # CONTINUE the structured plan (so a later cross-kiosk `move`/`verify` still runs). Only if
        # the segment makes no progress do we fall through to the outer Tier-3 resume safety net.
        if action == "vision_required":
            desc = _sub_captured(step.get("description", "complete the current sub-task"))
            print(f"    {i:>2}. [VISION REQUIRED — inline] {desc}")
            seg_steps, seg_ok = _run_inline_vision(
                desc, captured, credentials, scenario, app_map, run_id, test_id, i,
            )
            step_results.extend(seg_steps)
            # Only bail to the outer Tier-3 resume when the segment stalled AND there are still
            # structured steps we failed to reach (e.g. a cross-kiosk move back). For a stall on the
            # LAST plan step (typically a pure verify sub-task whose screen doesn't change by design)
            # there's nothing left to resume — keep the segment's own step results and let the
            # conclusive-verdict node judge, instead of firing a redundant end-of-plan Tier-3 pass.
            is_last = i >= len(plan.get("steps") or [])
            if not seg_ok and not is_last:
                print(f"    {i:>2}. [VISION REQUIRED] segment stalled with structured steps remaining "
                      f"→ handing off to outer Tier-3 resume")
                return step_results, "failed", captured
            continue

        # ── unknown ───────────────────────────────────────────────────────────
        print(f"    {i:>2}. [UNKNOWN action={action!r}] — skipped")

    _flush_robot_events()   # surface the final step's robot API calls
    return step_results, "passed", captured


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
        _position_for_test(tc)
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
    captured: dict | None = None,
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

    # ── Captured runtime values — carry them into Tier-3 ──────────────────────
    # A value read earlier in THIS test (e.g. the card number issued at VPS) lives only in the
    # Tier-1/2 executor's `captured` dict. Without threading it here, Tier-3 would see the literal
    # "{{captured.card_number}}" (or nothing) and invent a number — exactly the E2E failure where
    # it typed a bogus '1234' at RPS. Hand Claude the real values AND the remaining planned-step
    # descriptions (placeholders resolved) so it knows the specific sub-goal (e.g. "enter card
    # 9013"). Generic: any {{captured.*}} the plan defined is substituted from live values.
    captured = captured or {}
    def _sub_cap(text: str) -> str:
        for _n, _v in captured.items():
            text = text.replace(f"{{{{captured.{_n}}}}}", str(_v))
        return text
    if captured:
        _cv = "\n".join(f"  {k} = {v}" for k, v in captured.items() if str(v).strip())
        if _cv:
            task_description += (
                "\n\nValues captured earlier in THIS test — when a remaining step needs one of "
                "these (e.g. re-entering an issued card number), use the EXACT value below; never "
                "invent or guess one:\n" + _cv
            )
    # Remaining planned steps give Claude the specific intent for the uncharted tail of the flow.
    _remaining = plan_steps_all[start_step_idx:]
    _rem_lines = [
        f"  - {_sub_cap(s.get('description') or s.get('action',''))}"
        for s in _remaining if (s.get("description") or s.get("action"))
    ]
    if _rem_lines:
        task_description += (
            "\n\nRemaining intended steps (guidance — identify the real on-screen elements "
            "yourself; values already resolved):\n" + "\n".join(_rem_lines)
        )

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
        print(f"  [RUN] TIER-1/2 EXECUTION [{backend}] — running {len(structured_plan.get('steps') or [])} "
              f"stored-coordinate steps from the app map (0 LLM calls)")

        # Register the live AGV/arm status sink BEFORE positioning so the per-test AGV move streams in
        # real time (10s ticks) instead of a silent live monitor until the first in-step move. Without
        # this, the positioning drive (which can take MINUTES) showed nothing on the live stream until
        # _execute_structured_plan later registered the sink — observed on TC-AGV-002 (2026-08-18).
        # _execute_structured_plan re-registers the same sink (idempotent).
        _register_status_sink(run_id, tc["test_id"])

        # Point the browser at THIS test's kiosk, then reset to app entry and wait for the SPA /
        # camera to settle. Per-test URL is essential for a mixed suite (RPS test then VPS test);
        # reset_to_entry also clears localStorage/sessionStorage so no prior login carries over.
        _position_for_test(tc)
        robot.reset_to_entry()
        time.sleep(1.2)

        step_results, outcome, captured = _execute_structured_plan(
            structured_plan, credentials,
            run_id=run_id, test_id=tc["test_id"], app_map=app_map,
            start_kiosk=tc.get("kiosk_id", ""),
        )
        passed = sum(1 for r in step_results if r["success"])

        # ── ROBOT / HARDWARE FAULT — STOP, do NOT hand off to Tier-3 ──────────────
        # A genuine robot fault (motion-planning failure like DESCEND_LIN_FAILED / HOVER_FAILED, a
        # click that did-not-land, an API timeout, or the robot API being unreachable) means the arm
        # may be stuck and unable to return to its home position. Tier-3 would just issue MORE taps to
        # the SAME faulted arm — pointless and potentially unsafe — so we stop the run and report the
        # fault. Scoped to the real backend (playwright/demo faults still fall through to Tier-3), and
        # gated by settings.robot_error_stops_run so the old always-Tier-3 behaviour can be restored.
        if outcome == "robot_error":
            if settings.robot_backend == "real" and settings.robot_error_stops_run:
                last_sr = step_results[-1] if step_results else {}
                why = last_sr.get("observation") or last_sr.get("note") or "robot fault"
                print(f"\n  [RUN] TIER-1/2 EXECUTION: ROBOT ERROR at step {len(step_results)} — {why}")
                print("  [RUN] → STOPPING run (Tier-3 SKIPPED): a physical robot fault means the arm may be "
                      "unable to recover / return home; retrying via vision would drive the same faulted arm.")
                if run_id:
                    broadcaster.emit(run_id, {"event": "log", "run_id": run_id, "test_id": tc["test_id"],
                                              "message": f"[ROBOT ERROR] {why} — run STOPPED (Tier-3 skipped)"})
                test_result: TestResult = {
                    "test_id":        tc["test_id"],
                    "summary":        tc["summary"],
                    "outcome":        "failed",
                    "step_results":   step_results,
                    "vision_summary": f"ROBOT ERROR [{backend}] — run STOPPED, Tier-3 skipped: {why}",
                    "robot_error":    True,
                }
                return {"test_results": [*(state.get("test_results") or []), test_result]}
            # playwright/demo, or the stop-gate is disabled → treat like a normal failure (Tier-3).
            outcome = "failed"

        # On failure: hand off to Tier-3, ALWAYS resuming from the current screen.
        if outcome in ("failed", "vision_required"):
            last_sr = step_results[-1] if step_results else {}

            # A failed VERIFY is a test ASSERTION failure — the app is genuinely not in the expected
            # state. Do NOT hand off to Tier-3, which would re-plan and re-attempt the PRECEDING actions
            # (e.g. re-login) that the test never asked to retry (observed: TC-RPS-001 on a broken login
            # looped through 3 re-login attempts). Fail terminally. Action-step (tap/type) failures are
            # NOT assertions — they still hand off so Tier-3 can locate the element via vision and
            # COMPLETE the step. Gated by settings.verify_failure_stops_run; applies to ALL backends.
            is_verify_fail = (
                str(last_sr.get("step", "")).startswith("verify:")
                and not last_sr.get("verification_gap")
            )
            if is_verify_fail and settings.verify_failure_stops_run:
                why = last_sr.get("observation") or last_sr.get("note") or "verification failed"
                print(f"\n  [RUN] TIER-1/2 EXECUTION: FAILED at step {len(step_results)} (verify assertion) — {why}")
                print("  [RUN] → verify assertion FAILED; NOT retrying via Tier-3 "
                      "(the test did not ask to retry). Failing the test.")
                if run_id:
                    broadcaster.emit(run_id, {"event": "log", "run_id": run_id, "test_id": tc["test_id"],
                                              "message": f"[VERIFY FAILED] {why} — test failed (no retry)"})
                test_result: TestResult = {
                    "test_id":        tc["test_id"],
                    "summary":        tc["summary"],
                    "outcome":        "failed",
                    "step_results":   step_results,
                    "vision_summary": f"Verify assertion failed [{backend}] — Tier-3 skipped (no retry): {why}",
                }
                return {"test_results": [*(state.get("test_results") or []), test_result]}

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
            _why = last_sr.get("observation") or last_sr.get("note") or last_sr.get("step", "")
            print(
                f"\n  [RUN] TIER-1/2 EXECUTION: {outcome.upper()} at step {failed_idx + 1} "
                f"(method={last_sr.get('method', '?')}) — reason: {_why!r}"
            )
            print(f"  [RUN] → handing off to TIER-3 (vision agent), resuming from the CURRENT screen (no reset)")
            t3_result = _run_tier3_continue(state, tc, completed, failed_idx, captured=captured)

            t3_steps   = t3_result.get("step_results") or []
            t3_outcome = t3_result.get("outcome", "failed")
            t3_passed  = sum(1 for r in t3_steps if r.get("success"))
            print(f"\n  [RUN] TIER-3 (vision) RESULT: {tc['test_id']}  {t3_outcome.upper()}  ({t3_passed}/{len(t3_steps)} steps passed)")
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

        print(f"\n  [RUN] TIER-1/2 EXECUTION: {outcome.upper()} — {tc['test_id']}  ({passed}/{len(step_results)} steps passed, 0 LLM calls)")

        test_result: TestResult = {
            "test_id":        tc["test_id"],
            "summary":        tc["summary"],
            "outcome":        outcome,
            "step_results":   step_results,
            "vision_summary": f"Executed via app_map [{backend}] ({outcome})",
        }
        return {"test_results": [*(state.get("test_results") or []), test_result]}

    # ── Tier 3: VisionAgent (screenshot + Claude vision per step) ─────────────
    print(f"  [RUN] TIER-3 (vision agent) — no structured plan; planning+executing from live screenshots")

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
