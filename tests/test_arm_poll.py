"""Arm _poll must complete when the physical arm settles to 'ready' (not the spec's 'idle').

Regression for the 2026-07-28 live TC-RPS-001 run: the arm PHYSICALLY clicked the email field, but the
tap-poll waited for state 'idle' and the arm reported 'ready' → 30s timeout → abort → step failed. The
arm uses the same ready-state convention as the AGV base; _poll now treats 'ready' and 'idle' as terminal
and does not hang on a cmd_id the controller doesn't echo.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest
import vision_agent.robot.real_robot as rr


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(rr.settings, "robot_poll_interval_s", 0.001)


def _seq_get(states):
    """Return a fake _get that yields the given state dicts, then repeats the last."""
    i = {"n": 0}

    def _get(ep):
        v = states[min(i["n"], len(states) - 1)]
        i["n"] += 1
        return v
    return _get


def test_poll_completes_on_ready_with_cmd_id(monkeypatch):
    monkeypatch.setattr(rr, "_get", _seq_get([
        {"state": "moving", "cmd_id": "cmd-2"},
        {"state": "moving", "cmd_id": "cmd-2"},
        {"state": "ready",  "cmd_id": "cmd-2"},
    ]))
    res = rr._poll("/arm/state", "cmd-2", timeout_s=5.0, abort_ep="/arm/abort")
    assert res["state"] == "ready"


def test_poll_completes_on_ready_without_cmd_id(monkeypatch):
    # Some controllers don't echo a usable cmd_id → state is authoritative (like the base).
    monkeypatch.setattr(rr, "_get", _seq_get([{"state": "moving"}, {"state": "ready"}]))
    res = rr._poll("/arm/state", "cmd-9", timeout_s=5.0, abort_ep="/arm/abort")
    assert res["state"] == "ready"


def test_poll_completes_on_idle_still_works(monkeypatch):
    # The spec's 'idle' must still be accepted (no regression).
    monkeypatch.setattr(rr, "_get", _seq_get([{"state": "idle", "cmd_id": "cmd-1"}]))
    res = rr._poll("/arm/state", "cmd-1", timeout_s=5.0)
    assert res["state"] == "idle"


def test_poll_raises_on_error(monkeypatch):
    monkeypatch.setattr(rr, "_get", _seq_get([{"state": "error", "cmd_id": "cmd-2"}]))
    with pytest.raises(RuntimeError):
        rr._poll("/arm/state", "cmd-2", timeout_s=5.0)


def test_poll_times_out_when_never_terminal(monkeypatch):
    # An arm that stays 'moving' must still time out (not hang forever).
    monkeypatch.setattr(rr, "_get", _seq_get([{"state": "moving", "cmd_id": "cmd-2"}]))
    monkeypatch.setattr(rr, "_post", lambda *a, **k: {})  # swallow the abort POST
    with pytest.raises(TimeoutError):
        rr._poll("/arm/state", "cmd-2", timeout_s=0.05, abort_ep="/arm/abort")


def test_card_terminal_states_unchanged():
    # Card ops still complete in 'holding_card' (arm keeps gripping) — not affected by the arm default.
    assert "holding_card" in rr._CARD_HOLD_STATES
    assert "ready" not in rr._CARD_HOLD_STATES   # a card pick must NOT be 'done' at bare 'ready'


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
