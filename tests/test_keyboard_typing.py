"""Keyboard typing on the real robot + the merge that must PRESERVE the keyboard_map.

Regression for the 2026-07-29 live TC-RPS-001 run: the arm tapped the email field but typed NOTHING.
Two root causes, both covered here:
  1. app_map/store.merge_explored_app dropped the top-level `keyboard_map` on a multi-app save, so
     app_map.json had no keyboard and real_robot.type_text had nothing to tap.
  2. real_robot.type_text did not dismiss the on-screen keyboard (Done key) after typing, so it stayed
     open and covered the next field — the following focus tap landed on a key and the arm hung.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from app_map import store
import vision_agent.robot.real_robot as rr


# ── merge_explored_app must preserve the environment-wide keyboard_map ──────────────

def _kmap(keys):
    return {"keys": keys}


def test_merge_preserves_new_keyboard_map():
    existing = {"screens": {}, "apps": {}}
    new_map = {"screens": {"login": {"elements": [1]}}, "entry_screen": "login",
               "keyboard_map": _kmap({"a": [0.1, 0.2], "done": [0.7, 0.98]})}
    merged = store.merge_explored_app(existing, new_map, app_id="kiosk-2")
    assert "keyboard_map" in merged, "fresh exploration's keyboard_map must survive the merge"
    assert len(merged["keyboard_map"]["keys"]) == 2


def test_merge_keeps_existing_keyboard_map_when_new_has_none():
    # Re-exploring an app that did NOT remap the keyboard must not wipe the prior one.
    existing = {"screens": {}, "apps": {}, "keyboard_map": _kmap({"a": [0.1, 0.2]})}
    new_map  = {"screens": {"products": {"elements": [1]}}}   # no keyboard_map
    merged   = store.merge_explored_app(existing, new_map, app_id="kiosk-1")
    assert merged.get("keyboard_map", {}).get("keys", {}).get("a") == [0.1, 0.2]


def test_merge_new_keyboard_map_overrides_old():
    existing = {"screens": {}, "apps": {}, "keyboard_map": _kmap({"a": [0.1, 0.1]})}
    new_map  = {"screens": {"login": {"elements": [1]}},
                "keyboard_map": _kmap({"a": [0.9, 0.9], "b": [0.8, 0.8]})}
    merged   = store.merge_explored_app(existing, new_map, app_id="kiosk-2")
    assert merged["keyboard_map"]["keys"]["a"] == [0.9, 0.9]
    assert "b" in merged["keyboard_map"]["keys"]


def test_merge_preserves_other_apps_and_screens():
    existing = {"screens": {"vps_home": {"app_id": "kiosk-1", "elements": [1]}},
                "apps": {"kiosk-1": {"app_id": "kiosk-1"}},
                "keyboard_map": _kmap({"a": [0.1, 0.2]})}
    new_map  = {"screens": {"login": {"elements": [1]}}}
    merged   = store.merge_explored_app(existing, new_map, app_id="kiosk-2")
    assert "vps_home" in merged["screens"]          # other app's screen kept
    assert "login" in merged["screens"]             # new app's screen added
    assert "keyboard_map" in merged                 # keyboard kept


# ── real_robot.type_text: keyboard load + dismiss behaviour ─────────────────────────

@pytest.fixture
def typing_robot(monkeypatch):
    """Patch the real robot so type_text runs without hardware; capture the /screen/click body."""
    posted = {}

    def fake_post(ep, body):
        posted["ep"] = ep
        posted["body"] = body
        return {"status": "accepted"}

    monkeypatch.setattr(rr, "_post", fake_post)
    monkeypatch.setattr(rr, "_poll", lambda *a, **k: {"state": "ready"})
    monkeypatch.setattr(rr, "_ensure_localized", lambda: None)
    # Calibration → 1:1 so u/v equal viewport pixels; keep numbers simple.
    monkeypatch.setattr(rr, "_calibration", {"scale_x": 1.0, "scale_y": 1.0})
    return posted


def test_type_text_no_keyboard_map_fails(monkeypatch, typing_robot):
    monkeypatch.setattr(rr, "_keyboard_map", {})
    res = rr.type_text("abc")
    assert res["success"] is False
    assert "keyboard_map" in res["error"]
    assert "ep" not in typing_robot          # nothing posted to the arm


def test_type_text_types_and_appends_done(monkeypatch, typing_robot):
    kmap = {"a": [0.10, 0.90], "b": [0.20, 0.90], "done": [0.74, 0.977]}
    monkeypatch.setattr(rr, "_keyboard_map", kmap)
    res = rr.type_text("ab")
    assert res["success"] is True
    assert res["tapped_keys"] == 2           # 2 chars (Done not counted as a char)
    pts = typing_robot["body"]["points"]
    assert len(pts) == 3                      # a, b, + Done dismiss tap
    # last point is the Done key. Keys now route through _scale_key → _scale (which rounds), so allow a
    # 1px tolerance vs the raw fraction×dim (the mapping reuses the login vmap; identity here).
    assert abs(pts[-1]["u"] - int(0.74 * rr.settings.viewport_width)) <= 1
    assert abs(pts[-1]["v"] - int(0.977 * rr.settings.viewport_height)) <= 1


def test_type_text_uppercase_taps_shift(monkeypatch, typing_robot):
    kmap = {"p": [0.10, 0.90], "shift": [0.05, 0.977], "done": [0.74, 0.977]}
    monkeypatch.setattr(rr, "_keyboard_map", kmap)
    res = rr.type_text("P")
    assert res["success"] is True
    pts = typing_robot["body"]["points"]
    # shift, then p, then done
    assert len(pts) == 3


def test_type_text_all_chars_unmapped_fails(monkeypatch, typing_robot):
    # Characters that are not on the keyboard → cannot type → failure (not a false success).
    monkeypatch.setattr(rr, "_keyboard_map", {"done": [0.74, 0.977]})
    res = rr.type_text("xyz")
    assert res["success"] is False
    assert "ep" not in typing_robot


def test_type_text_empty_dismisses_only(monkeypatch, typing_robot):
    # type_text("") is used to DISMISS the keyboard → taps Done only, reports success, 0 chars.
    monkeypatch.setattr(rr, "_keyboard_map", {"a": [0.1, 0.9], "done": [0.74, 0.977]})
    res = rr.type_text("")
    assert res["success"] is True
    assert res["tapped_keys"] == 0
    assert len(typing_robot["body"]["points"]) == 1   # just Done


# ── click-completion detection: a terminal state does NOT mean the touch landed ─────

def test_check_click_completed_raises_on_partial():
    # completed < total → the touch did not land (e.g. DESCEND_LIN_FAILED, completed=0/total=1).
    state = {"state": "idle", "click_result": {"completed": 0, "total": 1,
                                               "code": "DESCEND_LIN_FAILED", "detail": "descend failed"}}
    with pytest.raises(RuntimeError):
        rr._check_click_completed(state, {}, "cmd-2-arm-click")


def test_check_click_completed_ok_when_complete():
    state = {"state": "idle", "click_result": {"completed": 1, "total": 1}}
    rr._check_click_completed(state, {}, "cmd-2")   # must NOT raise


def test_check_click_completed_ok_when_absent():
    # No click_result (no capture_after_last / older controller) → treated as OK, no false failure.
    rr._check_click_completed({"state": "ready"}, {}, "cmd-2")   # must NOT raise


def test_tap_raises_on_failed_descend(monkeypatch):
    # tap() must surface a click that reported completed<total so the runner fails the step.
    monkeypatch.setattr(rr, "_ensure_localized", lambda: None)
    monkeypatch.setattr(rr, "_scale", lambda x, y: (x, y))
    monkeypatch.setattr(rr, "_post", lambda ep, body, **k: {"status": "accepted"})
    monkeypatch.setattr(rr, "_poll", lambda *a, **k: {
        "state": "ready", "click_result": {"completed": 0, "total": 1, "code": "DESCEND_LIN_FAILED"}})
    with pytest.raises(RuntimeError):
        rr.tap(635, 320)


def test_type_text_timeout_scales_with_key_count(monkeypatch, typing_robot):
    # The type poll deadline must grow with the number of keys (each is a slow physical tap), not the
    # old flat +0.1s/key that timed out mid-word on the real arm.
    seen = {}
    def rec_poll(ep, cmd_id, timeout_s, **k):
        seen["timeout"] = timeout_s
        return {"state": "ready", "click_result": {"completed": 99, "total": 99}}
    monkeypatch.setattr(rr, "_poll", rec_poll)
    kmap = {c: [0.1, 0.9] for c in "abcdefghij"}
    kmap["done"] = [0.74, 0.977]
    monkeypatch.setattr(rr, "_keyboard_map", kmap)
    rr.type_text("abcdefghij")   # 10 chars + Done = 11 taps
    expected = rr.settings.arm_move_timeout_s + 11 * rr.settings.arm_key_tap_timeout_s
    assert seen["timeout"] == expected
    assert seen["timeout"] > 100   # generous, not the old ~62s


def test_type_text_fails_on_partial_type(monkeypatch, typing_robot):
    # A key tap that fails mid-sequence (completed<total) → type_text reports failure, not success.
    monkeypatch.setattr(rr, "_keyboard_map", {"a": [0.1, 0.9], "b": [0.2, 0.9], "done": [0.74, 0.977]})
    monkeypatch.setattr(rr, "_poll", lambda *a, **k: {
        "state": "ready", "click_result": {"completed": 1, "total": 3, "code": "IK_FAIL"}})
    res = rr.type_text("ab")
    assert res["success"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
