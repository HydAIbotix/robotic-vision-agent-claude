"""
Result Publisher Node — persist each defect record to the DB and emit a
WebSocket event so the UI can update immediately.

The broadcast function is retrieved from test_runner.broadcaster using the
run_id — no callable is stored in LangGraph state (avoids serialisation issues).
"""
from defect_agent.state import DefectAgentState


def result_publisher_node(state: DefectAgentState) -> dict:
    defects  = state.get("defects") or []
    run_id   = state["run_id"]
    if not defects:
        return {}

    # DB session — import here to avoid circular imports at module level
    from api.database import SessionLocal
    from api import models

    db = SessionLocal()
    try:
        for d in defects:
            record = models.Defect(
                run_id             = d["run_id"],
                test_id            = d["test_id"],
                title              = d["title"],
                description        = d["description"],
                steps_to_reproduce = d["steps_to_reproduce"],
                root_cause         = d["root_cause"],
                probable_fix       = d["probable_fix"],
                severity           = d["severity"],
                priority           = d["priority"],
                jira_key           = d["jira_key"],
                jira_url           = d["jira_url"],
                status             = "open",
                evidence_json      = d.get("evidence") or [],
            )
            db.add(record)
        db.commit()
        print(f"  [DEFECT:PUBLISH] {len(defects)} defect(s) saved to DB for run {run_id}")
    except Exception as e:
        print(f"  [DEFECT:PUBLISH] DB error: {e}")
        db.rollback()
    finally:
        db.close()

    # Broadcast via the registered handler (if run is still active)
    from test_runner import broadcaster
    broadcaster.emit(run_id, {
        "event":   "defects_ready",
        "run_id":  run_id,
        "count":   len(defects),
        "defects": [
            {
                "test_id":  d["test_id"],
                "jira_key": d["jira_key"],
                "jira_url": d["jira_url"],
                "severity": d["severity"],
                "title":    d["title"],
            }
            for d in defects
        ],
    })

    return {}
