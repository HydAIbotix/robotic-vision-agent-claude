#!/usr/bin/env python3
"""
Demo: run the vision agent against local kiosk screenshots.

Configure the mock robot to serve pre-recorded screenshots as if the robot
arm camera were capturing them after each action.  The agent itself is
identical to what runs in production — only the robot stub is in mock mode.

Usage:
    python run_demo.py                     # full login flow (default)
    python run_demo.py checkout            # add-to-cart → checkout flow
"""
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SCREENSHOTS = Path("C:/Users/gsk54/Desktop/Robotics_Project/Kiosk_Screenshots_Latest")

# ── Configure mock robot ───────────────────────────────────────────────────────
from vision_agent.robot import stubs as robot

FLOWS = {
    # Each entry = screenshot the robot camera "sees" after executing that step.
    # The agent plans the exact steps; we pre-load one frame per step.
    "login": {
        "task": "Log in with email tester@kiosk.local and password Password123",
        "start": SCREENSHOTS / "login_page.png",
        # after: tap email, type email, tap password, type password, tap Sign In
        "screens": [
            SCREENSHOTS / "login_page.png",       # focus on email field
            SCREENSHOTS / "login_page.png",       # email entered
            SCREENSHOTS / "login_page.png",       # focus on password field
            SCREENSHOTS / "login_page.png",       # password entered
            SCREENSHOTS / "products_page.png",    # ← Sign In navigates here
        ],
    },
    "checkout": {
        "task": "Add Nexora Phone X2 to cart and proceed to checkout",
        "start": SCREENSHOTS / "products_page.png",
        "screens": [
            SCREENSHOTS / "product_added_to_cart.png",  # after Add to Cart
            SCREENSHOTS / "cart_checkout_page.png",     # after Cart button
        ],
    },
}

flow_name = sys.argv[1] if len(sys.argv) > 1 else "login"
flow = FLOWS.get(flow_name)
if not flow:
    print(f"Unknown flow '{flow_name}'. Available: {', '.join(FLOWS)}")
    sys.exit(1)

robot.set_demo_screens([str(p) for p in flow["screens"]])

# ── Build and run the agent ────────────────────────────────────────────────────
from vision_agent.agent import create_agent
from vision_agent.state import VisionAgentState

agent = create_agent()

initial_state: VisionAgentState = {
    "task_description": flow["task"],
    "image_path": str(flow["start"]),
    "screen_analysis": None,
    "planned_steps": [],
    "current_step_idx": 0,
    "step_results": [],
    "retry_count": 0,
    "screen_history": [],
    "decision_tree": {},
    "outcome": "running",
    "summary": "",
    "error_message": None,
}

print("=" * 64)
print(f"  Robotic Vision Agent — {flow_name.title()} Flow Demo")
print("=" * 64)
print(f"  Task: {flow['task']}")
print()

result = agent.invoke(initial_state)

# ── Print results ──────────────────────────────────────────────────────────────
print()
print("=" * 64)
outcome_icon = "✓ PASSED" if result["outcome"] == "passed" else "✗ FAILED"
print(f"  {outcome_icon}")
print(f"  {result['summary']}")
print()

print("  Steps:")
for i, r in enumerate(result["step_results"], 1):
    icon = "✓" if r["success"] else "✗"
    print(f"    {i}. [{icon}] {r['step_instruction']}")
    if r.get("observation"):
        print(f"          {r['observation']}")
    if not r["success"] and r.get("error"):
        print(f"          ERROR: {r['error']}")

print()
print("  Screen journey:")
print("    " + " → ".join(result["screen_history"]))

print()
print("  Decision tree learned:")
for screen, transitions in result["decision_tree"].items():
    for action, next_screen in transitions.items():
        print(f"    [{screen}] {action!r} → {next_screen}")
print("=" * 64)
