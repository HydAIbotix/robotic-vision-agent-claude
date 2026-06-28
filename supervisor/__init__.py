"""
Supervisor — parallel multi-robot test orchestrator.

Usage:
    from supervisor import run_parallel
    results = run_parallel([
        {"robot_id": "R-01", "kiosk_id": "K-01", "excel_path": "tests.xlsx", "filter_tc": None},
        {"robot_id": "R-02", "kiosk_id": "K-02", "excel_path": "tests.xlsx", "filter_tc": "TC-K2"},
    ])
"""
from supervisor.graph import run_parallel, SupervisorResult  # noqa: F401
