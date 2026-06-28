"""Shared state types for the supervisor orchestrator."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WorkerAssignment:
    robot_id:   str
    kiosk_id:   str
    excel_path: str
    filter_tc:  Optional[str] = None
    credentials: dict = field(default_factory=dict)
    demo_screens: dict = field(default_factory=dict)


@dataclass
class WorkerResult:
    robot_id:    str
    kiosk_id:    str
    run_id:      str
    status:      str        # "completed" | "failed" | "running"
    total:       int = 0
    passed:      int = 0
    failed:      int = 0
    error:       Optional[str] = None
    results:     list = field(default_factory=list)
    started_at:  float = 0.0
    finished_at: float = 0.0


@dataclass
class SupervisorResult:
    workers:       list[WorkerResult]
    total_tests:   int
    total_passed:  int
    total_failed:  int
    wall_time_s:   float
