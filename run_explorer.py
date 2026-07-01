#!/usr/bin/env python3
"""
Phase 1 — App Explorer: one-time app understanding.

Run this ONCE for a new application.  The agent opens the browser, navigates
every reachable screen autonomously, identifies all UI elements with Claude
vision, records element coordinates and navigation transitions, and writes
app_map.json.  Subsequent test runs load that map instead of re-exploring.

Usage:
    python run_explorer.py                      # explore using ROBOT_BACKEND from .env
    python run_explorer.py --output my_map.json

Logs:
    All console output is mirrored to explorer_run_YYYYMMDD_HHMMSS.log
    so every run is fully reproducible from the log alone.
"""
import sys
import io
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# ── Stdout → both console and a timestamped log file ──────────────────────────
# Must happen before any other import that might print.
_log_path = f"explorer_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_log_file = open(_log_path, "w", encoding="utf-8")

class _Tee:
    """Write to multiple streams simultaneously (console + log file)."""
    def __init__(self, *streams):
        self._streams = streams
    def write(self, s):
        for f in self._streams:
            f.write(s)
    def flush(self):
        for f in self._streams:
            f.flush()
    def isatty(self):
        return False
    @property
    def encoding(self):
        return "utf-8"
    @property
    def errors(self):
        return "replace"

_real_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_real_stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.stdout = _Tee(_real_stdout, _log_file)
sys.stderr = _Tee(_real_stderr, _log_file)

load_dotenv()

OUTPUT_PATH = "app_map.json"
for arg in sys.argv[1:]:
    if arg.startswith("--output"):
        OUTPUT_PATH = arg.split("=", 1)[-1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1]

# ── Credentials ────────────────────────────────────────────────────────────────
# "email" key = the value typed into the first text/username field on a login form.
# Adjust these to match the target kiosk app's actual login credentials.
CREDENTIALS = {
    "valid":   {"email": "tester@kiosk.local", "password": "Password123"},
    "invalid": {"email": "baduser@example.com", "password": "WrongPass"},
}

# ── Capture the entry screen from the live browser ────────────────────────────
from vision_agent.config import settings
from vision_agent import robot

screenshots_dir = Path(settings.screenshots_dir)
screenshots_dir.mkdir(parents=True, exist_ok=True)

_eff_mode = (
    "claude (forced: real robot)"
    if settings.robot_backend == "real" and settings.exploration_mode == "playwright_aria"
    else settings.exploration_mode
)

print("=" * 60)
print("  App Explorer — Generic Kiosk POS")
print(f"  Backend        : {settings.robot_backend}")
print(f"  Explore mode   : {_eff_mode}")
print(f"  App URL        : {settings.kiosk_url}")
print(f"  Log file       : {_log_path}")
print(f"  Credentials (valid): {CREDENTIALS['valid']}")
print("=" * 60)

initial_path = str(screenshots_dir / "explore_entry.png")
capture = robot.capture_screen(initial_path)
initial_image = capture["image_path"]
print(f"\n  Entry screenshot captured: {initial_image}")

# ── Build and run the explorer ─────────────────────────────────────────────────
from app_explorer.agent import create_explorer
from app_explorer.state import ExplorerState
from app_map.store import empty as empty_map

explorer = create_explorer()

# Enforce: real robot must always use Claude vision (ARIA requires Playwright DOM)
_exploration_mode = settings.exploration_mode
if settings.robot_backend == "real" and _exploration_mode == "playwright_aria":
    print("  [WARNING] exploration_mode=playwright_aria is incompatible with robot_backend=real.")
    print("  [WARNING] Forcing exploration_mode=claude.")
    _exploration_mode = "claude"

initial: ExplorerState = {
    "app_name":              "Generic Kiosk POS",
    "entry_image_path":      initial_image,
    "credentials":           CREDENTIALS,
    "app_map_path":          OUTPUT_PATH,
    "exploration_mode":      _exploration_mode,
    "app_map":               empty_map("Generic Kiosk POS", "signin"),
    "exploration_queue":     [],
    "explored_action_keys":  [],
    "current_image_path":    initial_image,
    "current_screen_id":     "",
    "last_executed_action":  None,
    "last_result_is_new":    False,
    "last_result_screen_id": "",
    "approach_paths":        {},
    "complete":              False,
}

result = explorer.invoke(initial)


# ── Commerce walkthrough — discover payment / success / order_history screens ───
# The main exploration may miss screens that require cart state to reach
# (e.g. "Proceed to Card Payment" does nothing on an empty cart).
# This walkthrough adds Nexora to the cart, completes the purchase, and
# explores each resulting screen so future test runs have accurate coordinates.

def _commerce_walkthrough(app_map: dict) -> dict:
    """Return updated app_map after navigating the full purchase flow."""
    import time as _time
    from app_explorer.nodes.explore_screen import explore_screen as _explore_node
    from app_map.store import empty as _empty_map

    def _snap(label: str) -> str:
        path = str(screenshots_dir / f"walkthrough_{label}_{int(_time.time())}.png")
        robot.capture_screen(path)
        _time.sleep(0.5)
        return path

    def _explore(image_path: str, hint_sid: str) -> dict:
        """Run explore_screen for one screenshot and return updated app_map."""
        nonlocal app_map
        state = {
            "app_name":              app_map.get("app_name", "Generic Kiosk POS"),
            "entry_image_path":      "",
            "credentials":           CREDENTIALS,
            "app_map_path":          OUTPUT_PATH,
            "exploration_mode":      _exploration_mode,
            "app_map":               app_map,
            "exploration_queue":     [],
            "explored_action_keys":  [],
            "current_image_path":    image_path,
            "current_screen_id":     "",
            "last_executed_action":  None,
            "last_result_is_new":    True,
            "last_result_screen_id": hint_sid,
            "approach_paths":        {},
            "complete":              False,
        }
        updates = _explore_node(state)
        return updates.get("app_map", app_map)

    def _set_transition(sid: str, eid: str, dest: str) -> None:
        scr = (app_map.setdefault("screens", {})).setdefault(sid, {})
        scr.setdefault("transitions", {})[eid] = dest

    def _find_element(sid: str, *keywords: str) -> dict | None:
        for el in ((app_map.get("screens") or {}).get(sid) or {}).get("elements") or []:
            if any(kw in el["id"].lower() for kw in keywords):
                return el
        return None

    def _tap(el: dict) -> None:
        cx, cy = el.get("center") or [0, 0]
        robot.tap(int(cx), int(cy))

    def _dom_screen() -> str:
        """Return current DOM screen ID or '' if unavailable."""
        try:
            return robot.get_dom_screen_id() or ""
        except Exception:
            return ""

    # ── Only run for backends that have a live browser ─────────────────────────
    if settings.robot_backend not in ("playwright", "real"):
        print("\n  [WALKTHROUGH] Skipped (demo backend has no live browser)")
        return app_map

    # ── Skip if payment + success already fully mapped ─────────────────────────
    screens = app_map.get("screens") or {}
    if screens.get("payment", {}).get("elements") and screens.get("success", {}).get("elements"):
        print("\n  [WALKTHROUGH] Skipped (payment + success screens already in app_map)")
        return app_map

    print("\n" + "=" * 60)
    print("  [WALKTHROUGH] Commerce flow — completing purchase to discover")
    print("  [WALKTHROUGH] payment, success, and order_history screens")
    print("=" * 60)

    # ── Login ──────────────────────────────────────────────────────────────────
    robot.reset_to_entry()
    _time.sleep(1.5)

    login_sid = app_map.get("entry_screen", "login")
    login_els = {e["id"]: e for e in (screens.get(login_sid) or {}).get("elements") or []}
    email_el = login_els.get("email_input") or {}
    pwd_el   = login_els.get("password_input") or {}
    btn_el   = login_els.get("sign_in_button") or {}

    if not (email_el.get("center") and btn_el.get("center")):
        print("  [WALKTHROUGH] Login elements not found — skipping")
        return app_map

    _tap(email_el); robot.type_text(CREDENTIALS["valid"]["email"], clear_first=True); _time.sleep(0.3)
    _tap(pwd_el);   robot.type_text(CREDENTIALS["valid"]["password"], clear_first=True); _time.sleep(0.3)
    _tap(btn_el);   _time.sleep(2.0)

    # ── Add product to cart ────────────────────────────────────────────────────
    prod_sc  = screens.get("products") or {}
    add_el   = next(
        (e for e in prod_sc.get("elements") or []
         if "add" in e["id"].lower() and "cart" in e["id"].lower()),
        None,
    )
    if not add_el:
        print("  [WALKTHROUGH] add-to-cart element not found — skipping")
        return app_map

    _tap(add_el); _time.sleep(1.5)   # wait for toast to appear + settle

    # ── Open cart ─────────────────────────────────────────────────────────────
    cart_btn = next(
        (e for e in prod_sc.get("elements") or []
         if "cart" in e["id"].lower() and "checkout" in e["id"].lower()),
        None,
    )
    if not cart_btn:
        print("  [WALKTHROUGH] cart_checkout_button not found — skipping")
        return app_map

    _tap(cart_btn); _time.sleep(2.5)
    try:
        app_map = _explore(_snap("cart_with_item"), "cart")
    except Exception as _e:
        print(f"  [WALKTHROUGH] cart explore error: {_e}")
    _set_transition("products", cart_btn["id"], "cart")

    # ── Proceed to payment ─────────────────────────────────────────────────────
    pay_btn = _find_element("cart", "proceed", "payment", "checkout", "pay")
    if not pay_btn:
        print("  [WALKTHROUGH] Proceed-to-payment button not found in cart — skipping")
        print(f"  [WALKTHROUGH] Cart elements: {[e['id'] for e in (app_map.get('screens',{}).get('cart',{}).get('elements') or [])]}")
        return app_map

    _tap(pay_btn); _time.sleep(1.0)  # first second — let JS start navigating

    # Verify navigation completed via DOM; retry once if still on cart
    _dom = _dom_screen()
    if _dom in ("cart", "products", ""):
        print(f"  [WALKTHROUGH] Payment navigation slow (DOM={_dom!r}) — waiting extra 2.5s")
        _time.sleep(2.5)
        _dom = _dom_screen()
    else:
        _time.sleep(1.5)   # navigation already happened, just let the screen settle

    print(f"  [WALKTHROUGH] After payment tap: DOM screen = {_dom!r}")

    try:
        app_map = _explore(_snap("payment"), "payment")
    except Exception as _e:
        print(f"  [WALKTHROUGH] payment explore error: {_e}")
    _set_transition("cart", pay_btn["id"], "payment")

    # ── Mock approval → success ────────────────────────────────────────────────
    mock_btn = _find_element("payment", "mock", "approval", "approve")
    if not mock_btn:
        print("  [WALKTHROUGH] Mock-approval button not found in payment — skipping")
        print(f"  [WALKTHROUGH] Payment elements: {[e['id'] for e in (app_map.get('screens',{}).get('payment',{}).get('elements') or [])]}")
        return app_map

    _tap(mock_btn); _time.sleep(2.5)
    try:
        app_map = _explore(_snap("success"), "success")
    except Exception as _e:
        print(f"  [WALKTHROUGH] success explore error: {_e}")
    _set_transition("payment", mock_btn["id"], "success")

    # ── View order history ─────────────────────────────────────────────────────
    hist_btn = _find_element("success", "history", "order", "view")
    if hist_btn:
        _tap(hist_btn); _time.sleep(2.5)
        try:
            app_map = _explore(_snap("order_history_with_order"), "order_history")
        except Exception as _e:
            print(f"  [WALKTHROUGH] order_history explore error: {_e}")
        _set_transition("success", hist_btn["id"], "order_history")

    n = len(app_map.get("screens") or {})
    print(f"\n  [WALKTHROUGH] Done — app_map now has {n} screens")
    return app_map


result["app_map"] = _commerce_walkthrough(result["app_map"])

# ── Write app_map.json ─────────────────────────────────────────────────────────
Path(OUTPUT_PATH).write_text(
    json.dumps(result["app_map"], indent=2, default=str),
    encoding="utf-8",
)
print(f"\n  App map written to: {OUTPUT_PATH}")
print(f"  Run log written to: {_log_path}")

# ── Final summary ──────────────────────────────────────────────────────────────
screens    = result["app_map"].get("screens") or {}
n_screens  = len(screens)
n_elements = sum(len(s.get("elements") or []) for s in screens.values())
transitions = {
    sid: list(sc.get("transitions", {}).keys())
    for sid, sc in screens.items()
}
print(f"\n  Summary: {n_screens} screens, {n_elements} total elements")
for sid, actions in transitions.items():
    print(f"    {sid}: {actions}")
