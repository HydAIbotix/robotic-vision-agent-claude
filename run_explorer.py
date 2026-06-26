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

print("=" * 60)
print("  App Explorer — Generic Kiosk POS")
print(f"  Backend  : {settings.robot_backend}")
print(f"  App URL  : {settings.kiosk_url}")
print(f"  Log file : {_log_path}")
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

initial: ExplorerState = {
    "app_name":              "Generic Kiosk POS",
    "entry_image_path":      initial_image,
    "credentials":           CREDENTIALS,
    "app_map_path":          OUTPUT_PATH,
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
