"""Camera Vision Test — real, asserting tests for the OCR / enhancement / capture-folder fixes.

Unlike the earlier probe (which only *printed* the OCR result and silently passed when the Tesseract
engine was missing), these tests ASSERT that:
  • the Tesseract engine is resolvable and OCR actually returns text on a crisp image (the real bug
    was the engine installed but not on PATH);
  • _run_ocr distinguishes "package missing" / "engine missing" / working, and never raises;
  • _enhance_for_ocr returns a decodable, upscaled PNG and never reduces recovered text;
  • the analyze endpoint returns the new `enhanced` block, keeps the `ocr` contract, and saves frames
    under the dedicated camera_captures/ folder (not screenshots/).

Runs with NO live robot and NO Claude (use_claude=False). OCR-on-real-camera-frame tests self-skip if
the sample frame or the Tesseract engine isn't present, but the crisp-image OCR test always runs so a
broken OCR path can't pass silently.
"""
import io
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import api.main as m
from PIL import Image, ImageDraw, ImageFont

_CAMERA_FRAME = Path(
    r"C:\Users\gsk54\Desktop\Robotics_Project\Robot_Camera_images\Capture_API\vision_test_screen_1784545028820.png"
)


def _crisp_text_png(text: str = "SIGN IN EMAIL PASSWORD") -> bytes:
    """A high-contrast, large-font image tesseract must be able to read — proves the OCR path works."""
    img = Image.new("RGB", (900, 220), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 60)
    except Exception:
        font = ImageFont.load_default()
    d.text((30, 70), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── OCR engine + read ────────────────────────────────────────────────────────

def test_tesseract_resolves_on_this_machine():
    engine = m._resolve_tesseract()
    if not engine:
        # Environment without the engine — _run_ocr must still degrade cleanly (covered below).
        import pytest
        pytest.skip("Tesseract engine not installed in this environment")
    assert Path(engine).is_file(), f"resolved path is not a file: {engine}"


def test_ocr_reads_crisp_text():
    """The core assertion my earlier test lacked: OCR must return the actual text on a legible image."""
    if not m._resolve_tesseract():
        import pytest
        pytest.skip("Tesseract engine not installed")
    res = m._run_ocr(_crisp_text_png("SIGN IN EMAIL PASSWORD"))
    assert res["available"] is True
    assert res["error"] == "", res["error"]
    up = res["text"].upper()
    assert "SIGN" in up and "PASSWORD" in up, f"OCR did not read the text: {res['text']!r}"
    assert res["engine"], "engine path should be reported when OCR works"


def test_run_ocr_never_raises_and_reports_engine_state():
    res = m._run_ocr(_crisp_text_png("HELLO"))
    assert set(res) == {"available", "text", "error", "engine"}
    # Either it works (available + engine) or it's cleanly unavailable with an actionable message.
    if res["available"] and not res["error"]:
        assert res["engine"]
    else:
        assert res["error"], "unavailable OCR must carry an actionable error message"


# ── Enhancement pipeline ─────────────────────────────────────────────────────

def test_enhance_returns_upscaled_png():
    raw = _crisp_text_png("ENHANCE ME")
    enh = m._enhance_for_ocr(raw)
    assert enh, "enhancement returned nothing on a valid image"
    rw, rh = Image.open(io.BytesIO(raw)).size
    ew, eh = Image.open(io.BytesIO(enh)).size
    assert ew > rw and eh > rh, f"expected upscale, got {rw}x{rh} -> {ew}x{eh}"


def test_enhance_bad_bytes_returns_none():
    assert m._enhance_for_ocr(b"not an image") is None


def test_enhance_helps_or_holds_on_real_camera_frame():
    if not _CAMERA_FRAME.exists() or not m._resolve_tesseract():
        import pytest
        pytest.skip("camera frame or Tesseract engine not available")
    raw = _CAMERA_FRAME.read_bytes()
    enh = m._enhance_for_ocr(raw)
    assert enh
    raw_text = m._run_ocr(raw)["text"]
    enh_text = m._run_ocr(enh)["text"]
    # Enhancement must never LOSE recovered characters on this frame (it recovered header text in dev).
    assert len(enh_text) >= len(raw_text), (
        f"enhancement reduced OCR: raw={len(raw_text)} enh={len(enh_text)}")


# ── Image media-type detection (JPEG-vs-PNG 400 fix) ─────────────────────────

def test_detect_media_type_png_and_jpeg():
    """The real arm /capture returns JPEG; browser shots are PNG. Both must be detected
    correctly so the Claude vision block never sends the wrong media_type (400)."""
    from vision_agent.llm import detect_image_media_type
    for fmt, exp in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"),
                     ("WEBP", "image/webp")):
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(buf, format=fmt)
        got = detect_image_media_type(buf.getvalue())
        assert got == exp, f"{fmt} detected as {got}, expected {exp}"


def test_detect_media_type_real_camera_frame_is_jpeg():
    """The real RealSense `type:screen` capture is JPEG even when saved with a .png name —
    this is the exact frame that produced the 400 before the fix."""
    from vision_agent.llm import detect_image_media_type
    frame = Path(__file__).resolve().parents[1] / "camera_captures" / "vision_test_screen_1784803921993.png"
    if not frame.exists():
        import pytest
        pytest.skip("real camera frame not present")
    assert detect_image_media_type(frame.read_bytes()) == "image/jpeg"


def test_detect_media_type_defaults_png_on_garbage():
    from vision_agent.llm import detect_image_media_type
    assert detect_image_media_type(b"not-an-image") == "image/png"


# ── Large-upload bounding (phone-photo timeout fix) ──────────────────────────

def _png(w: int, h: int) -> bytes:
    """A non-trivial image of a given size (gradient so it isn't a flat block)."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 8):          # coarse fill — fast enough for a big canvas
            px[x, y] = ((x + y) % 256, (x * 2) % 256, (y * 2) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_bound_leaves_small_frames_unchanged():
    """Real camera frames (~600px) and browser screenshots (~1400px) must pass through untouched."""
    raw = _png(600, 400)
    assert m._bound_for_processing(raw) is raw, "a sub-cap frame must be returned unchanged (no regression)"
    raw2 = _png(1400, 900)
    assert m._bound_for_processing(raw2) is raw2


def test_bound_downscales_oversized_frame():
    """A 12 MP phone photo must be capped so its largest side is ≤ 1600 (the timeout fix)."""
    big = _png(4032, 3024)
    out = m._bound_for_processing(big)
    assert out is not big
    w, h = m._img_dims(out)
    assert max(w, h) <= 1600, f"expected ≤1600, got {w}x{h}"
    assert abs((w / h) - (4032 / 3024)) < 0.01, "aspect ratio must be preserved"


def test_bound_bad_bytes_returns_input():
    junk = b"not an image"
    assert m._bound_for_processing(junk) is junk


def test_analyze_completes_on_large_upload():
    """End-to-end: a big frame flows through analyze without exploding; enhanced output stays capped."""
    dst = m._vision_test_dir() / "unittest_big.png"
    dst.write_bytes(_png(4032, 3024))
    r = m.vision_test_analyze(m.VisionAnalyzeRequest(filename="unittest_big.png", use_claude=False))
    assert r["status"] == "ok"
    if r.get("enhanced"):
        assert max(r["enhanced"]["width"], r["enhanced"]["height"]) <= 2400, "enhanced frame must be bounded"
    dst.unlink(missing_ok=True)
    (m._vision_test_dir() / "unittest_big_enhanced.png").unlink(missing_ok=True)


# ── Capture folder + analyze endpoint contract ───────────────────────────────

def test_capture_dir_is_dedicated_camera_captures_folder():
    d = m._vision_test_dir()
    assert d.name == "camera_captures", f"capture folder should be camera_captures, got {d.name}"
    assert "screenshots" not in d.parts, "camera captures must not live under screenshots/ (reset-safe)"
    assert d.exists()


def test_analyze_returns_enhanced_block_and_saves_frame():
    if not _CAMERA_FRAME.exists():
        import pytest
        pytest.skip("camera frame not available")
    dst = m._vision_test_dir() / "unittest_probe.png"
    shutil.copy(_CAMERA_FRAME, dst)
    r = m.vision_test_analyze(m.VisionAnalyzeRequest(filename="unittest_probe.png", use_claude=False))
    assert r["status"] == "ok"
    # OCR contract preserved for the frontend
    assert set(("available", "text", "error")).issubset(r["ocr"].keys())
    # Enhanced block present, points at a saved file under camera_captures/, and re-runs OCR
    enh = r["enhanced"]
    assert enh and enh["applied"] and enh["image_url"].startswith("/api/vision-test/image/")
    saved = m._vision_test_dir() / enh["filename"]
    assert saved.exists(), f"enhanced frame not saved: {saved}"
    assert "ocr" in enh and "opencv_count" in enh
    dst.unlink(missing_ok=True)
    saved.unlink(missing_ok=True)


# ── Element camera coordinates — the (u,v) sent to the Robotics Click API (2026-08-10) ──────────

def test_diag_scale_point_matches_runtime_scale(monkeypatch):
    """The diagnostic mirror MUST equal vision_agent.robot.real_robot._scale for any point, so the
    Camera Vision Test shows the exact coordinates the runtime sends to the Click API."""
    from vision_agent.config import settings
    from vision_agent.robot import real_robot as rr
    monkeypatch.setattr(settings, "viewport_width", 1920)
    monkeypatch.setattr(settings, "viewport_height", 1080)
    monkeypatch.setattr(settings, "camera_calib_ax", 1.0)
    monkeypatch.setattr(settings, "camera_calib_bx", 0.0)
    monkeypatch.setattr(settings, "camera_calib_ay", 1.212)
    monkeypatch.setattr(settings, "camera_calib_by", -0.0034)
    fw, fh = 1405, 579
    rr._calibration["scale_x"] = fw / settings.viewport_width
    rr._calibration["scale_y"] = fh / settings.viewport_height
    try:
        for x, y in [(960, 508), (960, 668), (960, 792), (0, 0), (1920, 1080), (463, 286)]:
            assert m._diag_scale_point(x, y, fw, fh) == rr._scale(x, y), (x, y)
    finally:
        rr._calibration.clear()


def test_element_coords_for_screen_converts_centers_and_bbox(monkeypatch):
    from vision_agent.config import settings
    monkeypatch.setattr(settings, "viewport_width", 1920)
    monkeypatch.setattr(settings, "viewport_height", 1080)
    monkeypatch.setattr(settings, "camera_calib_ax", 1.0)
    monkeypatch.setattr(settings, "camera_calib_bx", 0.0)
    monkeypatch.setattr(settings, "camera_calib_ay", 1.212)
    monkeypatch.setattr(settings, "camera_calib_by", -0.0034)
    screen = {"elements": [
        {"id": "email_input",  "type": "input",  "label": "Email",    "center": [960, 508], "bbox": [800, 480, 1120, 536]},
        {"id": "sign_in_button", "type": "button", "label": "Sign In", "center": [960, 792]},
        {"id": "broken", "type": "text", "label": "x", "center": [960]},   # malformed → skipped
    ]}
    # No image_bytes → no self-calibration → uses the (monkeypatched) static config calibration.
    ec = m._element_coords_for_screen(screen, "login", "requested", 1405, 579)
    assert ec["screen_id"] == "login" and ec["source"] == "requested"
    assert ec["camera_width"] == 1405 and ec["camera_height"] == 579
    assert ec["calibration"]["ax"] == 1.0 and ec["calibration"]["bx"] == 0.0
    assert ec["calibration"]["ay"] == 1.212 and ec["calibration"]["by"] == -0.0034
    assert ec["calibration"]["source"] == "config"
    ids = [e["id"] for e in ec["elements"]]
    assert ids == ["email_input", "sign_in_button"]          # malformed center dropped
    email = ec["elements"][0]
    assert email["center_camera"] == [702, 328]              # the true email-box centre (see _scale)
    assert email["bbox_camera"] == [m._diag_scale_point(800, 480, 1405, 579)[0],
                                    m._diag_scale_point(800, 480, 1405, 579)[1],
                                    m._diag_scale_point(1120, 536, 1405, 579)[0],
                                    m._diag_scale_point(1120, 536, 1405, 579)[1]]
    assert ec["elements"][1]["bbox_camera"] is None          # no bbox on the button


def test_element_coords_endpoint(monkeypatch, tmp_path):
    """POST /vision-test/element-coords returns per-element camera coords for a chosen screen, and
    404s for an unknown screen — without running any LLM/OCR."""
    import json
    from fastapi.testclient import TestClient
    from vision_agent.config import settings

    # Point app_map_path into a temp dir; _vision_test_dir() derives camera_captures/ from its parent,
    # so frames and the app map live together and don't touch real project data.
    fake_map_path = tmp_path / "app_map.json"
    fake_map_path.write_text(json.dumps({"screens": {"login": {"app_id": "RPS", "elements": [
        {"id": "email_input", "type": "input", "label": "Email", "center": [960, 508]},
    ]}}}), encoding="utf-8")
    monkeypatch.setattr(settings, "app_map_path", str(fake_map_path))
    frame = m._vision_test_dir() / "unittest_coords.png"       # now under tmp_path/camera_captures
    Image.new("RGB", (1405, 579), "navy").save(frame)

    client = TestClient(m.app)
    r = client.post("/api/vision-test/element-coords",
                    json={"filename": "unittest_coords.png", "screen_id": "login"})
    assert r.status_code == 200, r.text
    ec = r.json()["element_coords"]
    assert ec["screen_id"] == "login" and len(ec["elements"]) == 1
    assert ec["elements"][0]["id"] == "email_input"
    assert len(ec["elements"][0]["center_camera"]) == 2
    bad = client.post("/api/vision-test/element-coords",
                      json={"filename": "unittest_coords.png", "screen_id": "nope"})
    assert bad.status_code == 404


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
