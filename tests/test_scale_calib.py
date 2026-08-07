"""real_robot._scale per-axis affine camera calibration (2026-08-07).

The arm /capture type=screen frame is a VERTICAL CROP of the display, so an app_map element's camera
y-position is a linear-but-different function of its monitor y-position — the email tap landed on the
"Email" LABEL ~67px above the input-box centre. _scale now applies a per-axis affine correction in
FRACTION space (settings.camera_calib_a*/b*). Default a=1,b=0 must be a byte-identical NO-OP.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from vision_agent.config import settings
from vision_agent.robot import real_robot as rr


def _set_cam(w, h):
    # Force the measured calibration so _scale uses a known camera resolution.
    rr._calibration["scale_x"] = w / settings.viewport_width
    rr._calibration["scale_y"] = h / settings.viewport_height


def test_default_calibration_is_noop(monkeypatch):
    # a=1,b=0 on both axes → identical to the plain camera/viewport scale (no regression).
    monkeypatch.setattr(settings, "viewport_width", 1920)
    monkeypatch.setattr(settings, "viewport_height", 1080)
    monkeypatch.setattr(settings, "camera_calib_ax", 1.0)
    monkeypatch.setattr(settings, "camera_calib_bx", 0.0)
    monkeypatch.setattr(settings, "camera_calib_ay", 1.0)
    monkeypatch.setattr(settings, "camera_calib_by", 0.0)
    _set_cam(1405, 579)
    # plain scale: u = 960*(1405/1920)=702, v = 508*(579/1080)=272
    assert rr._scale(960, 508) == (702, 272)
    assert rr._scale(0, 0) == (0, 0)
    assert rr._scale(1920, 1080) == (1405, 579)


def test_vertical_calibration_lands_on_field_centre(monkeypatch):
    # With the RPS-derived vertical calibration, the email/password/sign-in monitor coords map to the
    # measured TRUE camera box centres (email y≈328, password y≈432, sign-in y≈513) instead of ~67px high.
    monkeypatch.setattr(settings, "viewport_width", 1920)
    monkeypatch.setattr(settings, "viewport_height", 1080)
    monkeypatch.setattr(settings, "camera_calib_ax", 1.0)
    monkeypatch.setattr(settings, "camera_calib_bx", 0.0)
    monkeypatch.setattr(settings, "camera_calib_ay", 1.212)
    monkeypatch.setattr(settings, "camera_calib_by", -0.0034)
    _set_cam(1405, 579)
    u, v = rr._scale(960, 508)      # email
    assert u == 702                 # horizontal unchanged (faithful)
    assert abs(v - 328) <= 2        # was 272 (on the label) → now the input-box centre
    _, v2 = rr._scale(960, 668)     # password
    assert abs(v2 - 432) <= 2
    _, v3 = rr._scale(960, 792)     # sign-in
    assert abs(v3 - 513) <= 2


def teardown_module(module):
    rr._calibration.clear()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
