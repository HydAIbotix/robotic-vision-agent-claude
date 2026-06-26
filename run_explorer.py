#!/usr/bin/env python3
"""
Phase 1 — App Explorer: one-time app understanding.

Run this ONCE for a new application.  The agent navigates every reachable
screen, builds a navigation tree, and saves app_map.json.  Subsequent test
runs load the map instead of re-exploring.

Usage:
    python run_explorer.py                   # explore the kiosk app (demo mode)
    python run_explorer.py --output my_map.json
"""
import sys
import io
import json
from pathlib import Path
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

load_dotenv()

SCREENSHOTS = Path("C:/Users/gsk54/Desktop/Robotics_Project/Kiosk_Screenshots_Latest")
OUTPUT_PATH = "app_map.json"
for arg in sys.argv[1:]:
    if arg.startswith("--output"):
        OUTPUT_PATH = arg.split("=", 1)[-1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1]

# ── Demo navigation map ────────────────────────────────────────────────────────
# Maps "{screen_id}::{action_key}" to the screenshot that results from that action.
# Also includes "__default__" entry to reset a screen to its base state.
# Replace with real robot.capture_screen() results when hardware is available.

S = str(SCREENSHOTS) + "/"
DEMO_NAVIGATION = {
    # Login screen
    "login::__default__":                  S + "login_page.png",
    "login::tap_sign_in_valid":            S + "products_page.png",
    "login::tap_sign_in_valid_creds":      S + "products_page.png",
    "login::tap_sign_in_invalid":          S + "login_page.png",
    "login::tap_sign_in_invalid_creds":    S + "login_page.png",
    "login::tap_sign_up_link":             S + "login_page.png",   # not in demo screenshots
    "login::tap_forgot_password_link":     S + "login_page.png",
    "login::tap_developer_settings_link":  S + "login_page.png",

    # Products screen
    "products::__default__":               S + "products_page.png",
    "products::tap_order_history_button":  S + "order_history_page.png",
    "products::tap_nexora_add_to_cart":    S + "product_added_to_cart.png",
    "products::tap_orionbook_add_to_cart": S + "product_added_to_cart.png",
    "products::tap_vistaview_add_to_cart": S + "product_added_to_cart.png",
    "products::tap_audiopulse_add_to_cart":S + "product_added_to_cart.png",
    "products::tap_tabnova_add_to_cart":   S + "product_added_to_cart.png",
    "products::tap_fitpulse_add_to_cart":  S + "product_added_to_cart.png",

    # Product-added-to-cart screen
    "product_added_to_cart::__default__":          S + "product_added_to_cart.png",
    "product_added_to_cart::tap_cart_checkout":    S + "cart_checkout_page.png",
    "product_added_to_cart::tap_cart_checkout_button": S + "cart_checkout_page.png",

    # Cart screen
    "cart::__default__":                           S + "cart_checkout_page.png",
    "cart::tap_proceed_to_card_payment_button":    S + "tap_card_payment_page.png",
    "cart::tap_proceed_to_payment":                S + "tap_card_payment_page.png",
    "cart::tap_home_button":                       S + "products_page.png",
    "cart::tap_continue_shopping_button":          S + "products_page.png",

    # Payment screen
    "payment::__default__":                        S + "tap_card_payment_page.png",
    "payment::tap_use_mock_approval_button":       S + "payment_successful_page.png",
    "payment::tap_mock_approval":                  S + "payment_successful_page.png",
    "payment::tap_back_button":                    S + "cart_checkout_page.png",

    # Success screen
    "success::__default__":                        S + "payment_successful_page.png",
    "success::tap_view_order_history_button":      S + "order_history_page.png",
    "success::tap_go_to_home_page_button":         S + "products_page.png",

    # Order history screen
    "order_history::__default__":                  S + "order_history_page.png",
    "order_history::tap_go_to_home_page_button":   S + "products_page.png",
    "order_history::tap_home":                     S + "products_page.png",
}

# ── Credentials ────────────────────────────────────────────────────────────────
CREDENTIALS = {
    "valid":   {"email": "tester@kiosk.local", "password": "Password123"},
    "invalid": {"email": "baduser", "password": "wrongpass"},
}

# ── Build and run the explorer ─────────────────────────────────────────────────
from app_explorer.agent import create_explorer
from app_explorer.state import ExplorerState
from app_map.store import empty as empty_map

explorer = create_explorer()

initial: ExplorerState = {
    "app_name":             "Generic Kiosk POS",
    "entry_image_path":     S + "login_page.png",
    "credentials":          CREDENTIALS,
    "demo_navigation":      DEMO_NAVIGATION,
    "app_map_path":         OUTPUT_PATH,
    "app_map":              empty_map("Generic Kiosk POS", "login"),
    "exploration_queue":    [],
    "explored_action_keys": [],
    "current_image_path":   S + "login_page.png",
    "current_screen_id":    "",
    "last_executed_action": None,
    "last_result_is_new":   False,
    "last_result_screen_id": "",
    "complete":             False,
}

print("=" * 60)
print("  App Explorer — Generic Kiosk POS")
print("=" * 60)

result = explorer.invoke(initial)

print("\n  Final App Map:")
print(json.dumps(result["app_map"], indent=2, default=str))
