"""Arm _poll is STATE-AUTHORITATIVE and spec-aligned: a command is done 'once state is no longer
moving' (robot_kiosk_api.md). The physical arm settles to 'ready' (not the spec's 'idle') and echoes
its OWN cmd_id, so completion must NOT gate on a cmd_id match. Regression for the 2026-07-28/29 live
TC-RPS-001 runs (arm tap-poll hung waiting for 'idle'/our cmd_id).
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
    monkeypatch.setattr(rr.settings, "arm_settle_grace_s", 0.05)
    monkeypatch.setattr(rr.settings, "arm_status_tick_s", 0.01)
    # Recovery hits the real network; stub it out so _poll's error/timeout paths stay unit-isolated.
    monkeypatch.setattr(rr, "_recover_arm", lambda: None)


def _seq_get(states):
    """Return a fake _get_quiet that yields the given state dicts, then repeats the last."""
    i = {"n": 0}

    def _get(ep, *a, **k):
        v = states[min(i["n"], len(states) - 1)]
        i["n"] += 1
        return v
    return _get


def test_poll_completes_on_ready_with_cmd_id(monkeypatch):
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([
        {"state": "moving", "cmd_id": "c-030"},
        {"state": "moving", "cmd_id": "c-030"},
        {"state": "ready",  "cmd_id": "c-030"},   # arm echoes its OWN id, not ours
    ]))
    res = rr._poll("/arm/state", "cmd-2-arm-click", timeout_s=5.0, abort_ep="/arm/abort",
                   initial_state="moving")
    assert res["state"] == "ready"


def test_poll_completes_on_ready_without_cmd_id(monkeypatch):
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([{"state": "moving"}, {"state": "ready"}]))
    res = rr._poll("/arm/state", "cmd-9", timeout_s=5.0, abort_ep="/arm/abort", initial_state="moving")
    assert res["state"] == "ready"


def test_poll_completes_on_idle_still_works(monkeypatch):
    # The spec's 'idle' must still be accepted (no regression).
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([{"state": "moving"}, {"state": "idle"}]))
    res = rr._poll("/arm/state", "cmd-1", timeout_s=5.0, initial_state="moving")
    assert res["state"] == "idle"


def test_poll_raises_on_error(monkeypatch):
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([{"state": "error", "cmd_id": "c-1"}]))
    with pytest.raises(RuntimeError):
        rr._poll("/arm/state", "cmd-2", timeout_s=5.0)


def test_poll_times_out_when_never_terminal(monkeypatch):
    # An arm that stays 'moving' must still time out (not hang forever).
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([{"state": "moving", "cmd_id": "c-1"}]))
    monkeypatch.setattr(rr, "_post", lambda *a, **k: {})   # swallow the abort POST
    with pytest.raises(TimeoutError):
        rr._poll("/arm/state", "cmd-2", timeout_s=0.05, abort_ep="/arm/abort", initial_state="moving")


def test_poll_ignores_stale_ready_before_moving(monkeypatch):
    # The POST ack said 'moving'; the first sample is a STALE 'ready' from the PREVIOUS command. _poll
    # must NOT accept it as instant completion — it waits to observe 'moving', then the real 'ready'.
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([
        {"state": "ready"},    # stale (previous command) — must be ignored
        {"state": "moving"},   # command actually started
        {"state": "moving"},
        {"state": "ready"},    # real completion
    ]))
    res = rr._poll("/arm/state", "cmd-2", timeout_s=5.0, initial_state="moving")
    assert res["state"] == "ready"
    # sanity: it did not return on the very first (stale) sample
    assert rr._get_quiet is not None


def test_poll_accepts_terminal_without_moving_when_ack_not_moving(monkeypatch):
    # No 'moving' expected (initial_state blank) → a terminal state is accepted immediately (fast path
    # for state queries / instant ops). Keeps existing callers snappy.
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([{"state": "ready"}]))
    res = rr._poll("/arm/state", "cmd-x", timeout_s=5.0)
    assert res["state"] == "ready"


def test_poll_card_requires_holding_card(monkeypatch):
    # Card pick must complete only in a card-hold state, not a bare pre-grip 'idle'.
    monkeypatch.setattr(rr, "_get_quiet", _seq_get([
        {"state": "moving"}, {"state": "holding_card"},
    ]))
    res = rr._poll("/arm/state", "c-40", timeout_s=5.0, terminal_states=rr._CARD_HOLD_STATES,
                   initial_state="moving")
    assert res["state"] == "holding_card"


def test_card_terminal_states_unchanged():
    assert "holding_card" in rr._CARD_HOLD_STATES
    assert "ready" not in rr._CARD_HOLD_STATES   # a card pick must NOT be 'done' at bare 'ready'


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
