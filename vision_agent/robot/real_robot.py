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
  Poll GET state until state=="idle" and cmd_id matches
  Timeout → POST abort endpoint → raise TimeoutError
"""
import base64
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from vision_agent.config import settings

# ── Module-level state ─────────────────────────────────────────────────────────
_keyboard_map: dict = {}
_calibration:  dict = {}          # {"scale_x": float, "scale_y": float}
_current_kiosk_id: str = ""

# Telemetry ring-buffer — recent command events for management frontend polling
_events: list[dict] = []
_MAX_EVENTS = 500


# ── Internal helpers ───────────────────────────────────────────────────────────

def _base_url() -> str:
    return f"http://{settings.robot_ip}:{settings.robot_port}/api/v1"


def _new_cmd_id() -> str:
    return f"cmd-{uuid.uuid4().hex[:12]}"


def _record(event_type: str, endpoint: str, cmd_id: str,
            t0: float, t1: float, status: int, extra: dict) -> None:
    _events.append({
        "event_type":  event_type,
        "endpoint":    endpoint,
        "cmd_id":      cmd_id,
        "robot_id":    settings.robot_id,
        "request_at":  t0,
        "response_at": t1,
        "latency_ms":  round((t1 - t0) * 1000, 1),
        "http_status": status,
        **extra,
    })
    if len(_events) > _MAX_EVENTS:
        _events.pop(0)


def _post(endpoint: str, body: dict, timeout: float = 10.0) -> dict:
    url = f"{_base_url()}/{endpoint.lstrip('/')}"
    t0  = time.time()
    resp = requests.post(url, json=body, timeout=timeout)
    t1  = time.time()
    _record("POST", endpoint, body.get("cmd_id", ""), t0, t1, resp.status_code, {})
    resp.raise_for_status()
    return resp.json()


def _get(endpoint: str, timeout: float = 5.0) -> dict:
    url = f"{_base_url()}/{endpoint.lstrip('/')}"
    t0  = time.time()
    resp = requests.get(url, timeout=timeout)
    t1  = time.time()
    _record("GET", endpoint, "", t0, t1, resp.status_code, {})
    resp.raise_for_status()
    return resp.json()


def _poll(
    state_ep: str,
    cmd_id:   str,
    timeout_s: float,
    abort_ep: Optional[str] = None,
) -> dict:
    """Poll state_ep until cmd_id matches and state=="idle", or timeout."""
    deadline = time.time() + timeout_s
    last_state: dict = {}
    while time.time() < deadline:
        last_state = _get(state_ep)
        s = last_state.get("state", "")
        if s == "error":
            raise RuntimeError(f"Robot error on {state_ep}: {last_state}")
        if s == "idle" and last_state.get("cmd_id") == cmd_id:
            return last_state
        time.sleep(settings.robot_poll_interval_s)

    # Timed out — attempt graceful abort
    if abort_ep:
        try:
            _post(abort_ep, {"cmd_id": _new_cmd_id()}, timeout=5.0)
        except Exception:
            pass
    raise TimeoutError(
        f"Robot {state_ep} timed out after {timeout_s}s "
        f"(last state: {last_state.get('state')!r})"
    )


def _scale(x: int, y: int) -> tuple[int, int]:
    """Scale viewport pixel (x,y) → robot camera (u,v)."""
    sx = _calibration.get("scale_x") or (settings.robot_camera_width  / settings.viewport_width)
    sy = _calibration.get("scale_y") or (settings.robot_camera_height / settings.viewport_height)
    return int(round(x * sx)), int(round(y * sy))


def _kiosk() -> str:
    return _current_kiosk_id or settings.default_kiosk_id


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
    t0   = time.time()
    resp = requests.post(
        f"{_base_url()}/capture",
        json={"type": "screen"},
        timeout=20.0,
    )
    t1 = time.time()
    _record("POST", "/capture", "", t0, t1, resp.status_code, {})
    resp.raise_for_status()
    data = resp.json()

    img_bytes = base64.b64decode(data["image_b64"])
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    Path(save_path).write_bytes(img_bytes)

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


def tap(x: int, y: int) -> dict:
    """Physically tap kiosk touchscreen at viewport pixel (x, y).

    The /screen/click completion returns the post-tap camera frame (image_b64). We
    decode and save it, then surface it as image_path so callers can reuse it for
    verification without a separate /capture round-trip (saves an arm cycle).
    """
    u, v   = _scale(x, y)
    cmd_id = _new_cmd_id()
    print(f"    [ROBOT] tap viewport({x},{y}) → camera({u},{v})")
    post_resp = _post("/screen/click", {
        "kiosk_id":         _kiosk(),
        "points":           [{"u": u, "v": v}],
        "delay_between_ms": 0,
        "cmd_id":           cmd_id,
    })
    state = _poll("/arm/state", cmd_id, settings.arm_move_timeout_s, abort_ep="/arm/abort")
    # After the arm confirms tap complete, the kiosk still needs time to process the touch
    # event and complete any navigation (e.g. login API call + React re-render takes 300-700ms).
    # 0.2s was too short and caused the next camera capture to land mid-transition.
    time.sleep(0.8)

    result = {"success": True, "x": x, "y": y, "u": u, "v": v}
    # The camera frame may arrive in the click ack or in the completion state — check both.
    click_result = state.get("click_result") or post_resp.get("click_result") or {}
    b64 = click_result.get("image_b64")
    if b64:
        try:
            fmt = (click_result.get("format") or "jpeg").lower()
            ext = "jpg" if fmt in ("jpg", "jpeg") else fmt
            save_path = str(Path(settings.screenshots_dir) / f"click_{cmd_id}.{ext}")
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            Path(save_path).write_bytes(base64.b64decode(b64))
            result["image_path"] = save_path
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

    Captures the kiosk screen via the robot arm camera, then compares the image against
    the reference_screenshot stored per-screen in app_map (written by the App Explorer).
    No LLM call — pure image similarity.  Requires PIL + numpy (standard in the project).

    Returns {"actual_screen": str, "match": bool, "method": "camera_reference", "confidence": float}.
    The "actual_screen" is the app_map key whose reference screenshot is most similar to the
    current camera frame.  Empty string means no screen reached the 0.70 similarity threshold.
    """
    if not save_path:
        save_path = f"./screenshots/verify_{int(time.time() * 1000)}.png"

    result       = capture_screen(save_path)
    current_path = result["image_path"]

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
        return {
            "actual_screen": "",
            "match":         False,
            "method":        "camera_reference",
            "confidence":    round(max(best_score, 0.0), 3),
            "screenshot":    current_path,
        }

    return {
        "actual_screen": best_screen,
        "match":         (best_screen == expected_screen_id),
        "method":        "camera_reference",
        "confidence":    round(best_score, 3),
        "screenshot":    current_path,
    }


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

    if not points:
        return {"success": True, "text": text, "tapped_keys": 0}

    cmd_id = _new_cmd_id()
    print(f"    [ROBOT] type({text!r}) — {len(points)} key taps")
    _post("/screen/click", {
        "kiosk_id":         _kiosk(),
        "points":           points,
        "delay_between_ms": 80,   # 80 ms between each key
        "cmd_id":           cmd_id,
    })
    # type_text may take longer than a single tap — allow extra time
    _poll("/arm/state", cmd_id,
          settings.arm_move_timeout_s + len(points) * 0.1,
          abort_ep="/arm/abort")
    return {"success": True, "text": text, "tapped_keys": len(points)}


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
    """Swipe from (x1,y1) to (x2,y2) via two sequential taps with delay."""
    u1, v1 = _scale(x1, y1)
    u2, v2 = _scale(x2, y2)
    cmd_id = _new_cmd_id()
    print(f"    [ROBOT] swipe ({x1},{y1})→({x2},{y2})  [{duration_ms}ms]")
    _post("/screen/click", {
        "kiosk_id":         _kiosk(),
        "points":           [{"u": u1, "v": v1}, {"u": u2, "v": v2}],
        "delay_between_ms": duration_ms,
        "cmd_id":           cmd_id,
    })
    _poll("/arm/state", cmd_id, settings.arm_move_timeout_s, abort_ep="/arm/abort")
    return {"success": True}


# ── Setup & navigation (called once per test suite) ────────────────────────────

def setup(kiosk_definitions: list[dict], arm_poses: dict, nav_map: dict) -> dict:
    """
    Upload kiosk definitions, arm rest/home poses, and navigation map.
    Call once per robot session before any navigate_to_kiosk().
    """
    cmd_id = _new_cmd_id()
    print(f"  [ROBOT] setup — {len(kiosk_definitions)} kiosk(s)")
    return _post("/setup", {
        "kiosks":    kiosk_definitions,
        "arm_poses": arm_poses,
        "nav_map":   nav_map,
        "cmd_id":    cmd_id,
    })


def navigate_to_kiosk(kiosk_id: str, timeout_s: Optional[float] = None) -> dict:
    """Drive the mobile base to kiosk_id; block until the robot arrives."""
    global _current_kiosk_id
    t = timeout_s or settings.base_move_timeout_s
    cmd_id = _new_cmd_id()
    print(f"  [ROBOT] navigate_to_kiosk({kiosk_id!r})  timeout={t}s")
    _post("/base/goto", {"kiosk_id": kiosk_id, "cmd_id": cmd_id})
    result = _poll("/base/state", cmd_id, t, abort_ep="/base/abort")
    _current_kiosk_id = kiosk_id
    print(f"  [ROBOT] arrived at {kiosk_id!r}")
    return result


def get_base_pose() -> dict:
    return _get("/base/pose")


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

def card_pick(timeout_s: Optional[float] = None) -> dict:
    """Pick up a smart card from the card holder tray."""
    t      = timeout_s or settings.card_op_timeout_s
    cmd_id = _new_cmd_id()
    print("  [ROBOT] card_pick")
    _post("/card/pick", {"cmd_id": cmd_id})
    return _poll("/arm/state", cmd_id, t, abort_ep="/arm/abort")


def card_tap(reader_kiosk_id: str, timeout_s: Optional[float] = None) -> dict:
    """Present held card to the NFC reader on reader_kiosk_id."""
    t      = timeout_s or settings.card_op_timeout_s
    cmd_id = _new_cmd_id()
    print(f"  [ROBOT] card_tap → {reader_kiosk_id!r}")
    _post("/card/tap", {"kiosk_id": reader_kiosk_id, "cmd_id": cmd_id})
    return _poll("/arm/state", cmd_id, t, abort_ep="/arm/abort")


def card_replace(timeout_s: Optional[float] = None) -> dict:
    """Return held card to the card holder tray."""
    t      = timeout_s or settings.card_op_timeout_s
    cmd_id = _new_cmd_id()
    print("  [ROBOT] card_replace")
    _post("/card/replace", {"cmd_id": cmd_id})
    return _poll("/arm/state", cmd_id, t, abort_ep="/arm/abort")


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
      • kiosk  — GET /base/state  : the mobile base is idle/positioned at a kiosk
      • camera — POST /capture    : a rectified screen frame is returned (kiosk localized).
                 This also runs calibration, measuring the real camera resolution so the first
                 tap uses measured dimensions instead of the config fallback.

    Every probe is isolated in try/except so an unreachable robot yields structured errors
    rather than throwing.  Returns the robot URL and per-component {status, detail, …}.
    """
    url = _base_url()

    def comp(status: str, detail: str, **extra) -> dict:
        return {"status": status, "detail": detail, **extra}

    components: dict = {}

    # 1 ─ Robot (arm) reachable & responsive
    try:
        arm = _get("/arm/state")
        st  = arm.get("state", "unknown")
        ok  = st in ("idle", "holding_card")
        components["robot"] = comp(
            "ok" if ok else "error",
            f"Arm responsive (state: {st})" if ok else f"Arm reports state '{st}'",
            arm_state=st, robot_id=arm.get("robot_id", settings.robot_id),
        )
    except Exception as e:
        components["robot"] = comp("error", f"Robot unreachable at {url} — {e}")

    # 2 ─ Kiosk: base positioned / idle
    try:
        base = _get("/base/state")
        bst  = base.get("state", "unknown")
        try:
            pose = _get("/base/pose")
        except Exception:
            pose = {}
        ok = bst == "idle"
        components["kiosk"] = comp(
            "ok" if ok else "error",
            f"Base idle at kiosk '{_current_kiosk_id or settings.default_kiosk_id}'"
            if ok else f"Base is '{bst}', not ready",
            base_state=bst, base_pose=pose,
        )
    except Exception as e:
        components["kiosk"] = comp("error", f"Base state unavailable — {e}")

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
        "robot_url":  url,
        "robot_id":   settings.robot_id,
        "kiosk_id":   _current_kiosk_id or settings.default_kiosk_id,
        "simulated":  False,
        "healthy":    healthy,
        "components": components,
        "checked_at": time.time(),
    }
