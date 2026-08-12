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
# App Explorer run logs live in a dedicated project-root folder (explorer_logs/) with a clear name,
# so they are not scattered at the repo root mixed with source. Anchored to THIS file's directory so
# it lands in the project root regardless of the caller's cwd (the API spawns run_explorer.py as a
# subprocess). The folder is gitignored via the existing "*.log" rule.
_log_dir = Path(__file__).resolve().parent / "explorer_logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_log_path = str(_log_dir / f"explorer_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
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

# Exploration-only: pre-fill the RPS mock-card field with the always-available demo card so the
# card-payment flow can be completed during the commerce walkthrough (see config.explore_demo_card).
# This is set ONLY here (the exploration entrypoint), so test execution never gets ?demoCard=1.
settings.explore_demo_card = True

# ── Group THIS exploration's screenshots into a dedicated, meaningfully-named folder ──────────
# Every raw capture (entry, per-screen explore_*, walkthrough_*, keyboard_map_*, aria_*, scroll_*)
# is routed under screenshots/exploration_<app_id>_<YYYYMMDD_HHMMSS>/ so each run's shots are grouped
# instead of scattered at the top level of screenshots/.  The folder is named for the app being
# explored (EXPLORE_APP_ID, e.g. "kiosk-2") + a timestamp.
#   • reference_screenshot paths are built from settings.screenshots_dir AT CAPTURE TIME, so they
#     stay self-consistent — real-robot Tier-1 template matching still resolves them from this folder.
#   • The ANNOTATED shots stay in the SHARED screenshots/annotated/ (anchored to app_map_path in
#     explore_screen.py), so the App Map page's annotated view is unaffected.
import os as _os_pre
_base_screens_root = Path(settings.screenshots_dir)
_explore_app_id = (_os_pre.environ.get("EXPLORE_APP_ID", "").strip() or "single")
_safe_app_id = "".join(c if (c.isalnum() or c in "-_") else "-" for c in _explore_app_id) or "single"
# Prune this app's PRIOR exploration folders (a full re-exploration re-captures all of the app's
# screens, so old shots become orphans — matches the previous overwrite-in-place behaviour).  Only
# this app's folders are touched; other apps' exploration folders are left intact.
import shutil as _shutil_pre
if _base_screens_root.exists():
    for _d in _base_screens_root.glob(f"exploration_{_safe_app_id}_*"):
        if _d.is_dir():
            _shutil_pre.rmtree(_d, ignore_errors=True)
_explore_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
_explore_shot_dir = _base_screens_root / f"exploration_{_safe_app_id}_{_explore_ts}"
_explore_shot_dir.mkdir(parents=True, exist_ok=True)
settings.screenshots_dir = str(_explore_shot_dir)

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
print(f"  Screenshots    : {settings.screenshots_dir}")
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
    "captured_values":       {},
    "complete":              False,
}

# ── Snapshot any EXISTING multi-app map BEFORE exploration runs ───────────────
# finalize_map/store.save() overwrites app_map.json mid-run (inside explorer.invoke), so if we
# read the "existing" map AFTER exploration it would already contain only THIS app's screens and
# every previously-explored kiosk would be lost.  Capture it now, while it still holds the other
# apps, and merge into this snapshot at the end.  (EXPLORE_APP_ID blank → legacy single-app.)
import os as _os
_app_id = _os.environ.get("EXPLORE_APP_ID", "").strip()
_existing_map = None
if _app_id and Path(OUTPUT_PATH).exists():
    try:
        _existing_map = json.loads(Path(OUTPUT_PATH).read_text(encoding="utf-8"))
        _prev_n = len((_existing_map.get("screens") or {}))
        print(f"  [APP MAP] Snapshotted existing map ({_prev_n} screens) — app '{_app_id}' will merge into it")
    except Exception:
        _existing_map = None

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
    from vision_agent.llm import get_llm, invoke_json
    from langchain_core.messages import HumanMessage

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

    def _record_observed_dependency(sid: str, element_id: str, requires: list,
                                     recipe: list, reason: str) -> None:
        """Write back a dependency that the walkthrough EMPIRICALLY exercised.

        Upserts into app_map["screens"][sid]["dependencies"]: if an entry for element_id
        already exists (e.g. inferred by SUGGEST_EXPLORABLE_ACTIONS during static analysis),
        it is UPGRADED to observed=True rather than duplicated.  observed=True means the recipe
        was actually executed and confirmed to work — the highest-confidence signal for the planner.
        """
        scr  = (app_map.setdefault("screens", {})).setdefault(sid, {})
        deps = scr.setdefault("dependencies", [])
        entry = next((d for d in deps if d.get("element_id") == element_id), None)
        if entry is None:
            entry = {"element_id": element_id}
            deps.append(entry)
        entry["requires"]           = list(dict.fromkeys((entry.get("requires") or []) + list(requires)))
        entry["prerequisite_steps"] = recipe
        entry["reason"]             = reason
        entry["observed"]           = True

    def _pick_sequence(goal: str, elements: list) -> list:
        """Let Opus choose which element(s) to tap (in order) to achieve `goal`.

        Replaces brittle id-keyword matching: Opus reads each element's id, TYPE, LABEL and
        description and returns the exact ids to tap — robust to any naming ("increase" vs
        "increment", "+" label, etc.).  Returns the matched element dicts in order, or [].
        """
        els = [e for e in (elements or []) if e.get("id") and e.get("center")]
        if not els:
            return []
        inv = "\n".join(
            f"- id={e['id']!r}  type={e.get('type','')!r}  label={(e.get('label') or '')!r}  desc={(e.get('description') or '')!r}"
            for e in els
        )
        prompt = (
            "You are operating a touchscreen app during automated exploration.\n"
            "These are the interactive elements currently on screen:\n"
            f"{inv}\n\n"
            f"GOAL: {goal}\n\n"
            "Decide the exact element id(s) to tap, IN ORDER, to achieve the goal using ONLY the ids "
            "above. If a quantity stepper (type 'stepper', label '+'/'increase') must be raised before "
            "an add/confirm/proceed button, list it first. Pick a single product/item if several exist.\n"
            'Return ONLY JSON: {"element_ids": ["<id>", ...]}  (empty list if nothing fits).'
        )
        data = invoke_json(get_llm(), [HumanMessage(content=prompt)],
                           default={"element_ids": []}, label="walkthrough_pick")
        by_id = {e["id"]: e for e in els}
        picked = [by_id[i] for i in (data.get("element_ids") or []) if i in by_id]
        print(f"  [WALKTHROUGH] Opus picked for goal ({goal[:40]}…): {[e['id'] for e in picked] or 'NONE'}")
        return picked

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

    # ── Local helpers ───────────────────────────────────────────────────────────
    def _el_in(sc: dict, *keywords: str) -> dict | None:
        """First element on screen dict `sc` whose id contains ALL keywords."""
        for e in sc.get("elements") or []:
            eid = e["id"].lower()
            if all(k in eid for k in keywords) and (e.get("center")):
                return e
        return None

    def _nav_tap(el: dict, prev_dom: str, label: str) -> str:
        """Tap `el`, then poll the DOM until the screen id changes (nav happened)."""
        _tap(el)
        dest = prev_dom
        for _ in range(10):          # up to ~5 s
            _time.sleep(0.5)
            d = _dom_screen()
            if d and d != prev_dom:
                dest = d
                break
        status = "changed" if dest != prev_dom else "NO CHANGE"
        print(f"  [WALKTHROUGH] {label}: DOM {prev_dom!r} -> {dest!r}  ({status})")
        return dest

    # ── Login (find the login screen by content, not by entry_screen key) ───────
    # The seeded entry_screen ("signin") often differs from the mapped screen id
    # ("sign_in"), so locate whichever screen actually carries the login elements.
    robot.reset_to_entry()
    _time.sleep(1.5)

    login_order = ([app_map["entry_screen"]] if app_map.get("entry_screen") else []) + list(screens.keys())
    login_sid, login_sc = "", {}
    for sid in login_order:
        sc = screens.get(sid) or {}
        if _el_in(sc, "email") and (_el_in(sc, "sign", "in") or _el_in(sc, "login") or _el_in(sc, "signin")):
            login_sid, login_sc = sid, sc
            break

    if not login_sc:
        print("  [WALKTHROUGH] No login screen (email + sign-in) found in app_map — skipping")
        return app_map

    email_el = _el_in(login_sc, "email")
    pwd_el   = _el_in(login_sc, "password") or _el_in(login_sc, "pass")
    btn_el   = _el_in(login_sc, "sign", "in") or _el_in(login_sc, "login") or _el_in(login_sc, "signin")
    print(f"  [WALKTHROUGH] Login screen: '{login_sid}'  (email={email_el['id']}, button={btn_el['id']})")

    _tap(email_el); robot.type_text(CREDENTIALS["valid"]["email"], clear_first=True); _time.sleep(0.3)
    if pwd_el:
        _tap(pwd_el); robot.type_text(CREDENTIALS["valid"]["password"], clear_first=True); _time.sleep(0.3)
    dom_after_login = _nav_tap(btn_el, login_sid, "login")

    # ── Locate the products screen (prefer DOM id, fall back to 'products') ──────
    products_sid = dom_after_login if screens.get(dom_after_login) else "products"
    prod_sc = screens.get(products_sid) or screens.get("products") or {}
    if not prod_sc.get("elements"):
        print(f"  [WALKTHROUGH] Products screen not in app_map (dom={dom_after_login!r}) — skipping")
        return app_map
    products_sid = next((s for s, v in screens.items() if v is prod_sc), products_sid)

    # ── Add a product to the cart — Opus picks the recipe (increment then add) ───
    # "Add to Cart" is a no-op while item quantity is 0.  Opus reads the element type/label
    # and returns the correct ordered ids (e.g. nexora_increase → nexora_add_to_cart),
    # robust to naming ("increase" vs "increment", "+" label, etc.).
    add_seq = _pick_sequence(
        "Add exactly ONE product to the cart so it becomes non-empty. If that product has a "
        "quantity stepper starting at 0, tap its increase/'+' control once first, THEN tap that "
        "same product's 'Add to Cart' button. Use one product only.",
        prod_sc.get("elements") or [],
    )
    if not add_seq:
        print("  [WALKTHROUGH] Opus could not identify an add-to-cart recipe — skipping")
        return app_map
    for el in add_seq:
        print(f"  [WALKTHROUGH] add-recipe: tap '{el['id']}'")
        _tap(el); _time.sleep(0.8)
    add_el = add_seq[-1]                                   # the gated add-to-cart button
    inc_el = add_seq[-2] if len(add_seq) >= 2 else None    # its quantity prerequisite (if any)

    # ── Open cart (DOM-verified) ────────────────────────────────────────────────
    cart_pick = _pick_sequence("Open the shopping cart / go to checkout.", prod_sc.get("elements") or [])
    cart_btn = cart_pick[0] if cart_pick else None
    if not cart_btn:
        print("  [WALKTHROUGH] cart/checkout button not identified — skipping")
        return app_map

    cart_sid = _nav_tap(cart_btn, products_sid, "open cart")
    if cart_sid == products_sid:
        print("  [WALKTHROUGH] Cart did not open (still on products — item likely not added). Skipping payment flow.")
        return app_map
    try:
        app_map = _explore(_snap("cart_with_item"), cart_sid)
    except Exception as _e:
        print(f"  [WALKTHROUGH] cart explore error: {_e}")
    _set_transition(products_sid, cart_btn["id"], cart_sid)

    # ── Close the loop: write back the dependency we just EXERCISED ──────────────
    if inc_el:
        _record_observed_dependency(
            products_sid, add_el["id"], [inc_el["id"]],
            [{"action_type": "tap", "element_id": inc_el["id"]}],
            f"Confirmed during exploration: tapping '{inc_el['id']}' then '{add_el['id']}' "
            f"produced a non-empty cart (the cart screen opened).",
        )
        print(f"  [WALKTHROUGH] Recorded OBSERVED dependency: '{add_el['id']}' requires '{inc_el['id']}'")

    # ── Proceed to payment (DOM-verified) — Opus picks the button ────────────────
    cart_els = (app_map.get("screens", {}).get(cart_sid, {}).get("elements")) or []
    pay_pick = _pick_sequence("Proceed from the cart to the payment / card-payment / checkout screen.", cart_els)
    pay_btn = pay_pick[0] if pay_pick else None
    if not pay_btn:
        print(f"  [WALKTHROUGH] Proceed-to-payment button not identified on '{cart_sid}' — skipping")
        print(f"  [WALKTHROUGH] '{cart_sid}' elements: {[e['id'] for e in cart_els]}")
        return app_map

    payment_sid = _nav_tap(pay_btn, cart_sid, "proceed to payment")
    try:
        app_map = _explore(_snap("payment"), payment_sid)
    except Exception as _e:
        print(f"  [WALKTHROUGH] payment explore error: {_e}")
    _set_transition(cart_sid, pay_btn["id"], payment_sid)

    # ── Complete the order — up to 5 hops (mock-card path is TWO STEPS on the SAME screen) ──
    # The RPS card payment is a two-step reveal: "Use Mock Card" reveals a card-number entry ON the
    # SAME screen (no navigation), then "Confirm Mock Card Payment" completes the order and navigates.
    # So we must NOT stop when a tap doesn't change the screen (that's the reveal); instead we re-explore
    # the payment screen each hop to pick up the newly-revealed controls, TYPE the demo card into the
    # mock-card input (the exploration URL also pre-fills it via ?demoCard=1 — this is a belt-and-braces
    # backup), and tap the next NEW button. A `tapped` set prevents re-tapping the same button, so a
    # genuinely dead button can't loop. Some apps also insert a 'Start card reader session' hop first.
    demo_card = settings.demo_card_number
    tapped_ids: set = set()
    for hop in range(5):
        # Re-map the payment screen so any controls revealed by the previous tap (e.g. the mock-card
        # entry field + confirm button) are available to choose from.
        try:
            app_map = _explore(_snap(f"payment_hop{hop}"), payment_sid)
        except Exception as _e:
            print(f"  [WALKTHROUGH] payment hop{hop} explore error: {_e}")
        pay_els = (app_map.get("screens", {}).get(payment_sid, {}).get("elements")) or []

        # If a mock-card number input is present, focus + type the demo card (idempotent with the
        # ?demoCard=1 pre-fill). Generic: any input whose id/label mentions card/mock/number.
        card_input = next(
            (e for e in pay_els
             if e.get("type") == "input" and e.get("center")
             and any(k in (e.get("id", "") + " " + (e.get("label") or "")).lower()
                     for k in ("card", "mock", "number"))),
            None,
        )
        if card_input:
            try:
                _tap(card_input)
                robot.type_text(demo_card, clear_first=True)
                _time.sleep(0.3)
                print(f"  [WALKTHROUGH] entered demo mock card into '{card_input['id']}'")
            except Exception as _e:
                print(f"  [WALKTHROUGH] demo-card entry skipped: {_e}")

        nxt = _pick_sequence(
            "Advance ONE step to COMPLETE the order/payment. If a 'Use Mock Card' option is present, "
            "tap it to reveal the mock-card entry; otherwise tap Confirm/Complete/Pay to finish the "
            "mock-card payment, or start the card reader session and approve. Do NOT pick a "
            "Back/Cancel/edit button. Return the single next button to tap on THIS screen (empty if the "
            "order already looks complete).",
            pay_els,
        )
        step_btn = next((e for e in nxt if e["id"] not in tapped_ids), None)
        if not step_btn:
            print(f"  [WALKTHROUGH] No further NEW payment action on '{payment_sid}' (hop {hop}) — stopping")
            break
        tapped_ids.add(step_btn["id"])
        next_sid = _nav_tap(step_btn, payment_sid, f"payment hop {hop}: {step_btn['id']}")
        _set_transition(payment_sid, step_btn["id"], next_sid)
        if next_sid == payment_sid:
            # Same screen — a reveal (e.g. Use Mock Card) or an in-place update; keep going (the next
            # hop re-explores and picks the newly-revealed confirm button). tapped_ids stops a loop.
            print(f"  [WALKTHROUGH] '{step_btn['id']}' stayed on '{payment_sid}' (likely revealed the "
                  f"mock-card entry) — continuing")
            continue
        label = "success" if any(k in next_sid.lower() for k in ("success", "result", "order")) else f"payment_next{hop}"
        try:
            app_map = _explore(_snap(label), next_sid)
        except Exception as _e:
            print(f"  [WALKTHROUGH] {label} explore error: {_e}")
        payment_sid = next_sid
        # Heuristic stop: if the screen id suggests an order result, we're done.
        if any(k in next_sid.lower() for k in ("success", "result", "confirm", "complete", "receipt", "order")):
            print(f"  [WALKTHROUGH] Reached order-result screen '{next_sid}' — purchase flow mapped")
            break

    n = len(app_map.get("screens") or {})
    print(f"\n  [WALKTHROUGH] Done — app_map now has {n} screens")
    return app_map


result["app_map"] = _commerce_walkthrough(result["app_map"])

# ── Correct the entry screen ───────────────────────────────────────────────────
# The seed entry_screen ("signin") is a placeholder for login-first apps. When the app's real
# entry screen is something else (e.g. a card station with no login), the placeholder is wrong
# and confusing. Generically: if the seeded entry isn't an actual explored screen, use the FIRST
# screen explored (dict insertion order = exploration order) — the true entry — for any app.
_explored_screens = result["app_map"].get("screens") or {}
if _explored_screens and result["app_map"].get("entry_screen") not in _explored_screens:
    result["app_map"]["entry_screen"] = next(iter(_explored_screens))
    print(f"  [APP MAP] entry_screen corrected to first explored screen: '{result['app_map']['entry_screen']}'")

# ── Write app_map.json ─────────────────────────────────────────────────────────
# Multi-app: when EXPLORE_APP_ID is set, MERGE this app's screens (tagged by app id)
# into the PRE-EXPLORATION snapshot instead of overwriting — so exploring a second kiosk
# app does not wipe the first.  We merge into `_existing_map` (captured BEFORE the graph
# ran) rather than re-reading the file here, because finalize_map/store.save already
# clobbered app_map.json with only THIS app's screens mid-run.  Blank app id → legacy
# single-app overwrite (unchanged).
from app_map import store as _store
result["app_map"] = _store.merge_explored_app(
    _existing_map, result["app_map"], _app_id, app_label=_app_id, app_url=settings.kiosk_url,
)
Path(OUTPUT_PATH).write_text(
    json.dumps(result["app_map"], indent=2, default=str),
    encoding="utf-8",
)
print(f"\n  App map written to: {OUTPUT_PATH}" + (f"  (merged as app '{_app_id}')" if _app_id else ""))
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
