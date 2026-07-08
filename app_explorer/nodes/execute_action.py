"""
execute_action — navigate to the source screen (smartly) and execute the queued
action's steps via the live robot backend, then capture a real screenshot.

Session strategy (avoids repeated logout/login):
  1. If browser DOM already shows the source screen → execute directly (no reset).
  2. If both current screen and source screen are authenticated (post-login) →
       navigate via sidebar without logging out.
  3. Otherwise → full reset (localStorage clear) + approach-path replay.

Works with any ROBOT_BACKEND (playwright for browser, real for hardware arm).
"""
import time
from pathlib import Path
from vision_agent import robot
from vision_agent.config import settings
from app_explorer.state import ExplorerState, ExplorationAction

def _requires_valid_login(screen_id: str, approach_paths: dict) -> bool:
    """Return True if this screen can only be reached after a valid login.

    Inspects the screen's approach path for any action with credential_scenario="valid".
    Generic: works for any app — no hardcoded screen-name lists.
    """
    for action in approach_paths.get(screen_id, []):
        if action.get("credential_scenario") == "valid":
            return True
    return False


def _resolve(value: str, credentials: dict, captured: dict | None = None) -> str:
    """Substitute credential + captured-value placeholders written by SUGGEST_EXPLORABLE_ACTIONS.

    Credentials → {{valid_email}} etc.  Captured identifiers (issued card number, order id, …)
    generated earlier in the exploration → {{captured.NAME}}, so stateful management flows
    (add money / check balance) can be driven with the value the app itself produced.
    """
    if not value:
        return value
    valid   = credentials.get("valid", {})
    invalid = credentials.get("invalid", {})
    out = (
        value
        .replace("{{valid_email}}",      valid.get("email",    "tester@example.com"))
        .replace("{{valid_password}}",   valid.get("password", "Password123"))
        .replace("{{invalid_email}}",    invalid.get("email",  "baduser@example.com"))
        .replace("{{invalid_password}}", invalid.get("password", "WrongPass!"))
    )
    for name, val in (captured or {}).items():
        out = out.replace(f"{{{{captured.{name}}}}}", str(val))
    return out


def _get_pixel_center(app_map: dict, screen_id: str, element_id: str) -> tuple[int, int] | None:
    """Return the stored pixel center for an element.

    analyze_screen converts normalized → pixels before writing to app_map
    (see analyze.py _norm_to_px), so centers are already absolute pixels.
    """
    screen = (app_map.get("screens") or {}).get(screen_id, {})
    for el in screen.get("elements") or []:
        if el.get("id") == element_id:
            cx, cy = el["center"]
            return int(cx), int(cy)
    return None


def _run_steps(steps: list, screen_id: str, app_map: dict, credentials: dict,
               captured: dict | None = None) -> None:
    """
    Execute a list of ExplorationSteps against the live robot backend.

    For 'type' steps the element is tapped first to acquire focus — the
    SUGGEST_EXPLORABLE_ACTIONS prompt groups type+tap into compound actions
    without explicit focus steps.
    """
    for step in steps or []:
        act = step["action_type"]
        eid = step.get("element_id", "")
        val = _resolve(step.get("value") or "", credentials, captured)
        center = _get_pixel_center(app_map, screen_id, eid)
        px = center[0] if center else 700   # ~centre of 1400-wide viewport
        py = center[1] if center else 450   # ~centre of 900-tall viewport

        if center is None:
            print(f"    [WARN] element '{eid}' not in app_map for '{screen_id}' — using screen centre")

        if act == "tap":
            print(f"    tap   {eid!r}  @ ({px}, {py})")
            robot.tap(px, py)
            time.sleep(0.4)

        elif act == "type":
            # Tap to focus the field first, then type into it
            if center:
                print(f"    focus {eid!r}  @ ({px}, {py})")
                robot.tap(px, py)
                time.sleep(0.3)
            print(f"    type  {eid!r}  value={val!r}")
            robot.type_text(val)
            time.sleep(0.2)


def _do_reset_and_replay(
    action: ExplorationAction,
    approach_paths: dict,
    app_map: dict,
    credentials: dict,
    captured: dict | None = None,
) -> None:
    """Full reset: clear session storage, navigate to entry URL, replay approach path."""
    robot.reset_to_entry()
    time.sleep(0.8)
    approach = approach_paths.get(action["screen_id"], [])
    for past_action in approach:
        print(f"    [REPLAY] {past_action['screen_id']}::{past_action['action_key']}")
        _run_steps(past_action["steps"], past_action["screen_id"], app_map, credentials, captured)
        time.sleep(0.6)


def execute_action(state: ExplorerState) -> dict:
    queue          = list(state["exploration_queue"])
    action: ExplorationAction = queue.pop(0)
    explored       = list(state.get("explored_action_keys") or [])
    credentials    = state.get("credentials") or {}
    app_map        = state.get("app_map") or {}
    approach_paths = state.get("approach_paths") or {}
    captured       = state.get("captured_values") or {}
    known_screens  = app_map.get("screens") or {}

    full_key = f"{action['screen_id']}::{action['action_key']}"
    print(f"\n  [ACTION] {full_key}")
    print(f"           {action['description']}")

    # Live progress HUD — shows in the browser window (playwright mode only; no-op elsewhere)
    total_known = len(explored) + len(queue) + 1  # +1 for the action we just popped
    robot.update_explorer_progress(len(explored), total_known, full_key)

    # ── Smart session: avoid logout/login wherever possible ───────────────────
    # Strategy: check the live browser DOM to decide the cheapest path to the
    # source screen.  Falls back to full reset+replay when DOM check fails.
    reset_needed = True
    try:
        current_dom = robot.get_dom_screen_id()   # "" in demo/real modes
        source_sid  = action["screen_id"]
        source_dom  = (known_screens.get(source_sid) or {}).get("dom_id", "")

        # Resolve current DOM id → explorer screen_id (for approach_path lookup)
        current_sid = next(
            (sid for sid, sc in known_screens.items() if sc.get("dom_id") == current_dom),
            current_dom,  # fallback: treat DOM id as screen_id
        )

        if source_dom and current_dom and current_dom == source_dom:
            # Browser is already on the correct source screen.
            print(f"    [SMART] Already on '{source_sid}' — skip reset+replay")
            reset_needed = False

        elif (
            _requires_valid_login(current_sid, approach_paths)
            and _requires_valid_login(source_sid, approach_paths)
        ):
            # Both current and target screens require valid login — already authenticated.
            # Navigate via sidebar (one click) instead of full logout + replay.
            try:
                navigated = robot.navigate_to_screen(source_sid)
            except AttributeError:
                navigated = False   # backend doesn't support direct navigation

            if navigated:
                new_dom = robot.get_dom_screen_id()
                print(f"    [SMART] Sidebar nav: '{current_dom}' → '{source_sid}' (DOM now: '{new_dom}')")
                reset_needed = False
            else:
                print(f"    [RESET] Sidebar nav to '{source_sid}' unavailable — full reset")

    except Exception as e:
        print(f"    [RESET] DOM check failed ({e}) — full reset")

    if reset_needed:
        _do_reset_and_replay(action, approach_paths, app_map, credentials, captured)

    # ── Execute the queued action ─────────────────────────────────────────────
    _run_steps(action["steps"], action["screen_id"], app_map, credentials, captured)
    time.sleep(1.0)  # allow any page transition to fully settle

    # ── Capture the result screenshot ─────────────────────────────────────────
    safe_key  = full_key.replace("::", "_").replace("/", "-")
    ts        = int(time.time() * 1000)
    save_path = str(Path(settings.screenshots_dir) / f"explore_{safe_key}_{ts}.png")
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    capture      = robot.capture_screen(save_path)
    result_image = capture["image_path"]
    print(f"    captured: {result_image}")

    explored.append(full_key)

    return {
        "exploration_queue":    queue,
        "explored_action_keys": explored,
        "current_image_path":   result_image,
        "last_executed_action": action,
    }
