"""Runtime self-calibration of the vertical viewport→camera tap mapping (2026-08-10).

Proves the fix for "email tap lands ~2 cm below the field": a static CAMERA_CALIB_AY/BY fit to one
pose mis-places taps when the robot's rectification crop changes. The login screen's own input boxes
are detected in the live camera frame and the per-pose vertical affine is derived from them, so taps
land on element CENTRES regardless of the crop — verified against the REAL captured login frame.
"""
import io
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from PIL import Image, ImageDraw

from vision_agent.config import settings
from vision_agent.vision import screen_calibrate as sc

_REAL_LOGIN = Path("reference_screens/sign_in_click.png")   # 491×462 real arm capture (aspect 1.06)


def _synthetic_login(w=500, h=460, email_cy=200, pwd_cy=300, box_h=44) -> bytes:
    """Dark auth card with two light input boxes at known centres — deterministic detector input."""
    img = Image.new("RGB", (w, h), (30, 60, 110))
    d = ImageDraw.Draw(img)
    for cy in (email_cy, pwd_cy):
        d.rectangle([int(w * 0.36), cy - box_h // 2, int(w * 0.64), cy + box_h // 2], fill=(225, 232, 240))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_detect_form_field_fracs_finds_two_boxes():
    fr = sc.detect_form_field_fracs(_synthetic_login(email_cy=200, pwd_cy=300))
    assert len(fr) >= 2
    assert abs(fr[0] - 200 / 460) < 0.02          # email centre
    assert abs(fr[1] - 300 / 460) < 0.02          # password centre


def test_fit_vertical_affine_and_bounds():
    # email monitor 0.47 → camera 0.43 ; password monitor 0.62 → camera 0.63
    fit = sc.fit_vertical_affine(0.47, 0.62, 0.43, 0.63)
    assert fit is not None
    ay, by = fit
    assert abs(ay - (0.43 - 0.63) / (0.47 - 0.62)) < 1e-6
    # Out-of-bounds slope is rejected (detection error → caller keeps config)
    assert sc.fit_vertical_affine(0.47, 0.62, 0.10, 0.95) is None   # ay ≈ 5.7 > max
    assert sc.fit_vertical_affine(0.5, 0.5, 0.4, 0.6) is None       # degenerate


def test_derive_login_vertical_maps_boxes_to_centres():
    res = sc.derive_login_vertical(_synthetic_login(email_cy=200, pwd_cy=300), 0.47, 0.62)
    assert res is not None
    # The derived affine must send the monitor fracs to the detected camera fracs.
    assert abs(0.47 * res["ay"] + res["by"] - res["email_cam_frac"]) < 1e-6
    assert abs(0.62 * res["ay"] + res["by"] - res["password_cam_frac"]) < 1e-6


def test_derive_returns_none_without_two_boxes():
    blank = Image.new("RGB", (400, 400), (30, 60, 110))
    buf = io.BytesIO(); blank.save(buf, "PNG")
    assert sc.derive_login_vertical(buf.getvalue(), 0.47, 0.62) is None


def test_find_login_anchors():
    screen = {"elements": [
        {"id": "email_input", "center": [960, 508]},
        {"id": "password_input", "center": [960, 668]},
        {"id": "sign_in_button", "center": [960, 792]},
    ]}
    assert sc.find_login_anchors(screen) == (508.0, 668.0)
    assert sc.find_login_anchors({"elements": [{"id": "menu", "center": [1, 2]}]}) is None


def test_real_frame_email_lands_on_box_centre():
    """The exact operator scenario: on the REAL login capture, the derived affine sends the app_map
    email/password monitor centres onto the true camera box centres (email ≈y198, password ≈y292 in
    the 491×462 frame) — instead of the ~y262 gap the stale static calibration produced."""
    if not _REAL_LOGIN.exists():
        import pytest; pytest.skip("real login frame not present")
    img = _REAL_LOGIN.read_bytes()
    w, h = Image.open(io.BytesIO(img)).size
    res = sc.derive_login_vertical(img, 508 / settings.viewport_height, 668 / settings.viewport_height)
    assert res is not None
    email_v = round((res["ay"] * (508 / settings.viewport_height) + res["by"]) * h)
    pwd_v = round((res["ay"] * (668 / settings.viewport_height) + res["by"]) * h)
    assert abs(email_v - 198) <= 6, email_v         # on the email box (was ~262, in the gap)
    assert abs(pwd_v - 292) <= 6, pwd_v


def test_real_robot_scale_uses_derived_calibration(monkeypatch):
    if not _REAL_LOGIN.exists():
        import pytest; pytest.skip("real login frame not present")
    from vision_agent.robot import real_robot as rr
    w, h = Image.open(io.BytesIO(_REAL_LOGIN.read_bytes())).size
    rr._calibration["scale_x"] = w / settings.viewport_width
    rr._calibration["scale_y"] = h / settings.viewport_height
    monkeypatch.setattr(settings, "camera_calib_ay", 1.0)   # identity fallback
    monkeypatch.setattr(settings, "camera_calib_by", 0.0)
    monkeypatch.setattr(settings, "auto_tap_calibration", True)
    try:
        before = rr._scale(960, 508)[1]
        info = rr.calibrate_vertical_from_login(str(_REAL_LOGIN), 508.0, 668.0)
        assert info["applied"] is True
        after = rr._scale(960, 508)[1]
        assert abs(after - 198) <= 6                # lands on the email box centre after calibration
        assert after != before                      # calibration actually changed the mapping
        # Disabling the toggle refuses to calibrate (config-only path).
        rr._calibration.pop("calib_ay", None); rr._calibration.pop("calib_by", None)
        monkeypatch.setattr(settings, "auto_tap_calibration", False)
        assert rr.calibrate_vertical_from_login(str(_REAL_LOGIN), 508.0, 668.0)["applied"] is False
    finally:
        rr._calibration.clear()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
