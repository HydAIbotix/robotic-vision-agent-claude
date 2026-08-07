"""
Real robot backend — connects to the physical robotic arm via its REST API.

Set ROBOT_BACKEND=real in .env to activate, then configure:
  ROBOT_IP, ROBOT_PORT, ROBOT_ID, DEFAULT_KIOSK_ID
  ROBOT_CAMERA_WIDTH, ROBOT_CAMERA_HEIGHT (auto-updated from /capture response)
  ARM_MOVE_TIMEOUT_S, BASE_MOVE_TIMEOUT_S, ROBOT_POLL_INTERVAL_S

Every public function has the SAME signature as playwright_stubs.py and stubs.py
so all agent code is backend-agnostic.

Coordinate mapping
  app_map stores pixel coords in "viewport space" (1400×900, learned in Playwright).
  The robot camera produces a rectified image at a (potentially different) resolution.
  _scale(x, y) converts viewport pixels → camera (u, v) using calibration scale factors.
  Calibration runs automatically on first capture; call calibrate() explicitly for accuracy.

Non-blocking pattern (all tap/swipe/card ops)
  POST command → 202 + {cmd_id}
  Poll the matching GET state endpoint until a TERMINAL state (and, for the arm, cmd_id matches):
    • arm tap/type/swipe → "idle"          (fast, echoes cmd_id)
    • card pick/tap      → "holding_card"   (arm keeps gripping the card; replace → "idle")
    • AGV base move      → "ready" (_poll_base; state authoritative, cmd_id not echoed)
  Timeout → POST abort endpoint → raise TimeoutError

Screen-localization precondition (spec)
  /screen/click and every /card/* require a successful /capture (type:"screen") since the last
  base motion — the kiosk screen pose is derived from that capture, else the call 409s. A base
  move clears the _screen_localized latch; _ensure_localized() re-captures on demand before the
  first screen/card op so a structured Tier-1/2 tap right after arrival can't 409.
"""
import base64
import re
import time
from pathlib import Path
from typing import Optional

import requests

from vision_agent.config import settings

# ── Module-level state ─────────────────────────────────────────────────────────
_keyboard_map: dict = {}
_calibration:  dict = {}          # {"scale_x": float, "scale_y": float}
_current_kiosk_id: str = ""

# Path of the most recent camera frame we have on disk (set by every capture_screen success and by
# the post-tap /screen/click frame). Used to build the annotated "before click" screenshot — the
# frame the arm is about to touch — WITHOUT an extra /capture arm cycle (the previous tap's returned
# frame IS the current screen, since tap reuses capture_after_last).
_last_frame_path: str = ""

# Screen-localization latch. Per the Robot API spec, /screen/click and every /card/* op REQUIRE at
# least one successful screen capture (type:"screen") since the LAST base motion — the kiosk's screen
# pose is derived from that capture, and calling before it returns 409. The base move clears this;
# a successful /capture (or a capture_after_last frame) sets it. _ensure_localized() captures on demand
# so a structured Tier-1/2 tap (which skips analyze) can't 409 as the first op after the AGV arrives.
_screen_localized: bool = False

# Telemetry ring-buffer — recent command events for management frontend polling
_events: list[dict] = []
_MAX_EVENTS = 500

# Optional real-time event sink. The test runner registers a callback (set_event_sink) so robot
# telemetry AND long-poll status ticks reach the live monitor AS THEY HAPPEN, not only after the
# (potentially long) blocking call returns. None in playwright/demo or when no runner is attached.
_event_sink = None

# AGV base terminal states: the mobile base reports "ready" (arrived/settled at a kiosk) rather than
# the arm's "idle". Treat these as "the base has arrived and is holding position".
_BASE_READY_STATES = frozenset({"ready", "idle", "arrived", "done", "reached"})

# Arm "available & responsive" states for the health probe. The physical arm controller reports
# "ready" when idle-and-available (same convention as the base), not the spec's "idle" — so a
# health check must accept "ready" as healthy. "holding_card" is a valid mid-flow gripping state.
_ARM_HEALTHY_STATES = frozenset({"idle", "ready", "holding_card"})

# Arm command-COMPLETE states for _poll (tap/type/swipe). The real arm settles to "ready" after a tap
# (verified on hardware 2026-07-28: a click completed physically but the poll waited for "idle" and
# timed out at 30s). Both "ready" and "idle" mean "done". Card ops override with "holding_card".
_ARM_TERMINAL_STATES = frozenset({"idle", "ready"})

# Monotonic command counter → human-readable command ids (cmd-1-goto-VPS, cmd-2-arm-click, …).
# Reset at the start of each run by reset_command_seq() so ids read 1..N per run.
_cmd_seq = 0


# ── Internal helpers ───────────────────────────────────────────────────────────

def _arm_base_url() -> str:
    """Base URL for arm, camera, screen and card endpoints."""
    return settings.arm_api_base()


def _agv_base_url() -> str:
    """Base URL for the mobile-base (AGV) /base/* endpoints."""
    return settings.agv_api_base()


def _base_for(endpoint: str) -> str:
    """Pick the controller URL for an endpoint: /base/* → AGV, everything else → arm."""
    ep = "/" + endpoint.lstrip("/")
    return _agv_base_url() if ep.startswith("/base") else _arm_base_url()


def reset_command_seq() -> None:
    """Restart the human-readable command counter (call once at the start of a run)."""
    global _cmd_seq
    _cmd_seq = 0


def _new_cmd_id(label: str = "cmd") -> str:
    """Self-explanatory command id: cmd-<n>-<label>, e.g. cmd-1-goto-VPS, cmd-3-arm-click.

    The counter increments per robot command so a user reading the live monitor / robot event log
    can tell exactly which command each id refers to and in what order it ran.
    """
    global _cmd_seq
    _cmd_seq += 1
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", str(label)).strip("-") or "cmd"
    return f"cmd-{_cmd_seq}-{safe}"


def set_event_sink(sink) -> None:
    """Register (or clear with None) a callback the runner uses to stream robot telemetry / AGV
    status to the live monitor in real time. Signature: sink(event: dict) -> None. Optional — the
    ring-buffer + get_events() polling still works when no sink is attached."""
    global _event_sink
    _event_sink = sink


def _now_hms(epoch: float) -> str:
    """Format an epoch time as a user-friendly local hh:mm:ss.mmm (millisecond precision)."""
    lt = time.localtime(epoch)
    ms = int((epoch - int(epoch)) * 1000)
    return f"{time.strftime('%H:%M:%S', lt)}.{ms:03d}"


def _host_of(url: str) -> str:
    """Strip a base URL down to host[:port] for compact, verifiable logging (which controller was hit)."""
    return re.sub(r"^https?://", "", url or "").split("/")[0]


def _push(evt: dict) -> None:
    """Push an event to the live sink (if attached) immediately — used for real-time progress
    (AGV status ticks) that must reach the monitor DURING a blocking call, not after it returns.
    These are NOT stored in the _events ring-buffer, so the runner's post-step pull-flush (which
    replays _events) never double-emits them."""
    if _event_sink is not None:
        try:
            _event_sink(evt)
        except Exception:
            pass


def _record(event_type: str, endpoint: str, cmd_id: str,
            t0: float, t1: float, status: int, extra: dict, base_url: str = "") -> None:
    host = _host_of(base_url or _base_for(endpoint))
    evt = {
        "event_type":     event_type,
        "endpoint":       endpoint,
        "cmd_id":         cmd_id,
        "robot_id":       settings.robot_id,
        "controller":     host,             # which controller (AGV vs arm) actually served this call
        "request_at":     t0,
        "response_at":    t1,
        "request_time":   _now_hms(t0),     # user-friendly hh:mm:ss.mmm
        "response_time":  _now_hms(t1),
        "latency_ms":     round((t1 - t0) * 1000, 1),
        "http_status":    status,
        **extra,
    }
    # Buffer for the runner's post-step pull-flush (and the management get_events() poll).
    _events.append(evt)
    if len(_events) > _MAX_EVENTS:
        _events.pop(0)
    # Console line mirrored to the run log — the runner also surfaces these in the live monitor.
    print(f"    [ROBOT API] {evt['request_time']} → {evt['response_time']} "
          f"{event_type} {endpoint}{(' (' + cmd_id + ')') if cmd_id else ''} "
          f"@ {host} → {status} in {evt['latency_ms']}ms")


def _status(endpoint: str, cmd_id: str, state: str, feedback: dict, base_url: str = "") -> None:
    """Emit a real-time AGV progress tick (not an HTTP call record) while long-polling a move.
    Surfaces the base state ('moving'/'ready') and nav feedback (distance remaining) to the live
    monitor every poll so the user can watch the AGV approach the kiosk instead of a silent wait."""
    now  = time.time()
    host = _host_of(base_url or _base_for(endpoint))
    dist = (feedback or {}).get("distance_remaining")
    nav  = (feedback or {}).get("nav2_state")
    evt = {
        "event_type":    "STATUS",
        "endpoint":      endpoint,
        "cmd_id":        cmd_id,
        "robot_id":      settings.robot_id,
        "controller":    host,
        "response_at":   now,
        "response_time": _now_hms(now),
        "state":         state,
        "distance_remaining": round(dist, 3) if isinstance(dist, (int, float)) else dist,
        "nav2_state":    nav,
    }
    _push(evt)
    extra = ""
    if isinstance(dist, (int, float)):
        extra += f", {dist:.2f}m remaining"
    if nav:
        extra += f", nav2={nav}"
    print(f"    [ROBOT AGV] {evt['response_time']} {endpoint}"
          f"{(' (' + cmd_id + ')') if cmd_id else ''} @ {host} — state='{state}'{extra}")


def _resp_timeout(timeout: Optional[float] = None) -> float:
    """Per-call HTTP response timeout — configurable via settings.robot_response_timeout_s (2s
    default). Every robot REST call uses this so a hung/slow robot fails fast and gracefully
    instead of stalling the suite. Callers may override for genuinely long single calls (capture)."""
    return timeout if timeout is not None else settings.robot_response_timeout_s


def _post_to(base: str, endpoint: str, body: dict, timeout: Optional[float] = None) -> dict:
    url = f"{base}/{endpoint.lstrip('/')}"
    t0  = time.time()
    resp = requests.post(url, json=body, timeout=_resp_timeout(timeout))
    t1  = time.time()
    _record("POST", endpoint, body.get("cmd_id", ""), t0, t1, resp.status_code, {}, base_url=base)
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, body: dict, timeout: Optional[float] = None) -> dict:
    return _post_to(_base_for(endpoint), endpoint, body, timeout)


def _get(endpoint: str, timeout: Optional[float] = None) -> dict:
    base = _base_for(endpoint)
    url  = f"{base}/{endpoint.lstrip('/')}"
    t0  = time.time()
    resp = requests.get(url, timeout=_resp_timeout(timeout))
    t1  = time.time()
    _record("GET", endpoint, "", t0, t1, resp.status_code, {}, base_url=base)
    resp.raise_for_status()
    return resp.json()


def _get_quiet(endpoint: str, timeout: Optional[float] = None) -> dict:
    """GET without emitting an [ROBOT API] telemetry record. Used inside long base-state polling so
    the monitor shows one consolidated AGV STATUS tick per interval instead of dozens of raw GET
    lines. Still routed strictly by _base_for (/base/* → AGV URL, else arm URL)."""
    base = _base_for(endpoint)
    resp = requests.get(f"{base}/{endpoint.lstrip('/')}", timeout=_resp_timeout(timeout))
    resp.raise_for_status()
    return resp.json()


def _arm_status(endpoint: str, cmd_id: str, state: str, label: str, elapsed: float) -> None:
    """Push ONE consolidated [ROBOT ARM] progress tick to the live monitor while polling an arm
    command, so the operator sees 'what the arm is doing' instead of dozens of raw GET /arm/state
    lines. Not an HTTP record and not stored in the ring-buffer (delivered via the push sink), so the
    runner's post-step pull-flush never double-emits it. Mirrors the AGV _status tick."""
    now  = time.time()
    host = _host_of(_base_for(endpoint))
    evt  = {
        "event_type":    "STATUS",
        "endpoint":      endpoint,
        "cmd_id":        cmd_id,
        "robot_id":      settings.robot_id,
        "controller":    host,
        "response_at":   now,
        "response_time": _now_hms(now),
        "state":         state,
        "operation":     label,
        "elapsed_s":     round(elapsed, 1),
    }
    _push(evt)
    print(f"    [ROBOT ARM] {evt['response_time']} {label or endpoint}"
          f"{(' (' + cmd_id + ')') if cmd_id else ''} @ {host} — state='{state}' ({elapsed:.0f}s)")


def _recover_arm() -> None:
    """Recover the arm to a known-safe state after an ERROR or a stuck-moving TIMEOUT, per the spec
    ('use /arm/abort + /arm/command {action:"home"} to recover from error states before issuing new
    commands'). Best-effort and NEVER raises — recovery must not mask the original failure. Bounded
    wait for the home move to settle; does NOT recurse into _poll."""
    try:
        _post("/arm/abort", {"cmd_id": _new_cmd_id("arm-abort")},
              timeout=settings.robot_response_timeout_s)
    except Exception:
        pass
    try:
        _post("/arm/command", {"cmd_id": _new_cmd_id("arm-home"), "action": "home"},
              timeout=settings.robot_response_timeout_s)
    except Exception as exc:
        print(f"    [ROBOT ARM] home command failed during recovery (ignored): {exc}")
        return
    deadline = time.time() + settings.arm_move_timeout_s
    while time.time() < deadline:
        try:
            st = str(_get_quiet("/arm/state").get("state", "")).lower()
        except Exception:
            break
        if st and st != "moving":
            print(f"    [ROBOT ARM] recovered to home (state='{st}')")
            return
        time.sleep(settings.robot_poll_interval_s)
    print("    [ROBOT ARM] home recovery did not confirm within timeout")


def _poll(
    state_ep: str,
    cmd_id:   str,
    timeout_s: float,
    abort_ep: Optional[str] = None,
    terminal_states: Optional[tuple] = None,
    initial_state: str = "",
    label: str = "",
    recover_on_fail: bool = True,
) -> dict:
    """Poll GET state_ep until the ARM command FINISHES, per the robot API spec: the click/type result
    is available 'once state is no longer moving'. Returns the final state dict (the caller inspects
    click_result for completed/total). Used for the ARM (taps / typing / swipes / card ops); the AGV
    base uses _poll_base.

    Completion is STATE-AUTHORITATIVE — the arm echoes its OWN last cmd_id (e.g. 'c-030'), never the
    one we sent, so we do NOT gate on a cmd_id match (doing so caused 30s timeouts). Rules:
      • state == "error"          → the command FAILED (spec surfaces errors via the state endpoint);
                                     recover (abort + home) and raise.
      • state == "moving"         → still in progress; keep polling.
      • any other non-empty state → the command is DONE (idle / ready / holding_card / …). Return it.
        When terminal_states is given (card ops), completion ADDITIONALLY requires the state to be in
        that set, so a card pick isn't 'done' at a bare 'idle' mid-motion.

    Race guard: if the POST ack reported the command 'moving' (initial_state), we wait to OBSERVE a
    'moving' sample — or a short settle grace (arm_settle_grace_s) — before accepting a terminal
    state, so a STALE pre-command 'ready' can't be misread as instant completion.

    Live feed: polls QUIETLY (via _get_quiet — no per-GET spam) and pushes ONE consolidated
    [ROBOT ARM] status tick per arm_status_tick_s while the arm is busy. On timeout (arm stuck in
    'moving'): abort + recover, then raise TimeoutError."""
    deadline      = time.time() + timeout_s
    start         = time.time()
    expect_moving = str(initial_state).lower() == "moving"
    seen_moving   = False
    last_tick     = 0.0
    last_state: dict = {}
    first_conn_err: float = 0.0   # when the API first became unreachable during this poll (0 = reachable)
    unreachable_after = settings.robot_unreachable_timeout_s
    if label:
        _arm_status(state_ep, cmd_id, str(initial_state or "moving"), label, 0.0)
        last_tick = time.time()

    while time.time() < deadline:
        try:
            last_state = _get_quiet(state_ep)
            first_conn_err = 0.0   # reachable again → reset the unreachable timer
        except Exception as exc:
            # A transient state-read error shouldn't abort the whole command; retry — BUT if the robot
            # API stays UNREACHABLE (connection refused / read timeout on every poll) beyond
            # robot_unreachable_timeout_s, fail fast instead of retrying for the whole (possibly long,
            # e.g. 345s for a 19-key type) command deadline. Recovery is skipped — abort/home would
            # only hit the same dead API. This is the "REST API not running on the robot machine" case.
            now = time.time()
            if first_conn_err == 0.0:
                first_conn_err = now
            waited = now - first_conn_err
            if waited >= unreachable_after:
                raise ConnectionError(
                    f"Robot {state_ep} unreachable for {waited:.0f}s (>= {unreachable_after:.0f}s) "
                    f"for {label or cmd_id} — robot REST API not responding: {exc}"
                )
            print(f"    [ROBOT ARM] state read error (retrying, unreachable {waited:.0f}/{unreachable_after:.0f}s): {exc}")
            time.sleep(settings.robot_poll_interval_s)
            continue
        s       = str(last_state.get("state", "")).lower()
        elapsed = time.time() - start
        now     = time.time()
        if label and (now - last_tick) >= settings.arm_status_tick_s:
            _arm_status(state_ep, cmd_id, s or "?", label, elapsed)
            last_tick = now

        if s == "error":
            if label:
                _arm_status(state_ep, cmd_id, "error", f"{label} — FAILED", elapsed)
            if recover_on_fail:
                _recover_arm()
            raise RuntimeError(
                f"Robot {state_ep} reported state 'error' for {label or cmd_id}: {last_state}")

        if s == "moving":
            seen_moving = True
        elif s:
            # Non-empty, non-moving, non-error → potential completion.
            accepted = (s in terminal_states) if terminal_states else True
            if accepted and (seen_moving or not expect_moving or elapsed >= settings.arm_settle_grace_s):
                if label:
                    _arm_status(state_ep, cmd_id, s, f"{label} — done", elapsed)
                return last_state
        time.sleep(settings.robot_poll_interval_s)

    # Timed out — the arm never left 'moving'. Abort + recover so the NEXT command starts clean.
    if abort_ep:
        try:
            _abort_label = abort_ep.strip("/").replace("/", "-")  # /arm/abort → arm-abort
            _post(abort_ep, {"cmd_id": _new_cmd_id(_abort_label)}, timeout=settings.robot_response_timeout_s)
        except Exception:
            pass
    if recover_on_fail:
        _recover_arm()
    raise TimeoutError(
        f"Robot {state_ep} timed out after {timeout_s}s for {label or cmd_id} "
        f"(last state: {last_state.get('state')!r})"
    )


def _poll_base(cmd_id: str, timeout_s: float, initial_state: str = "") -> dict:
    """Poll /base/state until the AGV ARRIVES, emitting a live status tick each interval.

    Arrival contract (per the real AGV controller, confirmed on hardware 2026-07-13):
      • the base reports state "moving" while navigating, then "ready" once it reaches and settles
        at the target — so we wait for a READY state (see _BASE_READY_STATES), NOT the arm's "idle";
      • /base/state does NOT reliably echo our cmd_id (its sample returns a placeholder), so the
        STATE field is authoritative for arrival — we do not gate on a cmd_id match here;
      • state "error" fails fast.
    Polls every settings.base_poll_interval_s (2s default, configurable). Each tick surfaces the
    state + nav_feedback (distance remaining) to the live monitor via _status so the move is visible
    instead of a silent wait. On timeout: POST /base/abort (best effort) then raise TimeoutError.
    Strictly uses the AGV URL (all /base/* calls route to agv_api_base via _base_for)."""
    if initial_state:
        _status("/base/goto", cmd_id, initial_state, {})   # immediate status from the goto response
        if initial_state.lower() in _BASE_READY_STATES:
            return {"state": initial_state, "cmd_id": cmd_id}

    deadline   = time.time() + timeout_s
    last_state: dict = {}
    while time.time() < deadline:
        last_state = _get_quiet("/base/state")
        s = str(last_state.get("state", "")).lower()
        _status("/base/state", cmd_id, s or "?", last_state.get("nav_feedback") or {})
        if s == "error":
            raise RuntimeError(f"AGV base error on /base/state: {last_state}")
        if s in _BASE_READY_STATES:
            return last_state
        time.sleep(settings.base_poll_interval_s)

    # Timed out — attempt graceful abort so the base doesn't keep driving.
    try:
        _post("/base/abort", {"cmd_id": _new_cmd_id("base-abort")},
              timeout=settings.robot_response_timeout_s)
    except Exception:
        pass
    raise TimeoutError(
        f"AGV /base/state timed out after {timeout_s}s "
        f"(last state: {last_state.get('state')!r}) — base never reached a ready state"
    )


def _annotate_click(src_path: str, points: list[tuple[int, int]], save_path: str) -> str:
    """Draw a crosshair + circle (and the pixel coordinate) at each camera-space (u,v) the arm will
    touch, on a COPY of the given camera frame. Returns save_path on success, "" on any failure.
    Pure PIL (already a dep); never raises — a screenshot aid must not break a tap."""
    try:
        from PIL import Image, ImageDraw
        im = Image.open(src_path).convert("RGB")
        d  = ImageDraw.Draw(im)
        r  = 16
        for (u, v) in points:
            d.ellipse([u - r, v - r, u + r, v + r], outline=(255, 0, 0), width=3)
            d.line([u - r - 10, v, u + r + 10, v], fill=(255, 0, 0), width=2)
            d.line([u, v - r - 10, u, v + r + 10], fill=(255, 0, 0), width=2)
            d.text((u + r + 4, v + 4), f"({u},{v})", fill=(255, 0, 0))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        im.save(save_path)
        return save_path
    except Exception as exc:
        print(f"    [ROBOT] before-click annotation failed: {exc}")
        return ""


def _scale(x: int, y: int) -> tuple[int, int]:
    """Scale viewport pixel (x,y) → robot camera (u,v)."""
    sx = _calibration.get("scale_x") or (settings.robot_camera_width  / settings.viewport_width)
    sy = _calibration.get("scale_y") or (settings.robot_camera_height / settings.viewport_height)
    return int(round(x * sx)), int(round(y * sy))


def _ensure_localized() -> None:
    """Guarantee the kiosk screen is localized before a /screen/click or /card/* op.

    Spec precondition: those endpoints need a successful /capture (type:"screen") since the last base
    motion, or they 409. In the vision flow analyze_screen already captures first, so this is a no-op;
    it only fires for a structured Tier-1/2 tap that arrives right after an AGV move and would
    otherwise be the first screen op with no capture yet. One capture localizes the screen for all
    subsequent taps until the base moves again (which re-clears the latch)."""
    global _screen_localized
    if _screen_localized:
        return
    save_path = str(Path(settings.screenshots_dir) / f"localize_{int(time.time() * 1000)}.png")
    print("  [ROBOT] screen not localized since last base move — capturing to establish screen pose")
    capture_screen(save_path)   # sets _screen_localized on success


# ── Public robot interface (identical signature to playwright_stubs / stubs) ────

def set_keyboard_map(kmap: dict) -> None:
    """Store virtual-keyboard normalized coords from app_map."""
    global _keyboard_map
    _keyboard_map = kmap.get("keys", kmap)


def set_demo_screens(paths: list) -> None:
    pass  # no-op — real robot uses live camera


def reset_to_entry() -> None:
    """No URL bar on physical kiosk — robot is already positioned; no action needed."""
    pass


def get_dom_screen_id() -> str:
    return ""  # DOM not accessible from robot camera


def get_dom_element_centers() -> list[dict]:
    return []


def navigate_to_screen(screen_id: str) -> bool:
    return False  # nav buttons must be tapped via app_map coords


def scroll_page(x: int, y: int, delta_y: int) -> dict:
    swipe(x, y, x, y - delta_y, duration_ms=400)
    return {"success": True, "delta_y": delta_y}


def get_page_scroll_info() -> dict:
    return {
        "scrollTop": 0, "scrollLeft": 0,
        "scrollHeight":   settings.viewport_height,
        "scrollWidth":    settings.viewport_width,
        "viewportHeight": settings.viewport_height,
        "viewportWidth":  settings.viewport_width,
    }


def update_explorer_progress(explored: int, total: int, current_action: str = "") -> None:
    pass  # HUD overlay only works in Playwright mode


# ── Core robot operations ──────────────────────────────────────────────────────

def capture_screen(save_path: str) -> dict:
    """
    Capture kiosk screen via robot arm camera.
    Blocking — robot API returns image directly (not a poll pattern).
    Auto-updates calibration scale factors from the returned image dimensions.
    """
    global _screen_localized, _last_frame_path
    cmd_id = _new_cmd_id("capture")
    t0   = time.time()
    resp = requests.post(
        f"{_arm_base_url()}/capture",
        json={"cmd_id": cmd_id, "type": "screen"},   # spec: every command carries a cmd_id
        # /capture is blocking and moves the arm to an inspection pose first — needs a generous
        # timeout, not the 2s per-call default (which read-timed-out after a failed tap left the arm
        # away from the inspect pose).
        timeout=_resp_timeout(settings.capture_timeout_s),
    )
    t1 = time.time()
    _record("POST", "/capture", cmd_id, t0, t1, resp.status_code, {})
    resp.raise_for_status()
    data = resp.json()

    img_bytes = base64.b64decode(data["image_b64"])
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    Path(save_path).write_bytes(img_bytes)
    # A successful type:"screen" capture establishes the screen pose used by /screen/click & /card/*.
    _screen_localized = True
    _last_frame_path  = save_path   # newest camera frame — source for the next "before click" image

    # Update calibration whenever we learn new camera dimensions
    w, h = data.get("width"), data.get("height")
    if w and h:
        _calibration["scale_x"] = w / settings.viewport_width
        _calibration["scale_y"] = h / settings.viewport_height

    return {
        "success":    True,
        "image_path": save_path,
        "timestamp":  data.get("timestamp", t1),
        "width":      w,
        "height":     h,
    }


def _check_click_completed(state: dict, post_resp: dict, cmd_id: str) -> None:
    """Raise if the /screen/click sequence reported a failed/partial completion.

    Per the robot API spec, GET /arm/state after a click carries a ``click_result`` with
    ``completed``/``total`` (plus ``code``/``failed_index``/``detail`` on failure).  The physical arm
    can return a TERMINAL state ("ready"/"idle") even when the touch itself FAILED — observed on
    hardware 2026-07-29: a ``DESCEND_LIN_FAILED`` left ``completed=0/total=1`` while the state went
    back to idle, so our state-only poll treated a click that never landed as success and the run
    proceeded to type into an unfocused field.  The state alone is therefore not sufficient; inspect
    the click_result.

    Only raises when the result is PRESENT and explicitly incomplete — a missing click_result (no
    ``capture_after_last``, or an older controller) is treated as OK, so this never manufactures a
    false failure for callers that don't request a completion frame."""
    cr = state.get("click_result") or post_resp.get("click_result") or {}
    total     = cr.get("total")
    completed = cr.get("completed")
    if total is not None and completed is not None and completed < total:
        code   = cr.get("code")
        detail = cr.get("detail") or code or "click did not land"
        extra  = f" (code={code}, failed_index={cr.get('failed_index')})" if code else ""
        raise RuntimeError(
            f"Arm click {cmd_id} did NOT land: completed {completed}/{total}{extra} — {detail}"
        )


def tap(x: int, y: int) -> dict:
    """Physically tap kiosk touchscreen at viewport pixel (x, y).

    The /screen/click completion returns the post-tap camera frame (image_b64). We
    decode and save it, then surface it as image_path so callers can reuse it for
    verification without a separate /capture round-trip (saves an arm cycle).
    """
    global _last_frame_path
    _ensure_localized()   # /screen/click 409s without a capture since the last base motion
    u, v   = _scale(x, y)
    cmd_id = _new_cmd_id("arm-click")
    print(f"    [ROBOT] tap viewport({x},{y}) → camera({u},{v})")

    # BEFORE screenshot: mark the exact camera pixel the arm is about to touch on the most recent
    # frame, so an operator can verify tap accuracy. Uses the cached last frame (no extra /capture) —
    # after _ensure_localized() this is guaranteed to exist for the first tap.
    before_path = ""
    if settings.save_click_screenshots and _last_frame_path and Path(_last_frame_path).exists():
        before_path = _annotate_click(
            _last_frame_path, [(u, v)],
            str(Path(settings.screenshots_dir) / f"before_{cmd_id}_at_{u}-{v}.png"),
        )
    # capture_after_last:true → the completion (GET /arm/state) carries a fresh rectified frame
    # (click_result.image_b64) taken delay_between_ms after the tap. We reuse it for verification
    # without a separate /capture arm cycle. delay_between_ms doubles as the post-tap settle so the
    # returned frame is captured AFTER the kiosk finishes its transition (login/render 300-700ms).
    post_resp = _post("/screen/click", {
        "cmd_id":             cmd_id,
        "points":             [{"u": u, "v": v}],
        "capture_after_last": True,
        "delay_between_ms":   800,
    })
    state = _poll("/arm/state", cmd_id, settings.arm_move_timeout_s, abort_ep="/arm/abort",
                  initial_state=post_resp.get("state", ""), label=f"tap ({x},{y})")
    # The arm can report a TERMINAL state even when the touch itself failed to land (e.g. a linear
    # descend that could not be planned) — verify the click_result actually completed, else raise so
    # the runner fails this step and hands off to Tier-3 instead of typing into an unfocused field.
    _check_click_completed(state, post_resp, cmd_id)
    # After the arm confirms tap complete, the kiosk still needs time to process the touch
    # event and complete any navigation (e.g. login API call + React re-render takes 300-700ms).
    # 0.2s was too short and caused the next camera capture to land mid-transition.
    time.sleep(0.8)

    result = {"success": True, "x": x, "y": y, "u": u, "v": v}
    if before_path:
        result["before_image_path"] = before_path
    # The camera frame may arrive in the click ack or in the completion state — check both.
    click_result = state.get("click_result") or post_resp.get("click_result") or {}
    b64 = click_result.get("image_b64")
    if b64:
        try:
            fmt = (click_result.get("format") or "jpeg").lower()
            ext = "jpg" if fmt in ("jpg", "jpeg") else fmt
            # AFTER screenshot = the frame the /screen/click response returned (post-tap, post-settle).
            save_path = str(Path(settings.screenshots_dir) / f"after_{cmd_id}.{ext}")
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            Path(save_path).write_bytes(base64.b64decode(b64))
            result["image_path"]       = save_path   # back-compat: callers reuse this for the next verify
            result["after_image_path"] = save_path
            _last_frame_path           = save_path    # this IS the current screen → next tap's "before"
            w, h = click_result.get("width"), click_result.get("height")
            if w and h:
                _calibration["scale_x"] = w / settings.viewport_width
                _calibration["scale_y"] = h / settings.viewport_height
                result["width"], result["height"] = w, h
        except Exception as exc:
            print(f"    [ROBOT] click image decode failed: {exc}")
    return result


def _image_similarity(path1: str, path2: str) -> float:
    """Pixel-level similarity between two images.  Returns 0.0 (different) – 1.0 (identical).

    Images are resized to 160×100 before comparison so the cost is negligible (~2 ms).
    Uses mean squared error over RGB channels — no extra dependencies beyond PIL/numpy.
    """
    try:
        from PIL import Image
        import numpy as np
        TARGET = (160, 100)
        img1 = np.array(Image.open(path1).convert("RGB").resize(TARGET), dtype=float)
        img2 = np.array(Image.open(path2).convert("RGB").resize(TARGET), dtype=float)
        mse  = float(np.mean((img1 - img2) ** 2))
        return 1.0 - mse / (255.0 ** 2)
    except Exception:
        return 0.0


def verify_current_screen(expected_screen_id: str, app_map: dict, save_path: str = "") -> dict:
    """Camera-based screen verification (real-robot backend).

    Captures the kiosk screen via the robot arm camera, then identifies it by TEMPLATE MATCHING
    (normalized cross-correlation, TM_CCOEFF_NORMED) against each screen's reference image — far
    more robust to the browser↔camera domain gap than a raw pixel MSE. References come from
    template_ref_dir overrides (clean per-screen templates) then the app_map reference_screenshot.
    No LLM call. Falls back to the legacy pixel-MSE similarity only if the template module is
    unavailable, so behaviour never hard-fails.

    Returns {"actual_screen": str, "match": bool, "method": str, "confidence": float, "screenshot": str}.
    "actual_screen" is the best-scoring screen id; empty when none reaches the match threshold.
    """
    if not save_path:
        save_path = f"./screenshots/verify_{int(time.time() * 1000)}.png"

    result       = capture_screen(save_path)
    current_path = result["image_path"]
    threshold    = settings.template_match_threshold

    # Primary: TM_CCOEFF_NORMED template matching (robust to camera↔browser domain gap).
    try:
        from vision_agent.vision.template_match import (
            build_references, rank_references, settings_center_crop,
        )
        refs    = build_references(app_map or {}, settings.template_ref_dir)
        ranking = (rank_references(Path(current_path).read_bytes(), refs,
                                   center_crop=settings_center_crop())
                   if refs else [])
        if ranking:
            best_screen = ranking[0]["screen_id"]
            best_score  = ranking[0]["score"]
            if best_score < threshold:
                return {"actual_screen": "", "match": False, "method": "template_match",
                        "confidence": round(max(best_score, 0.0), 3), "screenshot": current_path}
            return {"actual_screen": best_screen,
                    "match":         (best_screen == expected_screen_id),
                    "method":        "template_match",
                    "confidence":    round(best_score, 3),
                    "screenshot":    current_path}
    except Exception as exc:
        print(f"    [VERIFY] template match unavailable ({exc}); using pixel-MSE fallback")

    # Fallback: legacy pixel-MSE similarity against reference_screenshot.
    screens     = (app_map or {}).get("screens", {})
    best_screen = ""
    best_score  = -1.0
    for screen_id, screen_data in screens.items():
        ref_path = (screen_data or {}).get("reference_screenshot", "")
        if not ref_path or not Path(ref_path).exists():
            continue
        score = _image_similarity(current_path, ref_path)
        if score > best_score:
            best_score  = score
            best_screen = screen_id

    if not best_screen or best_score < 0.70:
        return {"actual_screen": "", "match": False, "method": "camera_reference",
                "confidence": round(max(best_score, 0.0), 3), "screenshot": current_path}
    return {"actual_screen": best_screen, "match": (best_screen == expected_screen_id),
            "method": "camera_reference", "confidence": round(best_score, 3),
            "screenshot": current_path}


def type_text(text: str, clear_first: bool = False) -> dict:
    """
    Type text by tapping each character's key on the kiosk virtual keyboard.
    Uses app_map keyboard_map (normalized 0-1 coords) loaded by set_keyboard_map().
    All key taps are batched into a single /screen/click call (fast).
    clear_first: no-op for real robot (no select-all gesture available).
    """
    if not _keyboard_map:
        print(f"    [ROBOT] type({text!r}) — keyboard_map not loaded; skipping")
        return {"success": False, "error": "keyboard_map not loaded", "text": text}

    _ensure_localized()   # keyboard taps go through /screen/click → same capture precondition

    # Derive camera dimensions from calibration (or settings fallback)
    cam_w = (_calibration.get("scale_x") or (settings.robot_camera_width  / settings.viewport_width)) * settings.viewport_width
    cam_h = (_calibration.get("scale_y") or (settings.robot_camera_height / settings.viewport_height)) * settings.viewport_height

    points: list[dict] = []
    skipped: list[str] = []

    for char in text:
        # Uppercase: tap Shift first
        if char.isupper() and char.isalpha():
            shift = _keyboard_map.get("shift")
            if shift:
                points.append({"u": int(shift[0] * cam_w), "v": int(shift[1] * cam_h)})

        lookup = char.lower() if char.isalpha() else char
        if char == " ":
            lookup = "space"

        coords = _keyboard_map.get(lookup)
        if coords:
            points.append({"u": int(coords[0] * cam_w), "v": int(coords[1] * cam_h)})
        else:
            skipped.append(char)

    if skipped:
        print(f"    [ROBOT] type: keys not in keyboard_map — skipped: {skipped!r}")

    char_points = points   # taps that actually enter characters

    # A caller that asked to type real characters, none of which are in the keyboard_map, cannot
    # enter the value — report failure so the runner trips Tier-3 (matches the "no keyboard_map"
    # contract) instead of silently reporting success on an empty field.
    if text.strip() and not char_points:
        print(f"    [ROBOT] type({text!r}) — no characters matched the keyboard_map; cannot type")
        return {"success": False, "error": "no characters in keyboard_map", "text": text}

    # Dismiss the on-screen keyboard by tapping its Done/return/enter key AFTER the characters —
    # mirrors the playwright backend.  Without this the keyboard stays open and overlaps the next
    # input, so the following focus tap lands on a key instead of the field (observed on RPS: the
    # password field stayed covered after the email type, and the next arm move hung).  Also lets
    # type_text("") act as a keyboard-dismiss (the App Explorer relies on that).
    dismiss = (_keyboard_map.get("done") or _keyboard_map.get("return")
               or _keyboard_map.get("enter"))
    all_points = list(char_points)
    if dismiss:
        all_points.append({"u": int(dismiss[0] * cam_w), "v": int(dismiss[1] * cam_h)})

    if not all_points:
        return {"success": True, "text": text, "tapped_keys": 0}

    cmd_id = _new_cmd_id("arm-type")
    print(f"    [ROBOT] type({text!r}) — {len(char_points)} key taps" + (" + Done" if dismiss else ""))
    type_resp = _post("/screen/click", {
        "cmd_id":           cmd_id,
        "points":           all_points,
        "delay_between_ms": 80,   # 80 ms between each key
    })
    # Each key is its own physical hover→descend→touch→ascend on the arm, executed sequentially, so
    # the deadline scales with the number of taps (see arm_key_tap_timeout_s) — NOT a flat +0.1s/key,
    # which timed out mid-word on the real arm.
    type_timeout = settings.arm_move_timeout_s + len(all_points) * settings.arm_key_tap_timeout_s
    state = _poll("/arm/state", cmd_id, type_timeout, abort_ep="/arm/abort",
                  initial_state=type_resp.get("state", ""),
                  label=f"type {text[:20]!r} ({len(all_points)} taps)")
    # If a key tap failed mid-sequence the remaining keys are aborted (completed < total) → the value
    # was only partially entered.  Report failure so the runner re-tries via Tier-3 rather than
    # proceeding with a half-typed field.
    try:
        _check_click_completed(state, type_resp, cmd_id)
    except RuntimeError as exc:
        print(f"    [ROBOT] type({text!r}) — {exc}")
        return {"success": False, "error": str(exc), "text": text}
    return {"success": True, "text": text, "tapped_keys": len(char_points)}


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
    """Swipe from (x1,y1) to (x2,y2) via two sequential taps with delay."""
    _ensure_localized()
    u1, v1 = _scale(x1, y1)
    u2, v2 = _scale(x2, y2)
    cmd_id = _new_cmd_id("arm-swipe")
    print(f"    [ROBOT] swipe ({x1},{y1})→({x2},{y2})  [{duration_ms}ms]")
    swipe_resp = _post("/screen/click", {
        "cmd_id":           cmd_id,
        "points":           [{"u": u1, "v": v1}, {"u": u2, "v": v2}],
        "delay_between_ms": duration_ms,
    })
    state = _poll("/arm/state", cmd_id, settings.arm_move_timeout_s, abort_ep="/arm/abort",
                  initial_state=swipe_resp.get("state", ""), label=f"swipe ({x1},{y1})->({x2},{y2})")
    _check_click_completed(state, swipe_resp, cmd_id)
    return {"success": True}


# ── Setup & navigation (called once per test suite) ────────────────────────────

def setup(kiosk_definitions: list[dict], arm_poses: dict, nav_map: dict) -> dict:
    """
    Upload kiosk definitions, arm rest/home poses, and navigation map.
    Call once per robot session before any navigate_to_kiosk().
    """
    # /setup is a BLOCKING 200 (not a 202 command), so per the spec it carries no cmd_id; the map
    # payload is keyed "map" (base64 map data), not "nav_map".
    body = {
        "robot_id":  settings.robot_id,
        "kiosks":    kiosk_definitions,
        "arm_poses": arm_poses,
        "map":       nav_map,
    }
    # /setup is common to both controllers (nav_map for the AGV, arm_poses for the arm).
    arm, agv = _arm_base_url(), _agv_base_url()
    targets = [arm] if arm == agv else [arm, agv]
    print(f"  [ROBOT] setup — {len(kiosk_definitions)} kiosk(s) → {len(targets)} controller(s)")
    result: dict = {}
    for base in targets:
        result = _post_to(base, "/setup", body)
    return result


def navigate_to_kiosk(kiosk_id: str, timeout_s: Optional[float] = None) -> dict:
    """Drive the mobile base to kiosk_id; block until the robot arrives.

    The AGV controller drives to a NAMED position from its pre-built map ("kiosk-1", "kiosk-2",
    "home"), so /base/goto expects a `target` field holding that name — which is exactly the
    kiosk_id (the Device-Map join key) resolved by the runner from the test-step alias, or the
    reserved "home". x/y/theta are not needed here (see move_to_position for the pose fallback)."""
    global _current_kiosk_id, _screen_localized
    _screen_localized = False   # base motion invalidates the screen pose → must re-capture before taps
    t = timeout_s or settings.base_move_timeout_s
    cmd_id = _new_cmd_id(f"goto-{kiosk_id}")
    print(f"  [ROBOT] navigate_to_kiosk({kiosk_id!r})  timeout={t}s")
    goto = _post("/base/goto", {"target": kiosk_id, "cmd_id": cmd_id})
    # Use the goto response's immediate state ("moving") for the first live status, then poll
    # /base/state (strictly the AGV URL) until it reports a ready state.
    result = _poll_base(cmd_id, t, initial_state=str((goto or {}).get("state", "")))
    _current_kiosk_id = kiosk_id
    print(f"  [ROBOT] arrived at {kiosk_id!r}  (state: {result.get('state')!r})")
    return result


def move_to_position(x: float, y: float, theta: float, target: Optional[str] = None) -> dict:
    """Drive the mobile base before interacting with a device's touchscreen.

    Backend-agnostic counterpart of stubs/playwright move_to_position (a no-op there — no physical
    base). The test runner calls this for CROSS-KIOSK hops, passing the destination kiosk_id as
    `target` plus the device's pose from the Device Map.

    The AGV controller drives to NAMED positions from its pre-built map, so when `target` is given
    (the destination kiosk_id or "home") we send it as /base/goto's `target` field — the same
    contract as navigate_to_kiosk. x/y/theta remain a FALLBACK for a controller that navigates by
    raw pose instead of by name (used only when no target name is available).

    Non-blocking POST → poll: the HTTP call itself respects settings.robot_response_timeout_s (fail
    fast if the base controller doesn't answer), while the physical move is awaited up to
    base_move_timeout_s. A timeout raises, which the runner catches and fails the step gracefully."""
    global _screen_localized
    _screen_localized = False   # base motion invalidates the screen pose → must re-capture before taps
    cmd_id = _new_cmd_id("base-goto")
    if target:
        print(f"  [ROBOT] move_to_position(target={target!r})")
        body = {"target": target, "cmd_id": cmd_id}
    else:
        print(f"  [ROBOT] move_to_position(x={x}, y={y}, θ={theta}°)")
        body = {"x": x, "y": y, "theta": theta, "cmd_id": cmd_id}
    goto = _post("/base/goto", body)
    return _poll_base(cmd_id, settings.base_move_timeout_s,
                      initial_state=str((goto or {}).get("state", "")))


def get_base_pose() -> dict:
    return _get("/base/pose")


def get_base_state() -> dict:
    """Current AGV base state (e.g. {'state':'idle'|'moving'|'error', ...}). GET, zero side-effects.
    Used by 'check_state' plan steps to assert the base reached/settled at a device."""
    return _get("/base/state")


def get_arm_state() -> dict:
    return _get("/arm/state")


# ── Calibration ────────────────────────────────────────────────────────────────

def calibrate(save_path: str = "./screenshots/calibration.png") -> dict:
    """
    Capture the screen and derive pixel-space scale factors.
    Call once before a test run to ensure coordinate accuracy.
    """
    result = capture_screen(save_path)
    w, h   = result.get("width"), result.get("height")
    print(
        f"  [CALIBRATE] Camera {w}×{h}  "
        f"viewport {settings.viewport_width}×{settings.viewport_height}  "
        f"scale=({_calibration.get('scale_x', '?'):.3f}, {_calibration.get('scale_y', '?'):.3f})"
    )
    return dict(_calibration)


# ── Card operations (physical card handling) ───────────────────────────────────

# Card pick/tap COMPLETE with the arm still gripping the card, so /arm/state settles to
# "holding_card" (not "idle"); replace hands it back and returns to "idle".
_CARD_HOLD_STATES = ("holding_card", "idle")


def card_pick(timeout_s: Optional[float] = None) -> dict:
    """Pick up a smart card from the card holder tray. Completes in state 'holding_card'."""
    _ensure_localized()   # /card/* require a screen capture since the last base motion (kiosk pose)
    t      = timeout_s or settings.card_op_timeout_s
    cmd_id = _new_cmd_id("card-pick")
    print("  [ROBOT] card_pick")
    resp = _post("/card/pick", {"cmd_id": cmd_id})
    return _poll("/arm/state", cmd_id, t, abort_ep="/arm/abort", terminal_states=_CARD_HOLD_STATES,
                 initial_state=resp.get("state", ""), label="card_pick", recover_on_fail=False)


def card_tap(reader_kiosk_id: str, timeout_s: Optional[float] = None) -> dict:
    """Present the held card to the kiosk's NFC reader. The reader kiosk is the one currently
    localized (derived from the last /capture), so the spec /card/tap body carries only cmd_id;
    reader_kiosk_id is kept in the signature for logging/backend parity. Stays 'holding_card'."""
    _ensure_localized()
    t      = timeout_s or settings.card_op_timeout_s
    cmd_id = _new_cmd_id(f"card-tap-{reader_kiosk_id}")
    print(f"  [ROBOT] card_tap → {reader_kiosk_id!r}")
    resp = _post("/card/tap", {"cmd_id": cmd_id})
    return _poll("/arm/state", cmd_id, t, abort_ep="/arm/abort", terminal_states=_CARD_HOLD_STATES,
                 initial_state=resp.get("state", ""), label=f"card_tap {reader_kiosk_id}",
                 recover_on_fail=False)


def card_replace(timeout_s: Optional[float] = None) -> dict:
    """Return the held card to its holder tray. Returns the arm to 'idle'."""
    _ensure_localized()
    t      = timeout_s or settings.card_op_timeout_s
    cmd_id = _new_cmd_id("card-replace")
    print("  [ROBOT] card_replace")
    resp = _post("/card/replace", {"cmd_id": cmd_id})
    return _poll("/arm/state", cmd_id, t, abort_ep="/arm/abort",
                 initial_state=resp.get("state", ""), label="card_replace", recover_on_fail=False)


# ── Telemetry for management frontend ─────────────────────────────────────────

def get_events(since_idx: int = 0) -> list[dict]:
    """Return telemetry events since index (for polling from management UI)."""
    return _events[since_idx:]


def get_status() -> dict:
    """Lightweight status summary for the management dashboard."""
    try:
        arm  = _get("/arm/state")
        base = _get("/base/pose")
        connected = True
    except Exception as e:
        arm  = {"state": "unknown", "error": str(e)}
        base = {}
        connected = False
    return {
        "robot_id":         settings.robot_id,
        "connected":        connected,
        "current_kiosk_id": _current_kiosk_id,
        "arm_state":        arm.get("state", "unknown"),
        "base_pose":        base,
        "event_count":      len(_events),
    }


def health_check(do_capture: bool = True) -> dict:
    """Pre-run readiness probe for the management UI.

    Checks the three things that must be healthy before a real-robot test run, each via the
    Robot REST API:
      • robot  — GET /arm/state   : the arm server is reachable and responsive (not error)
      • base   — GET /base/state  : the AGV base is idle/positioned at a kiosk
      • camera — POST /capture    : a rectified screen frame is returned (kiosk localized).
                 This also runs calibration, measuring the real camera resolution so the first
                 tap uses measured dimensions instead of the config fallback.

    Every probe is isolated in try/except so an unreachable robot yields structured errors
    rather than throwing.  Returns the robot URL and per-component {status, detail, …}.
    """
    arm_url = _arm_base_url()
    agv_url = _agv_base_url()

    def comp(status: str, detail: str, **extra) -> dict:
        return {"status": status, "detail": detail, **extra}

    components: dict = {}

    # 1 ─ Robot (arm) reachable & responsive
    try:
        arm = _get("/arm/state")
        st  = arm.get("state", "unknown")
        ok  = str(st).lower() in _ARM_HEALTHY_STATES
        components["robot"] = comp(
            "ok" if ok else "error",
            f"Arm responsive (state: {st})" if ok else f"Arm reports state '{st}'",
            arm_state=st, robot_id=arm.get("robot_id", settings.robot_id),
        )
    except Exception as e:
        components["robot"] = comp("error", f"Arm unreachable at {arm_url} — {e}")

    # 2 ─ AGV base positioned / idle
    try:
        base = _get("/base/state")
        bst  = base.get("state", "unknown")
        try:
            pose = _get("/base/pose")
        except Exception:
            pose = {}
        ok = bst == "idle"
        components["base"] = comp(
            "ok" if ok else "error",
            f"AGV base idle at kiosk '{_current_kiosk_id or settings.default_kiosk_id}'"
            if ok else f"AGV base is '{bst}', not ready",
            base_state=bst, base_pose=pose,
        )
    except Exception as e:
        components["base"] = comp("error", f"AGV base unreachable at {agv_url} — {e}")

    # 3 ─ Camera capture (also calibrates)
    if do_capture:
        try:
            cap  = capture_screen("./screenshots/health_capture.png")
            w, h = cap.get("width"), cap.get("height")
            if w and h:
                components["camera"] = comp(
                    "ok",
                    f"Captured screen {w}×{h}; kiosk localized & calibrated",
                    width=w, height=h,
                    scale_x=round(_calibration.get("scale_x", 0.0), 4),
                    scale_y=round(_calibration.get("scale_y", 0.0), 4),
                    calibrated=bool(_calibration),
                )
            else:
                components["camera"] = comp("error", "Capture returned no image dimensions")
        except Exception as e:
            components["camera"] = comp("error", f"Screen capture failed — {e}")
    else:
        components["camera"] = comp("unknown", "Not checked (capture skipped)")

    healthy = all(c.get("status") == "ok" for c in components.values())
    return {
        "backend":    settings.robot_backend,
        "arm_url":    arm_url,
        "agv_url":    agv_url,
        "robot_url":  arm_url,  # back-compat alias
        "robot_id":   settings.robot_id,
        "kiosk_id":   _current_kiosk_id or settings.default_kiosk_id,
        "simulated":  False,
        "healthy":    healthy,
        "components": components,
        "checked_at": time.time(),
    }
