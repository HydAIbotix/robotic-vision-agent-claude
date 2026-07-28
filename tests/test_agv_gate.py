"""Per-test AGV positioning gate — the runner must NOT auto-drive the base unless a test's steps
explicitly ask to move (real backend). Prevents a single-kiosk sign-in test from trying to relocate
the robot (observed: TC-RPS-001 tried to drive the AGV to kiosk-2 with the AGV controller down)."""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from test_runner.nodes.run_vision_step import _test_wants_agv_move, _position_for_test
from vision_agent.config import settings


# ── intent detection ─────────────────────────────────────────────────────────

def test_signin_test_does_not_want_move():
    assert _test_wants_agv_move({"steps_raw": "1. At RPS, enter email and password\n2. Tap 'Sign In'"}) is False


def test_ui_navigation_is_not_a_base_move():
    # "go to the products page" is a UI action, not a physical base move (no device token).
    assert _test_wants_agv_move({"steps_raw": "Go to the products page and add to cart"}) is False
    assert _test_wants_agv_move({"steps_raw": "Proceed to card payment and pay"}) is False


def test_explicit_moves_are_detected():
    for s in [
        "Move the AGV to Kiosk-1",
        "Go to VPS device",
        "Navigate to RPS and sign in",
        "Move to Kiosk-2, then sign in",
        "Drive the AGV to VPS, then move back home",
        "Go back to the home position",
        "Return home after the test",
    ]:
        assert _test_wants_agv_move({"steps_raw": s}) is True, s


def test_empty_steps_no_move():
    assert _test_wants_agv_move({"steps_raw": ""}) is False
    assert _test_wants_agv_move({}) is False


# ── gate behaviour in _position_for_test (real backend) ──────────────────────

def test_position_skips_move_without_intent(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "robot_backend", "real")
    monkeypatch.setattr(settings, "agv_move_requires_explicit_step", True)
    import test_runner.nodes.run_vision_step as rv
    monkeypatch.setattr(rv.robot, "navigate_to_kiosk", lambda *a, **k: calls.append(a), raising=False)
    _position_for_test({"kiosk_id": "kiosk-2", "steps_raw": "At RPS, sign in"})
    assert calls == [], "must NOT drive the AGV for a test with no explicit move step"


def test_position_moves_with_intent(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "robot_backend", "real")
    monkeypatch.setattr(settings, "agv_move_requires_explicit_step", True)
    import test_runner.nodes.run_vision_step as rv
    monkeypatch.setattr(rv.robot, "navigate_to_kiosk", lambda *a, **k: calls.append(a), raising=False)
    _position_for_test({"kiosk_id": "kiosk-1", "steps_raw": "Move the AGV to Kiosk-1"})
    assert calls, "must drive the AGV when the test explicitly asks to move"


def test_gate_disabled_restores_always_move(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "robot_backend", "real")
    monkeypatch.setattr(settings, "agv_move_requires_explicit_step", False)  # legacy behaviour
    import test_runner.nodes.run_vision_step as rv
    monkeypatch.setattr(rv.robot, "navigate_to_kiosk", lambda *a, **k: calls.append(a), raising=False)
    _position_for_test({"kiosk_id": "kiosk-2", "steps_raw": "At RPS, sign in"})
    assert calls, "with the gate disabled, positioning always drives (no regression for old behaviour)"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
