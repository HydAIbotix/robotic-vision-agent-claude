#!/usr/bin/env python3
"""
Calibrate the camera tap mapping (vertical AFFINE correction) for the REAL robot.

WHY: the arm `/capture type=screen` frame is a VERTICAL CROP of the display (it spans only the
AprilTag region — full width, partial height — NOT a faithful full-screen deskew). So an app_map
element's camera y-position is a linear-but-DIFFERENT function of its monitor y-position, and a plain
viewport→camera scale taps too high (observed: the email tap landed on the "Email" LABEL, ~67px above
the input-box centre). The mapping is affine and STABLE for a fixed arm/camera/screen pose:

    camera_frac_y = CAMERA_CALIB_AY * monitor_frac_y + CAMERA_CALIB_BY

This tool re-derives CAMERA_CALIB_AY / _BY from a live LOGIN capture by comparing the TRUE camera
centres of the email + password input boxes to the app_map's email/password monitor centres.

USAGE (with ROBOT_BACKEND=real, the arm positioned at the kiosk LOGIN screen):
    python calibrate_tap.py                                   # capture + AUTO-detect (a HINT only)
    python calibrate_tap.py --email-y 328 --password-y 432    # MANUAL: box-centre Y read off the frame
    python calibrate_tap.py --email-y 328 --password-y 432 --write   # also write CAMERA_CALIB_* to .env

The captured frame is saved to screenshots/calibrate_login.png — open it (or use the Camera Vision Test
page) and read the vertical CENTRE pixel of the Email box and the Password box, then pass them with
--email-y/--password-y (the RELIABLE path). Auto-detection is BEST-EFFORT only: real camera frames are
glary/vignetted, so the dimmer lower box is often missed — always sanity-check it.

Re-run after ANY change to the arm/camera pose or the kiosk screen. Horizontal is typically faithful
(AX=1, BX=0); this tool calibrates the VERTICAL axis (the one that drifts with the crop).
"""
import sys
import io
import json
import re
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from vision_agent.config import settings
from vision_agent import robot

WRITE = "--write" in sys.argv


def _arg(name: str):
    """Read --name VALUE from argv (int), or None."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                return None
    return None


def _detect_input_boxes(image_path: str) -> list[tuple[int, int]]:
    """Return [(center_y, height), …] for the bright horizontal bands (input boxes) in the central
    column of the camera frame, top→bottom. Input fields render light on the dark blue auth card, so a
    luminance row-profile over the form column isolates them robustly (no LLM)."""
    from PIL import Image
    import numpy as np
    a = np.array(Image.open(image_path).convert("RGB")).astype(float)
    h, w, _ = a.shape
    lum = a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114
    x0, x1 = int(w * 0.34), int(w * 0.66)          # central form column
    y_top = int(h * 0.18)                           # skip the title bar (white text on black leaks through)
    sub = lum[:, x0:x1]
    # An input box is a LARGE light region that FILLS the column width; title/label text and glare are
    # sparse (thin strokes). Real camera frames are glary/vignetted (lower boxes dimmer), so use a LOW
    # adaptive per-pixel threshold + a high FILL fraction to isolate the solid boxes and reject text.
    bg = float(np.median(sub))                      # ≈ the dark auth-card background
    pix_thr = bg + max(22.0, 0.20 * (float(sub.max()) - bg))
    frac = (sub > pix_thr).mean(axis=1)             # fraction of the column bright, per row
    rows = frac > 0.55
    rows[:y_top] = False
    bands, start, inb = [], 0, False
    for y in range(h):
        if rows[y] and not inb:
            start, inb = y, True
        elif not rows[y] and inb:
            if y - start >= max(10, h // 40):       # a real box is a thick band
                bands.append((start, y - 1))
            inb = False
    if inb and h - start >= max(10, h // 40):
        bands.append((start, h - 1))
    # Keep the two TALLEST bands (the email + password input boxes; the Sign In button is dimmer/shorter),
    # then return them TOP→BOTTOM so [0]=email, [1]=password.
    bands.sort(key=lambda b: (b[1] - b[0]), reverse=True)
    bands = sorted(bands[:2], key=lambda b: b[0])
    return [((b[0] + b[1]) // 2, b[1] - b[0]) for b in bands]


def _login_centers(app_map: dict) -> tuple[dict, int, int]:
    """Find the login screen and return (screen, email_monitor_y, password_monitor_y)."""
    for sid, sc in (app_map.get("screens") or {}).items():
        els = {e.get("id", ""): e for e in (sc.get("elements") or [])}
        email = next((e for i, e in els.items() if "email" in i.lower()), None)
        pwd   = next((e for i, e in els.items() if "pass" in i.lower()), None)
        if email and pwd and email.get("center") and pwd.get("center"):
            return sc, int(email["center"][1]), int(pwd["center"][1])
    return {}, 0, 0


def main() -> int:
    if settings.robot_backend != "real":
        print(f"  ROBOT_BACKEND is '{settings.robot_backend}', not 'real' — calibration needs the arm camera.")
        return 2
    app_map = json.loads(Path(settings.app_map_path).read_text(encoding="utf-8"))
    sc, email_my, pwd_my = _login_centers(app_map)
    if not sc:
        print("  Could not find a login screen (email + password) in app_map.json — explore first.")
        return 2

    shot = str(Path(settings.screenshots_dir) / "calibrate_login.png")
    Path(settings.screenshots_dir).mkdir(parents=True, exist_ok=True)
    cap = robot.capture_screen(shot)
    cam_w, cam_h = cap.get("width"), cap.get("height")
    print(f"  Captured login frame {cam_w}×{cam_h} → {cap['image_path']}")

    # AUTO-detect is a HINT (glary frames often miss the dim lower box); manual --email-y/--password-y wins.
    hint = _detect_input_boxes(cap["image_path"])
    print(f"  [hint] auto-detected box centres y = {[b[0] for b in hint]} (verify against the saved frame!)")

    email_cy, pwd_cy = _arg("--email-y"), _arg("--password-y")
    if email_cy is None or pwd_cy is None:
        if len(hint) >= 2:
            email_cy, pwd_cy = hint[0][0], hint[1][0]
            print(f"  Using the AUTO hint (email_y={email_cy}, password_y={pwd_cy}) — pass --email-y/"
                  f"--password-y to override with values you read off {cap['image_path']}.")
        else:
            print(f"  Auto-detect found <2 boxes. Open {cap['image_path']}, read the Email & Password box "
                  f"CENTRE y, and re-run with --email-y <N> --password-y <N>.")
            return 2

    # Affine fit in FRACTION space: camera_frac_y = ay * monitor_frac_y + by
    vh = settings.viewport_height
    mfa, mfb = email_my / vh, pwd_my / vh            # monitor fracs
    cfa, cfb = email_cy / cam_h, pwd_cy / cam_h      # camera fracs
    if abs(mfa - mfb) < 1e-6:
        print("  Email and password have the same monitor y — cannot fit. Aborting.")
        return 2
    ay = (cfa - cfb) / (mfa - mfb)
    by = cfa - ay * mfa

    print("\n  ── Derived vertical calibration ─────────────────────────────")
    print(f"    email:    monitor_y={email_my} (frac {mfa:.3f})  ->  camera_y={email_cy} (frac {cfa:.3f})")
    print(f"    password: monitor_y={pwd_my} (frac {mfb:.3f})  ->  camera_y={pwd_cy} (frac {cfb:.3f})")
    print(f"\n    CAMERA_CALIB_AX=1.0")
    print(f"    CAMERA_CALIB_BX=0.0")
    print(f"    CAMERA_CALIB_AY={ay:.4f}")
    print(f"    CAMERA_CALIB_BY={by:.4f}")
    print("  ─────────────────────────────────────────────────────────────")

    if WRITE:
        env = Path(".env")
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        def _set(k, v):
            nonlocal text
            if re.search(rf"(?m)^{k}=.*$", text):
                text = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", text)
            else:
                text += f"\n{k}={v}"
        _set("CAMERA_CALIB_AX", "1.0"); _set("CAMERA_CALIB_BX", "0.0")
        _set("CAMERA_CALIB_AY", f"{ay:.4f}"); _set("CAMERA_CALIB_BY", f"{by:.4f}")
        env.write_text(text, encoding="utf-8")
        print("  ✓ Wrote CAMERA_CALIB_* into .env. RESTART the backend to load it.")
    else:
        print("  (Run with --write to save these into .env, then RESTART the backend.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
