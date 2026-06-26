"""
execute_action — reset the browser to the source screen, execute the queued
action's steps via the live robot backend, then capture a real screenshot.

Works with any ROBOT_BACKEND (playwright for browser, real for hardware arm).
No demo_navigation dict — every result comes from an actual capture_screen() call.
"""
import time
from pathlib import Path
from vision_agent import robot
from vision_agent.config import settings
from app_explorer.state import ExplorerState, ExplorationAction

def _resolve(value: str, credentials: dict) -> str:
    """Substitute credential placeholders written by SUGGEST_EXPLORABLE_ACTIONS."""
    if not value:
        return value
    valid   = credentials.get("valid", {})
    invalid = credentials.get("invalid", {})
    return (
        value
        .replace("{{valid_email}}",      valid.get("email",    "tester@kiosk.local"))
        .replace("{{valid_password}}",   valid.get("password", "Password123"))
        .replace("{{invalid_email}}",    invalid.get("email",  "baduser@example.com"))
        .replace("{{invalid_password}}", invalid.get("password", "WrongPass!"))
    )


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


def _run_steps(steps: list, screen_id: str, app_map: dict, credentials: dict) -> None:
    """
    Execute a list of ExplorationSteps against the live robot backend.

    For 'type' steps the element is tapped first to acquire focus — the
    SUGGEST_EXPLORABLE_ACTIONS prompt groups type+tap into compound actions
    without explicit focus steps.
    """
    for step in steps or []:
        act = step["action_type"]
        eid = step.get("element_id", "")
        val = _resolve(step.get("value") or "", credentials)
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


def execute_action(state: ExplorerState) -> dict:
    queue          = list(state["exploration_queue"])
    action: ExplorationAction = queue.pop(0)
    explored       = list(state.get("explored_action_keys") or [])
    credentials    = state.get("credentials") or {}
    app_map        = state.get("app_map") or {}
    approach_paths = state.get("approach_paths") or {}

    full_key = f"{action['screen_id']}::{action['action_key']}"
    print(f"\n  [ACTION] {full_key}")
    print(f"           {action['description']}")

    # ── Reset browser to entry URL ────────────────────────────────────────────
    robot.reset_to_entry()
    time.sleep(0.8)

    # ── Replay approach path to reach the source screen ───────────────────────
    approach = approach_paths.get(action["screen_id"], [])
    for past_action in approach:
        print(f"    [REPLAY] {past_action['screen_id']}::{past_action['action_key']}")
        _run_steps(past_action["steps"], past_action["screen_id"], app_map, credentials)
        time.sleep(0.6)

    # ── Execute the queued action ─────────────────────────────────────────────
    _run_steps(action["steps"], action["screen_id"], app_map, credentials)
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
