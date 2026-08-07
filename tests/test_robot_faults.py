"""Real-robot fault handling + per-run artifacts (2026-08-07).

Covers the four fixes from the first live RPS sign-in fault run:
  1. _poll fails FAST when the robot REST API is unreachable (was retrying the whole 345s deadline).
  2. A genuine robot fault (DESCEND/HOVER/timeout/unreachable) → outcome "robot_error" so the run
     STOPS instead of handing to Tier-3; a data issue (keyboard_map) stays "failed" (→ Tier-3).
  3. tap saves an annotated BEFORE screenshot (crosshair at the tapped camera pixel).
  4. Every run's results are PRESERVED under results/<run_id>/ (results.json + run.log).
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest


# ── 1. _poll fails fast when the robot API is unreachable ────────────────────────

def test_poll_fails_fast_when_api_unreachable(monkeypatch):
    from vision_agent.robot import real_robot as rr
    from vision_agent.config import settings
    monkeypatch.setattr(settings, "robot_unreachable_timeout_s", 1.0)
    monkeypatch.setattr(settings, "robot_poll_interval_s", 0.05)

    def _boom(ep, timeout=None):
        raise ConnectionError("actively refused")
    monkeypatch.setattr(rr, "_get_quiet", _boom)

    t0 = time.time()
    # Long command deadline (would be 345s for a real type) — the unreachable cap must trip near 1s.
    with pytest.raises(ConnectionError):
        rr._poll("/arm/state", "cmd-x", timeout_s=300.0)
    elapsed = time.time() - t0
    assert elapsed < 10.0, f"unreachable poll should fail near ~1s, took {elapsed:.1f}s"


def test_poll_recovers_when_api_comes_back(monkeypatch):
    # A few connection errors then a real 'ready' → the unreachable timer resets and the poll succeeds.
    from vision_agent.robot import real_robot as rr
    from vision_agent.config import settings
    monkeypatch.setattr(settings, "robot_unreachable_timeout_s", 5.0)
    monkeypatch.setattr(settings, "robot_poll_interval_s", 0.01)
    calls = {"n": 0}

    def _flaky(ep, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("refused")
        return {"state": "ready"}
    monkeypatch.setattr(rr, "_get_quiet", _flaky)
    st = rr._poll("/arm/state", "cmd-y", timeout_s=30.0)
    assert st.get("state") == "ready"


# ── 2. robot fault → "robot_error"; data issue → "failed" ────────────────────────

def _one_tap_plan():
    return {"steps": [{"action": "tap", "px": 100, "py": 100,
                       "element_id": "email", "screen_id": "login"}]}


def _type_plan():
    return {"steps": [{"action": "type", "value": "hi", "px": 10, "py": 10,
                       "element_id": "email", "screen_id": "login"}]}


def test_tap_hardware_fault_yields_robot_error(monkeypatch):
    import test_runner.nodes.run_vision_step as rvs
    monkeypatch.setattr(rvs, "_load_device_map", lambda: {})

    class FakeRobot:
        def tap(self, x, y):
            raise RuntimeError("Arm click cmd-2 did NOT land: completed 0/1 (code=DESCEND_LIN_FAILED)")
    monkeypatch.setattr(rvs, "robot", FakeRobot())

    steps, outcome, _ = rvs._execute_structured_plan(_one_tap_plan(), {}, run_id="", test_id="T", app_map={})
    assert outcome == "robot_error"
    assert steps[-1]["method"] == "robot_error"


def test_type_keyboard_missing_stays_failed(monkeypatch):
    # A DATA problem (no keyboard_map) is NOT a robot fault → outcome "failed" (Tier-3 handoff kept).
    import test_runner.nodes.run_vision_step as rvs
    monkeypatch.setattr(rvs, "_load_device_map", lambda: {})

    class FakeRobot:
        def tap(self, x, y): return {}
        def type_text(self, v, clear_first=False):
            return {"success": False, "error": "keyboard_map not loaded"}
    monkeypatch.setattr(rvs, "robot", FakeRobot())

    steps, outcome, _ = rvs._execute_structured_plan(_type_plan(), {}, run_id="", test_id="T", app_map={})
    assert outcome == "failed"
    assert steps[-1]["method"] == "type_failed"


def test_type_hardware_fault_yields_robot_error(monkeypatch):
    # A partial type via a click that did-not-land IS a robot fault → "robot_error" (stop, no Tier-3).
    import test_runner.nodes.run_vision_step as rvs
    monkeypatch.setattr(rvs, "_load_device_map", lambda: {})

    class FakeRobot:
        def tap(self, x, y): return {}
        def type_text(self, v, clear_first=False):
            return {"success": False, "error": "Arm click did NOT land: completed 1/3 (code=HOVER_FAILED)"}
    monkeypatch.setattr(rvs, "robot", FakeRobot())

    steps, outcome, _ = rvs._execute_structured_plan(_type_plan(), {}, run_id="", test_id="T", app_map={})
    assert outcome == "robot_error"
    assert steps[-1]["method"] == "robot_error"


def test_is_robot_fault_classifier():
    import test_runner.nodes.run_vision_step as rvs
    assert rvs._is_robot_fault("completed 0/1 (code=DESCEND_LIN_FAILED)")
    assert rvs._is_robot_fault("HTTPConnectionPool ... actively refused")
    assert rvs._is_robot_fault("Robot /arm/state timed out after 60s")
    assert not rvs._is_robot_fault("keyboard_map not loaded")
    assert not rvs._is_robot_fault("no characters in keyboard_map")


# ── 3. BEFORE-click annotation ───────────────────────────────────────────────────

def test_annotate_click_writes_before_image(tmp_path):
    from PIL import Image
    from vision_agent.robot import real_robot as rr
    src = tmp_path / "frame.png"
    Image.new("RGB", (200, 120), (10, 20, 30)).save(src)
    out = tmp_path / "before_cmd-2_at_50-60.png"
    assert rr._annotate_click(str(src), [(50, 60)], str(out)) == str(out)
    assert out.exists()
    # A bad source must NEVER raise — returns "" so a tap is never broken by the screenshot aid.
    assert rr._annotate_click(str(tmp_path / "missing.png"), [(1, 1)], str(tmp_path / "x.png")) == ""


# ── 4. per-run results are preserved ─────────────────────────────────────────────

def test_write_run_artifacts_preserves_each_run(tmp_path, monkeypatch):
    import api.main as m
    from vision_agent.config import settings
    monkeypatch.setattr(settings, "results_dir", str(tmp_path))

    class Run:
        mode = "real"; kiosk_id = "kiosk-2"; started_at = "2026-08-07T16:00:00"
        total = 1; passed = 0; failed = 1; status = "completed"

    trs = [{
        "test_id": "TC-RPS-001", "summary": "Sign in to the RPS POS", "outcome": "failed",
        "vision_summary": "ROBOT ERROR [real] — run STOPPED, Tier-3 skipped",
        "step_results": [
            {"step": "tap: email_input @ (959,554)", "success": True, "method": "app_map",
             "screenshot_before": "before_cmd-2-arm-click_at_258-243.png",
             "screenshot_after": "after_cmd-2-arm-click.jpg"},
            {"step": "tap: email_input", "success": False, "method": "robot_error",
             "observation": "Arm click did NOT land (code=DESCEND_LIN_FAILED)"},
        ],
    }]
    events = [{"request_time": "16:03:32.690", "event_type": "POST", "endpoint": "/screen/click",
               "cmd_id": "cmd-2-arm-click", "u": 258, "v": 243, "http_status": 202,
               "latency_ms": 424.4, "controller": "192.168.0.107:8000"}]

    m._write_run_artifacts("run-3-160327-0708", Run(), trs, events)

    d = tmp_path / "run-3-160327-0708"
    assert (d / "results.json").exists()
    assert (d / "run.log").exists()
    log = (d / "run.log").read_text(encoding="utf-8")
    assert "TC-RPS-001" in log
    assert "959,554" in log                     # the tapped viewport coordinate is in the step label
    assert "DESCEND_LIN_FAILED" in log          # the fault reason is preserved
    assert "cam(258,243)" in log                # robot telemetry with the exact camera pixel

    # A SECOND run with a different id must NOT overwrite the first.
    m._write_run_artifacts("run-4-160500-0708", Run(), trs, events)
    assert (tmp_path / "run-3-160327-0708" / "results.json").exists()
    assert (tmp_path / "run-4-160500-0708" / "results.json").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
