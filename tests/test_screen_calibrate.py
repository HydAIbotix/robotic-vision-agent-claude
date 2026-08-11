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
# A real arm login capture used by the newer robustness tests; falls back gracefully when absent.
_REAL_LOGIN2 = next((p for p in (Path("reference_screens/sign_in.png"),
                                 Path("camera_captures/vision_test_screen_1786441593435.png"))
                     if p.exists()), None)


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


# ─────────────────────────────────────────────────────────────────────────────
# 4-anchor piecewise vertical map (2026-08-11) — footer-link fix
# ─────────────────────────────────────────────────────────────────────────────

def test_eval_vmap_interpolates_and_extrapolates():
    knots = [(0.2, 0.3), (0.5, 0.6), (0.7, 0.9)]
    # exact at knots
    assert abs(sc.eval_vmap(knots, 0.2) - 0.3) < 1e-9
    assert abs(sc.eval_vmap(knots, 0.5) - 0.6) < 1e-9
    assert abs(sc.eval_vmap(knots, 0.7) - 0.9) < 1e-9
    # interpolate within a segment
    assert abs(sc.eval_vmap(knots, 0.35) - 0.45) < 1e-9      # midpoint of (0.2,0.3)-(0.5,0.6)
    # extrapolate below/above using the end-segment slope
    assert abs(sc.eval_vmap(knots, 0.8) - (0.9 + 1.5 * 0.1)) < 1e-9   # last slope 1.5
    # 2-knot vmap is a plain affine (no regression vs the old single affine)
    assert abs(sc.eval_vmap([(0.3, 0.4), (0.5, 0.7)], 0.4) - 0.55) < 1e-9


def test_find_login_anchor_fracs():
    screen = {"elements": [
        {"id": "email_input", "center": [960, 360]},
        {"id": "password_input", "center": [960, 521]},
        {"id": "sign_in_button", "center": [960, 644]},
        {"id": "sign_up_link", "center": [755, 747]},
        {"id": "forgot_password_link", "center": [910, 747]},
        {"id": "developer_settings_link", "center": [1115, 747]},
    ]}
    af = sc.find_login_anchor_fracs(screen, 1080)
    assert af is not None
    assert abs(af["email"] - 360 / 1080) < 1e-6
    assert abs(af["signin"] - 644 / 1080) < 1e-6
    assert abs(af["footer"] - 747 / 1080) < 1e-6
    # no email/password → None
    assert sc.find_login_anchor_fracs({"elements": [{"id": "menu", "center": [1, 2]}]}, 1080) is None


def test_detect_footer_band_returns_none_on_blank():
    from PIL import Image
    blank = Image.new("RGB", (400, 400), (30, 60, 110))
    buf = io.BytesIO(); blank.save(buf, "PNG")
    assert sc.detect_footer_band(buf.getvalue()) is None


def test_derive_login_vmap_four_anchor_on_real_frame():
    """On the REAL login capture, the 4-anchor map anchors email/password/sign-in AND the footer row, so
    the footer links (monitor frac 0.6917) map onto their labels (~0.965 camera-frac) instead of the
    ~0.92 the 2-point affine extrapolates."""
    if not _REAL_LOGIN.exists():
        import pytest; pytest.skip("real login frame not present")
    from PIL import Image
    img = _REAL_LOGIN.read_bytes()
    w, h = Image.open(io.BytesIO(img)).size
    VH = settings.viewport_height
    anchors = {"email": 360 / VH, "password": 521 / VH, "signin": 644 / VH, "footer": 747 / VH}
    res = sc.derive_login_vmap(img, anchors)
    assert res is not None and res["kind"] == "vmap4"
    # sign-in stays essentially where the email/password affine puts it (never jumps)
    assert abs(res["signin_cam_frac"] - 0.787) < 0.03
    # footer anchored well below sign-in, near the true link row
    assert res["footer_cam_frac"] > res["signin_cam_frac"] + 0.10
    assert res["footer_cam_frac"] > 0.93
    # footer element maps onto its label row (was ~0.92 with the affine)
    fv = sc.eval_vmap(res["knots"], 747 / VH)
    assert abs(fv * h - 0.965 * h) < 0.03 * h


def test_derive_login_vmap_falls_back_to_affine_without_signin_footer():
    """No sign-in / footer anchors → 2-point affine (current behaviour), no vknots-shaped result."""
    if not _REAL_LOGIN.exists():
        import pytest; pytest.skip("real login frame not present")
    VH = settings.viewport_height
    res = sc.derive_login_vmap(_REAL_LOGIN.read_bytes(),
                               {"email": 360 / VH, "password": 521 / VH})
    assert res is not None and res["kind"] == "affine"
    assert len(res["knots"]) == 2


def test_real_robot_scale_four_anchor_fixes_footer(monkeypatch):
    """calibrate_vertical_from_login with sign-in+footer lands the footer tap on the link row while
    leaving email/password/sign-in on their centres; a base move clears it back to config."""
    if not _REAL_LOGIN.exists():
        import pytest; pytest.skip("real login frame not present")
    from PIL import Image
    from vision_agent.robot import real_robot as rr
    w, h = Image.open(io.BytesIO(_REAL_LOGIN.read_bytes())).size
    rr._calibration["scale_x"] = w / settings.viewport_width
    rr._calibration["scale_y"] = h / settings.viewport_height
    monkeypatch.setattr(settings, "camera_calib_ay", 1.0)
    monkeypatch.setattr(settings, "camera_calib_by", 0.0)
    monkeypatch.setattr(settings, "auto_tap_calibration", True)
    try:
        footer_before = rr._scale(910, 747)[1]
        info = rr.calibrate_vertical_from_login(str(_REAL_LOGIN), 360.0, 521.0,
                                                signin_center_y=644.0, footer_center_y=747.0)
        assert info["applied"] is True and info["kind"] == "vmap4"
        # footer tap now lands on the link row (~0.965*h), clearly below the affine extrapolation
        footer_after = rr._scale(910, 747)[1]
        assert footer_after > footer_before
        assert abs(footer_after - 0.965 * h) <= 0.03 * h
        # email/password/sign-in stay on their box/button centres
        assert abs(rr._scale(960, 360)[1] - 0.445 * h) <= 0.02 * h
        assert abs(rr._scale(960, 644)[1] - 0.787 * h) <= 0.03 * h
        assert "vknots" in rr._calibration
        # a base move drops the per-pose piecewise map (same cleanup navigate_to_kiosk performs) → config
        rr._calibration.pop("vknots", None)
        rr._calibration.pop("calib_ay", None)
        rr._calibration.pop("calib_by", None)
        assert "vknots" not in rr._calibration
        # back to the plain config affine (identity ay=1,by=0) — footer no longer anchored
        assert rr._scale(910, 747)[1] == round((747 / settings.viewport_height) * h)
    finally:
        rr._calibration.clear()


def test_consensus_fit_rejects_title_band():
    """The consensus form fit must NOT lock onto a title/helper band. With filled boxes [title, email,
    password], a title↔box pair yields an out-of-bounds affine, so only the true (email, password) pair
    survives — the failure that sent every crosshair floating high (config fallback) or onto the title."""
    E, P, S = 0.455, 0.603, 0.717
    # camera bands: title 0.176, email box 0.552, password box 0.751 (the frame-A pattern)
    fit = sc._consensus_form_fit([0.176, 0.552, 0.751], [0.176, 0.297, 0.552, 0.751], E, P, S)
    assert fit is not None
    ay, by, cE, cP, _ = fit
    assert abs(cE - 0.552) < 1e-6 and abs(cP - 0.751) < 1e-6      # the real boxes, not the title
    assert 0.70 <= ay <= 1.80


def test_derive_login_vmap_on_real_frame2():
    """On a real re-explored login capture, the consensus derive yields an in-bounds fit that places the
    email/password monitor fractions onto the detected boxes (no title contamination)."""
    if _REAL_LOGIN2 is None:
        import pytest; pytest.skip("no real login frame available")
    from PIL import Image
    img = _REAL_LOGIN2.read_bytes()
    res = sc.derive_login_vmap(img, {"email": 491 / 1080, "password": 651 / 1080,
                                      "signin": 774 / 1080, "footer": 907 / 1080})
    assert res is not None
    # email maps ABOVE password, both on-frame, sane slope
    ev = sc.eval_vmap(res["knots"], 491 / 1080)
    pv = sc.eval_vmap(res["knots"], 651 / 1080)
    assert 0.0 < ev < pv < 1.0


def test_scale_key_reuses_login_vmap(monkeypatch):
    """A keyboard key routes through the SAME per-pose vmap as form elements (footer knot ≈ keyboard
    bottom row), so the keyboard's vertical mapping is interpolated, not a raw extrapolation."""
    from vision_agent.robot import real_robot as rr
    monkeypatch.setattr(settings, "camera_calib_ax", 1.0)
    monkeypatch.setattr(settings, "camera_calib_bx", 0.0)
    rr._calibration.clear()
    rr._calibration["scale_x"] = 1.0     # cam == viewport for a clean check
    rr._calibration["scale_y"] = 1.0
    # a piecewise vmap whose footer knot (0.84) maps to camera 0.90
    rr._calibration["vknots"] = [(0.455, 0.50), (0.603, 0.66), (0.717, 0.78), (0.84, 0.90)]
    try:
        # a key at viewport-frac (0.5, 0.84) → vertical uses the vmap (0.90), horizontal identity (0.5)
        u, v = rr._scale_key(0.5, 0.84)
        assert abs(u - round(0.5 * settings.viewport_width)) <= 1
        assert abs(v - round(0.90 * settings.viewport_height)) <= 1
        # a key BELOW the footer knot extrapolates with the last segment (monotonic, on-frame)
        _, v2 = rr._scale_key(0.5, 0.864)
        assert v2 >= v
    finally:
        rr._calibration.clear()


def test_tap_image_point_no_double_scale(monkeypatch):
    """tap_image_point sends camera-space coords straight to /screen/click (NO _scale), while tap()
    applies _scale — the fix for Tier-3/inline vision double-scaling on the real backend."""
    from vision_agent.robot import real_robot as rr
    posted = {}
    monkeypatch.setattr(rr, "_ensure_localized", lambda: None)
    monkeypatch.setattr(rr, "_poll", lambda *a, **k: {"state": "ready"})
    monkeypatch.setattr(rr, "_check_click_completed", lambda *a, **k: None)
    monkeypatch.setattr(rr, "_post", lambda ep, body: posted.update({"ep": ep, "body": body}) or {"state": "moving"})
    monkeypatch.setattr(settings, "save_click_screenshots", False)
    rr._calibration.clear()
    rr._calibration["scale_x"] = 0.5      # camera is HALF the viewport → _scale would halve a point
    rr._calibration["scale_y"] = 0.5
    monkeypatch.setattr(settings, "camera_calib_ax", 1.0); monkeypatch.setattr(settings, "camera_calib_bx", 0.0)
    monkeypatch.setattr(settings, "camera_calib_ay", 1.0); monkeypatch.setattr(settings, "camera_calib_by", 0.0)
    try:
        rr.tap_image_point(300, 200)                    # already camera pixels → sent verbatim
        assert posted["body"]["points"] == [{"u": 300, "v": 200}]
        posted.clear()
        rr.tap(300, 200)                                # viewport pixels → _scale halves them
        u = posted["body"]["points"][0]["u"]; v = posted["body"]["points"][0]["v"]
        assert (u, v) == (150, 100)                     # 300*0.5, 200*0.5 — proves the paths differ
    finally:
        rr._calibration.clear()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
