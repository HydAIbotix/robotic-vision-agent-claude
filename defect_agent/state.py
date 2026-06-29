from typing import Optional
from typing_extensions import TypedDict


class DefectAgentState(TypedDict):
    run_id: str
    kiosk_id: str
    failed_results: list[dict]   # TestResult dicts (outcome == "failed")
    evaluations: list[dict]      # {test_id, severity, root_cause, affected_area}
    defects: list[dict]          # fully structured defect records ready for DB
