"""Runtime self-calibration of the vertical viewport→camera mapping from a login screen.

WHY THIS EXISTS
---------------
app_map element coordinates are learned in the Playwright exploration viewport; the real robot taps
by scaling them to the arm's rectified `/capture type=screen` frame. That scale is exact ONLY when the
rectified frame is a faithful, full-screen deskew of the kiosk display. In practice the AprilTag
rectification returns a VERTICAL CROP whose extent VARIES per arm/camera pose — two captures of the
same login screen have come back at wildly different aspect ratios (2.42:1 vs 1.06:1). A single
hardcoded per-axis affine (`CAMERA_CALIB_AY/BY`) fit to one pose therefore mis-places taps at the next
pose (observed: the email tap landed ~2 cm below the field, in the gap above password).

The robust fix is to MEASURE the vertical mapping from the frame itself: the login screen has two large,
high-contrast input boxes (email, password) at KNOWN app_map fractions. Detecting their centres in the
camera frame yields two (monitor_frac → camera_frac) correspondences — enough to solve the per-axis
vertical affine `camera_frac_y = ay * monitor_frac_y + by` for THIS pose. Horizontal stays faithful
(the form is centred and the frame spans the full screen width), so only the vertical axis is derived.

SAFETY / NO-REGRESSION
----------------------
- Detection is a luminance row-profile over the central column — pure OpenCV/PIL, no LLM, no network.
- A fit is accepted ONLY when two input boxes are found with sane spacing AND the derived (ay, by)
  fall inside conservative bounds. Otherwise `derive_login_vertical` returns None and the caller keeps
  its configured calibration — so a mis-detection can never make taps worse than today's behaviour.
- Nothing here runs for playwright/demo; it is invoked only on the real backend's login frame.
"""
from __future__ import annotations

import io

# Conservative bounds on the derived vertical affine. A real rectified frame spans between ~60% and
# 100% of the screen height (ay = 1/span ∈ [1.0, 1.7]) and crops from the top by 0–30% (by derived).
# Anything outside these is a detection error → reject and fall back to the configured calibration.
_AY_MIN, _AY_MAX = 0.70, 1.80
_BY_MIN, _BY_MAX = -0.45, 0.20


def detect_form_field_fracs(
    image_bytes: bytes,
    col: tuple[float, float] = (0.34, 0.66),
    top_skip: float = 0.15,
    min_fill: float = 0.55,
    min_h_frac: float = 0.02,
) -> list[float]:
    """Return the vertical CENTRE fraction (0..1, top→bottom) of each bright, column-filling band in
    the central column of the frame — the login form's input boxes render light on the dark auth card,
    so a luminance row-profile isolates them. The title bar is skipped via `top_skip`. Returns [] on
    any failure (never raises)."""
    try:
        from PIL import Image
        import numpy as np
        a = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB")).astype(float)
        h, w, _ = a.shape
        if h < 20 or w < 20:
            return []
        lum = a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114
        x0, x1 = int(w * col[0]), int(w * col[1])
        sub = lum[:, x0:x1]
        bg = float(np.median(sub))
        thr = bg + max(22.0, 0.20 * (float(sub.max()) - bg))    # adaptive: light box vs dark card
        rows = (sub > thr).mean(axis=1) > min_fill              # a box FILLS the column; text is sparse
        rows[: int(h * top_skip)] = False                      # ignore the title band
        min_h = max(8, int(h * min_h_frac))
        bands: list[tuple[int, int]] = []
        start: int | None = None
        for y in range(h):
            if rows[y] and start is None:
                start = y
            elif not rows[y] and start is not None:
                if y - start >= min_h:
                    bands.append((start, y - 1))
                start = None
        if start is not None and h - start >= min_h:
            bands.append((start, h - 1))
        return [((b[0] + b[1]) / 2.0) / h for b in bands]
    except Exception:
        return []


def fit_vertical_affine(m1: float, m2: float, c1: float, c2: float) -> tuple[float, float] | None:
    """Solve camera_frac = ay*monitor_frac + by from two correspondences; None if degenerate/out of
    bounds."""
    if abs(m1 - m2) < 1e-6:
        return None
    ay = (c1 - c2) / (m1 - m2)
    by = c1 - ay * m1
    if not (_AY_MIN <= ay <= _AY_MAX and _BY_MIN <= by <= _BY_MAX):
        return None
    return ay, by


def derive_login_vertical(
    image_bytes: bytes,
    email_monitor_frac: float,
    password_monitor_frac: float,
) -> dict | None:
    """From a login camera frame + the app_map email/password monitor fractions, derive the per-pose
    vertical affine (ay, by). Returns {ay, by, email_cam_frac, password_cam_frac, bands} or None when
    the frame doesn't yield a confident fit (caller then keeps its configured calibration)."""
    fr = detect_form_field_fracs(image_bytes)
    if len(fr) < 2:
        return None
    c_email, c_pass = fr[0], fr[1]                 # first two input boxes, top→bottom = email, password
    if c_pass - c_email < 0.05:                    # implausibly close → not the two form fields
        return None
    fit = fit_vertical_affine(email_monitor_frac, password_monitor_frac, c_email, c_pass)
    if fit is None:
        return None
    ay, by = fit
    return {"ay": ay, "by": by, "email_cam_frac": c_email, "password_cam_frac": c_pass, "bands": fr}


def find_login_anchors(screen: dict) -> tuple[float, float] | None:
    """Return (email_monitor_frac_y, password_monitor_frac_y) for a login-type app_map screen using the
    element CENTERS, or None if the screen has no email + password inputs. Fractions need the caller's
    viewport height; this returns the raw centre Y pixels instead — see resolve_login_anchor_fracs."""
    els = screen.get("elements") or []
    email = next((e for e in els if "email" in (e.get("id", "").lower()) and e.get("center")), None)
    pwd = next((e for e in els if "pass" in (e.get("id", "").lower()) and e.get("center")), None)
    if not email or not pwd:
        return None
    try:
        return float(email["center"][1]), float(pwd["center"][1])
    except Exception:
        return None
