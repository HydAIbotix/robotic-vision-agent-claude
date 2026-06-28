#!/usr/bin/env python3
"""
Parallel test runner — run test suites across multiple robots simultaneously.

Usage:
    python run_parallel.py                      # use config below
    python run_parallel.py --config my_run.json # load assignment config from file

Each assignment in the JSON config:
    {
        "robot_id":    "R-01",
        "kiosk_id":    "K-01",
        "excel_path":  "path/to/tests.xlsx",
        "filter_tc":   null,                    // or "TC-K1" to run only matching IDs
        "credentials": {
            "valid":   {"email": "...", "password": "..."},
            "invalid": {"email": "...", "password": "..."}
        }
    }
"""
import json
import sys
import io
from pathlib import Path
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

load_dotenv()

# ── Default assignments (edit here for quick runs) ─────────────────────────────
EXCEL_PATH = "C:/Users/gsk54/Desktop/Robotics_Project/Test Cases/Kiosk_Test_Cases_Vision_Agent.xlsx"

DEFAULT_CREDENTIALS = {
    "valid":   {"email": "tester@kiosk.local", "password": "Password123"},
    "invalid": {"email": "baduser", "password": "wrongpass"},
}

DEFAULT_ASSIGNMENTS = [
    {
        "robot_id":    "R-01",
        "kiosk_id":    "K-01",
        "excel_path":  EXCEL_PATH,
        "filter_tc":   "TC-K1",        # Kiosk-1 tests
        "credentials": DEFAULT_CREDENTIALS,
    },
    {
        "robot_id":    "R-02",
        "kiosk_id":    "K-02",
        "excel_path":  EXCEL_PATH,
        "filter_tc":   "TC-K2",        # Kiosk-2 tests
        "credentials": DEFAULT_CREDENTIALS,
    },
]
# ──────────────────────────────────────────────────────────────────────────────

# Load custom config if passed
assignments = DEFAULT_ASSIGNMENTS
for i, arg in enumerate(sys.argv[1:], 1):
    if arg.startswith("--config"):
        config_path = arg.split("=", 1)[-1] if "=" in arg else sys.argv[i + 1]
        with open(config_path) as f:
            assignments = json.load(f)
        break

from supervisor import run_parallel

summary = run_parallel(assignments)

# Write combined results JSON
import datetime
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
out_path = Path("results") / f"parallel_{ts}.json"
out_path.parent.mkdir(exist_ok=True)
out_path.write_text(json.dumps({
    "timestamp":    datetime.datetime.now().isoformat(),
    "wall_time_s":  summary.wall_time_s,
    "total":        summary.total_tests,
    "passed":       summary.total_passed,
    "failed":       summary.total_failed,
    "workers": [
        {
            "robot_id":  w.robot_id,
            "kiosk_id":  w.kiosk_id,
            "run_id":    w.run_id,
            "status":    w.status,
            "total":     w.total,
            "passed":    w.passed,
            "failed":    w.failed,
            "error":     w.error,
            "results":   w.results,
        }
        for w in summary.workers
    ],
}, indent=2, ensure_ascii=False))

print(f"\n  Results saved to {out_path}")
sys.exit(0 if summary.total_failed == 0 else 1)
