"""
finalize_tests — compute overall suite outcome and persist results to JSON.
"""
import json
import time
from pathlib import Path
from vision_agent.config import settings
from test_runner.state import TestRunnerState


def finalize_tests(state: TestRunnerState) -> dict:
    results = state.get("test_results") or []
    total   = len(results)
    passed  = sum(1 for r in results if r["outcome"] == "passed")
    failed  = total - passed

    summary_lines = [
        f"Suite: {passed}/{total} passed, {failed}/{total} failed",
        "",
    ]
    for r in results:
        icon = "PASS" if r["outcome"] == "passed" else "FAIL"
        summary_lines.append(f"  [{icon}] {r['test_id']}  {r['summary'][:70]}")

    summary = "\n".join(summary_lines)

    # Persist
    doc = {
        "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total":        total,
        "passed":       passed,
        "failed":       failed,
        "test_results": results,
    }
    from ports import paths as tenant_paths   # tenant-scoped in multi-tenant; == MVP when single
    out_dir  = tenant_paths.results_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"suite_{int(time.time())}.json"
    out_path.write_text(json.dumps(doc, indent=2))

    print(f"\n{'='*60}")
    print(summary)
    print(f"\n  Results saved -> {out_path}")
    print(f"{'='*60}")

    return {"summary": summary}
