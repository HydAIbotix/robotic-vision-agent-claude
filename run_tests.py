#!/usr/bin/env python3
"""
Phase 2 — Test Runner.

Backends (set ROBOT_BACKEND in .env):
  demo        Run against pre-captured screenshots — no live app needed
  playwright  Run against the kiosk at KIOSK_URL (default http://localhost:5173)
  real        Run against the physical robot arm

Usage:
    python run_tests.py                          # all test cases
    python run_tests.py --tc TC-KIOSK-001        # single test case
"""
import sys
import io
from pathlib import Path
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

load_dotenv()

SCREENSHOTS = Path("C:/Users/gsk54/Desktop/Robotics_Project/Kiosk_Screenshots_Latest")
EXCEL_PATH  = "C:/Users/gsk54/Desktop/Robotics_Project/Test Cases/Kiosk_Test_Cases_Vision_Agent.xlsx"
FILTER_TC   = None

for i, arg in enumerate(sys.argv[1:], 1):
    if arg.startswith("--tc"):
        FILTER_TC = arg.split("=", 1)[-1] if "=" in arg else sys.argv[i + 1]

# ── Demo screens: screen_id → screenshot path ──────────────────────────────────
# Used only in demo mode.  In playwright mode these are ignored — the live browser
# provides real screenshots after every action.
DEMO_SCREENS = {
    "login":                 str(SCREENSHOTS / "login_page.png"),
    "products":              str(SCREENSHOTS / "products_page.png"),
    "product_added_to_cart": str(SCREENSHOTS / "product_added_to_cart.png"),
    "cart":                  str(SCREENSHOTS / "cart_checkout_page.png"),
    "payment":               str(SCREENSHOTS / "tap_card_payment_page.png"),
    "success":               str(SCREENSHOTS / "payment_successful_page.png"),
    "order_history":         str(SCREENSHOTS / "order_history_page.png"),
}

CREDENTIALS = {
    "valid":   {"email": "tester@kiosk.local", "password": "Password123"},
    "invalid": {"email": "baduser", "password": "wrongpass"},
}

# ── Load test cases ────────────────────────────────────────────────────────────
from test_runner.reader.excel_reader import read_test_cases

all_cases  = read_test_cases(EXCEL_PATH)
test_cases = (
    [tc for tc in all_cases if tc["test_id"] == FILTER_TC]
    if FILTER_TC else all_cases
)
if not test_cases:
    print(f"No test cases found (filter={FILTER_TC})")
    sys.exit(1)

# ── Load app map + keyboard map ────────────────────────────────────────────────
from vision_agent.config import settings
from vision_agent import robot
from app_map import store as app_map_store
from pathlib import Path as _Path

_app_map = None
if _Path(settings.app_map_path).exists():
    _app_map = app_map_store.load(settings.app_map_path)
    if "keyboard_map" in _app_map:
        robot.set_keyboard_map(_app_map["keyboard_map"])
        print(f"  Keyboard map loaded ({len(_app_map['keyboard_map'].get('keys', _app_map['keyboard_map']))} keys)")
    else:
        print("  WARNING: app_map.json has no keyboard_map — type_text will use keyboard events fallback")
else:
    print("  No app_map.json found — run run_explorer.py first for tap-based typing")

# ── Run ────────────────────────────────────────────────────────────────────────
from test_runner.agent import create_test_runner
from test_runner.state import TestRunnerState

print("=" * 60)
print(f"  Test Runner — {len(test_cases)} test case(s)  [{settings.robot_backend.upper()} mode]")
if settings.robot_backend == "playwright":
    print(f"  Kiosk URL: {settings.kiosk_url}")
print("=" * 60)

runner = create_test_runner()
initial: TestRunnerState = {
    "test_cases":          test_cases,
    "app_map":             None,        # not required — agent navigates dynamically
    "credentials":         CREDENTIALS,
    "demo_screens":        DEMO_SCREENS,
    "current_tc_idx":      0,
    "current_tc":          None,
    "planned_steps":       [],
    "credential_scenario": "",
    "start_image":         str(SCREENSHOTS / "login_page.png"),
    "test_results":        [],
    "summary":             "",
}
runner.invoke(initial)
