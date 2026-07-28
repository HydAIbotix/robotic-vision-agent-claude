"""Template-matching screen identification — asserting tests.

Covers the ported Tier-1 screen-determination logic that replaces the legacy aHash:
  • template_match_score  — high on identical frames, lower on different ones
  • resolve_template_ref  — filename→screen_id mapping (Login_page.png → 'login')
  • build_references      — override dir wins over app_map reference_screenshot
  • identify_screen       — match / mismatch / inconclusive / no_reference verdicts
  • _match_by_template    — pipeline wrapper downgrades a mismatch to inconclusive (no new hard fails)

Deterministic: uses the clean reference templates in Image_processor/uploads as both the "frame"
and the templates, so no live robot or Claude is needed. Robot-dependent assertions self-skip.
"""
import io
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from PIL import Image
from vision_agent.vision import template_match as tm

_UPLOADS = Path(r"C:\Users\gsk54\Desktop\Robotics_Project\Image_processor\uploads")
_LOGIN   = _UPLOADS / "Login_page.png"
_PRODS   = _UPLOADS / "Products_page.png"


def _has_uploads() -> bool:
    return _LOGIN.exists() and _PRODS.exists()


def _png_bytes(color=(30, 60, 120), size=(400, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# ── template_match_score ─────────────────────────────────────────────────────

def test_score_identical_is_near_one():
    page = tm._to_bgr_from_bytes(_png_bytes((10, 20, 30)))
    # add structure so correlation is well-defined (a flat image has ~0 variance)
    import numpy as np, cv2
    page = cv2.rectangle(page.copy(), (50, 50), (200, 150), (240, 240, 240), -1)
    assert tm.template_match_score(page, page) > 0.99


def test_score_different_is_lower_than_identical():
    import cv2
    a = tm._to_bgr_from_bytes(_png_bytes((10, 20, 30)))
    a = cv2.rectangle(a.copy(), (40, 40), (180, 120), (240, 240, 240), -1)
    b = tm._to_bgr_from_bytes(_png_bytes((200, 200, 200)))
    b = cv2.rectangle(b.copy(), (10, 160), (90, 260), (0, 0, 0), -1)
    assert tm.template_match_score(a, b) < tm.template_match_score(a, a)


def test_score_handles_different_sizes():
    # template is resized to page dims internally — must not raise on mismatched sizes
    page = tm._to_bgr_from_bytes(_png_bytes(size=(400, 300)))
    tmpl = tm._to_bgr_from_bytes(_png_bytes(size=(640, 480)))
    s = tm.template_match_score(page, tmpl)
    assert -1.0 <= s <= 1.0


# ── resolve_template_ref / build_references ──────────────────────────────────

def test_resolve_template_ref_maps_filename_to_screen():
    if not _has_uploads():
        import pytest; pytest.skip("uploads templates not present")
    assert tm.resolve_template_ref("login", str(_UPLOADS)) == str(_LOGIN)
    assert tm.resolve_template_ref("products", str(_UPLOADS)) == str(_PRODS)
    # a screen with no matching file → None
    assert tm.resolve_template_ref("payment", str(_UPLOADS)) is None


def test_resolve_template_ref_no_dir_returns_none():
    assert tm.resolve_template_ref("login", "") is None
    assert tm.resolve_template_ref("login", r"C:\no\such\dir") is None


def test_resolve_prefers_exact_stem(tmp_path):
    # Both an exact <screen_id>.png and a looser name exist → exact wins deterministically.
    (tmp_path / "login.png").write_bytes(_png_bytes())
    (tmp_path / "Login_page.png").write_bytes(_png_bytes())
    assert tm.resolve_template_ref("login", str(tmp_path)) == str(tmp_path / "login.png")


def test_resolve_no_prefix_collision(tmp_path):
    # "product_detail.png" must NOT be picked for screen "products" (one name prefixing another).
    (tmp_path / "product_detail.png").write_bytes(_png_bytes())
    assert tm.resolve_template_ref("products", str(tmp_path)) is None
    # add the real one → it is chosen, product_detail is never confused for it
    (tmp_path / "products.png").write_bytes(_png_bytes())
    assert tm.resolve_template_ref("products", str(tmp_path)) == str(tmp_path / "products.png")


def test_resolve_token_fallback_still_works(tmp_path):
    # No exact stem, but the screen_id is a filename token → still resolves (Login_page → login).
    (tmp_path / "Login_page.png").write_bytes(_png_bytes())
    assert tm.resolve_template_ref("login", str(tmp_path)) == str(tmp_path / "Login_page.png")


def test_build_references_override_beats_app_map(tmp_path):
    if not _has_uploads():
        import pytest; pytest.skip("uploads templates not present")
    # app_map points login at some other file; the override dir must win.
    other = tmp_path / "stale.png"
    other.write_bytes(_png_bytes())
    app_map = {"screens": {
        "login":   {"reference_screenshot": str(other)},
        "unknown": {"reference_screenshot": str(tmp_path / "missing.png")},  # missing → dropped
    }}
    refs = tm.build_references(app_map, str(_UPLOADS))
    assert refs["login"] == str(_LOGIN)     # override won
    assert "unknown" not in refs            # missing reference dropped


# ── identify_screen verdicts ─────────────────────────────────────────────────

def test_identify_match_when_frame_is_expected_screen():
    if not _has_uploads():
        import pytest; pytest.skip("uploads templates not present")
    refs = {"login": str(_LOGIN), "products": str(_PRODS)}
    frame = _LOGIN.read_bytes()
    res = tm.identify_screen(frame, refs, expected_screen="login", threshold=0.55, margin=0.06)
    assert res["success"] is True
    assert res["actual_screen"] == "login"
    assert res["ranking"][0]["screen_id"] == "login"


def test_identify_mismatch_when_frame_is_a_different_screen():
    if not _has_uploads():
        import pytest; pytest.skip("uploads templates not present")
    refs = {"login": str(_LOGIN), "products": str(_PRODS)}
    frame = _LOGIN.read_bytes()          # a login frame …
    res = tm.identify_screen(frame, refs, expected_screen="products", threshold=0.55, margin=0.06)
    assert res["success"] is False        # … is NOT products
    assert res["actual_screen"] == "login"


def test_identify_no_reference_when_empty():
    res = tm.identify_screen(_png_bytes(), {}, expected_screen="login")
    assert res["success"] is None
    assert res["method"] == "template_no_reference"


def test_identify_pure_mode_picks_top_screen():
    if not _has_uploads():
        import pytest; pytest.skip("uploads templates not present")
    refs = {"login": str(_LOGIN), "products": str(_PRODS)}
    res = tm.identify_screen(_PRODS.read_bytes(), refs, expected_screen="", threshold=0.55, margin=0.06)
    assert res["actual_screen"] == "products"


# ── pipeline wrapper: mismatch is downgraded to inconclusive (no new hard fails) ──

def test_pipeline_wrapper_downgrades_mismatch(tmp_path, monkeypatch):
    if not _has_uploads():
        import pytest; pytest.skip("uploads templates not present")
    from vision_agent.config import settings
    from vision_agent.nodes import validate_pipeline as vp
    monkeypatch.setattr(settings, "template_ref_dir", str(_UPLOADS))
    monkeypatch.setattr(settings, "use_template_screen_match", True)

    frame = tmp_path / "login_frame.png"
    frame.write_bytes(_LOGIN.read_bytes())
    app_map = {"screens": {"login": {}, "products": {}}}

    # correct screen → confident 0-LLM match
    ok = vp._match_by_template(str(frame), app_map, "login")
    assert ok["success"] is True and ok["method"] == "template_match"

    # wrong screen → NOT a hard False; downgraded to inconclusive so Claude decides
    wrong = vp._match_by_template(str(frame), app_map, "products")
    assert wrong["success"] is None
    assert wrong["method"] == "template_inconclusive"


def test_pipeline_no_reference_falls_back(tmp_path, monkeypatch):
    from vision_agent.config import settings
    from vision_agent.nodes import validate_pipeline as vp
    monkeypatch.setattr(settings, "template_ref_dir", "")
    frame = tmp_path / "f.png"
    frame.write_bytes(_png_bytes())
    # expected screen has no reference anywhere → wrapper reports no_reference (→ aHash fallback)
    r = vp._match_by_template(str(frame), {"screens": {"login": {}}}, "login")
    assert r["method"] == "template_no_reference"
    assert r["success"] is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
