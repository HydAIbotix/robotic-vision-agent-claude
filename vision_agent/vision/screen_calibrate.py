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


# ─────────────────────────────────────────────────────────────────────────────
# 4-anchor piecewise vertical map (2026-08-11)
# ─────────────────────────────────────────────────────────────────────────────
# WHY: the 2-point (email, password) affine above is accurate through the Sign In button, but the
# FOOTER LINKS (sign-up / forgot-password / developer-settings) sit BELOW it and do NOT lie on the same
# line — the rectified /capture frame is compressed toward the middle and the footer text renders lower
# than any smooth extrapolation from the mid-screen form fields predicts (empirically ~0.955 camera-frac
# where the affine puts it at ~0.92). Rather than extrapolate, we ANCHOR the bottom of the screen too:
# detect the Sign In button band and the footer text row directly, and map the whole vertical axis with a
# monotonic PIECEWISE-LINEAR function through four detected anchors (email, password, sign-in, footer).
# email/password/sign-in stay on their detected centres (so the working elements are unchanged) and the
# footer finally lands on its links. This is used ONLY when all four anchors are confidently found and
# the sign-in band agrees with the email/password affine (so sign-in never jumps); otherwise the caller
# keeps the existing 2-point affine — no regression for any pose where the full set can't be measured.

def _affine_from(m1: float, m2: float, c1: float, c2: float) -> tuple[float, float]:
    ay = (c1 - c2) / (m1 - m2)
    return ay, c1 - ay * m1


def eval_vmap(knots: list[tuple[float, float]], m: float) -> float:
    """Piecewise-linear evaluate camera_frac at monitor_frac `m` over sorted (monitor,camera) knots.
    Interpolates between bracketing knots; extrapolates past the ends with the end segment's slope. A
    single knot returns its camera value; two knots reduce to a plain affine (so a 2-knot vmap is
    byte-identical to the old affine)."""
    ks = sorted(knots)
    if not ks:
        return m
    if len(ks) == 1:
        return ks[0][1]
    if m <= ks[0][0]:
        (m0, c0), (m1, c1) = ks[0], ks[1]
    elif m >= ks[-1][0]:
        (m0, c0), (m1, c1) = ks[-2], ks[-1]
    else:
        (m0, c0), (m1, c1) = ks[0], ks[1]
        for i in range(len(ks) - 1):
            if ks[i][0] <= m <= ks[i + 1][0]:
                (m0, c0), (m1, c1) = ks[i], ks[i + 1]
                break
    if abs(m1 - m0) < 1e-9:
        return c0
    return c0 + (c1 - c0) / (m1 - m0) * (m - m0)


def detect_login_form_bands(
    image_bytes: bytes,
    col: tuple[float, float] = (0.30, 0.70),
    k: float = 0.16,
    top_skip: float = 0.14,
    min_h_frac: float = 0.025,
) -> list[float]:
    """Return centre fractions of the login form's filled bands (email box, password box, AND the dimmer
    Sign In button) using a LOCAL-contrast threshold. The Sign In button often sits near the card's
    global median brightness, so the global-threshold detect_form_field_fracs misses it; here the
    threshold is a fraction `k` of the way from the 25th to the 92nd percentile of the central-column
    row-luminance profile, which catches the button as a plateau above the dark gaps. Returns [] on
    failure (never raises)."""
    try:
        from PIL import Image
        import numpy as np
        a = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB")).astype(float)
        h, w, _ = a.shape
        if h < 20 or w < 20:
            return []
        lum = a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114
        x0, x1 = int(w * col[0]), int(w * col[1])
        rmed = np.median(lum[:, x0:x1], axis=1)
        sm = np.convolve(rmed, np.ones(5) / 5, mode="same")
        yt = int(h * top_skip)
        prof = sm[yt:]
        if prof.size == 0:
            return []
        lo = float(np.percentile(prof, 25))
        hi = float(np.percentile(prof, 92))
        thr = lo + k * (hi - lo)
        on = sm > thr
        on[:yt] = False
        bands: list[float] = []
        start: int | None = None
        mh = max(6, int(h * min_h_frac))
        for y in range(h):
            if on[y] and start is None:
                start = y
            elif not on[y] and start is not None:
                if y - start >= mh:
                    bands.append((start + y - 1) / 2.0 / h)
                start = None
        if start is not None and h - start >= mh:
            bands.append((start + h - 1) / 2.0 / h)
        return bands
    except Exception:
        return []


def _match_form_anchors(cands: list[float], e_mon: float, p_mon: float,
                        s_mon: float) -> tuple[float, float, float] | None:
    """Pick the (email, password, sign-in) triplet from detected band centres whose SPACING RATIO best
    matches the app_map's known (email→password):(password→sign-in) ratio. Robust to a stray title band
    or noise — the correct triplet's spacing ratio is distinctive. Returns (cE,cP,cS) or None."""
    import itertools
    if len(cands) < 3:
        return None
    target = (p_mon - e_mon) / (s_mon - p_mon)
    best = None
    for tri in itertools.combinations(cands, 3):
        cE, cP, cS = sorted(tri)
        if cP - cE < 0.04 or cS - cP < 0.03:
            continue
        r = (cP - cE) / (cS - cP)
        err = abs(r - target) / target
        if err < 0.35 and (best is None or err < best[0]):
            best = (err, (cE, cP, cS))
    return best[1] if best else None


def detect_footer_band(
    image_bytes: bytes,
    lo: float = 0.83,
    hi: float = 0.995,
    col: tuple[float, float] = (0.18, 0.82),
    min_energy: float = 12.0,
) -> float | None:
    """Return the camera-frac of the footer link row (sign-up / forgot-password / developer-settings),
    the brightest text row in the bottom band. 'Text energy' = p90-minus-median luminance across a wide
    column (the footer text is faint but locally brighter than the dark strip around it). Gated by
    `min_energy` so a frame that crops the footer out returns None (caller keeps its calibration)."""
    try:
        from PIL import Image
        import numpy as np
        a = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB")).astype(float)
        h, w, _ = a.shape
        if h < 20 or w < 20:
            return None
        lum = a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114
        sub = lum[:, int(w * col[0]):int(w * col[1])]
        y0, y1 = int(h * lo), int(h * hi)
        if y1 - y0 < 3:
            return None
        best = None
        for y in range(y0, y1):
            e = float(np.percentile(sub[y], 90) - np.median(sub[y]))
            if best is None or e > best[1]:
                best = (y, e)
        if best is None or best[1] < min_energy:
            return None
        return best[0] / h
    except Exception:
        return None


def find_login_anchor_fracs(screen: dict, viewport_height: float) -> dict | None:
    """Return the monitor fractions {email, password, signin, footer} for a login app_map screen (values
    are None when an element is absent). Requires at least email + password, else None. `signin` is the
    Sign In button; `footer` is the median Y of the footer links."""
    els = screen.get("elements") or []

    def cy(pred) -> float | None:
        e = next((e for e in els if pred(e.get("id", "").lower()) and e.get("center")), None)
        try:
            return float(e["center"][1]) / viewport_height if e else None
        except Exception:
            return None

    email = cy(lambda i: "email" in i)
    pwd = cy(lambda i: "pass" in i)
    if email is None or pwd is None:
        return None
    signin = cy(lambda i: "sign" in i and "in" in i and "button" in i)
    footers: list[float] = []
    for e in els:
        i = e.get("id", "").lower()
        if e.get("center") and (("sign" in i and "up" in i) or "forgot" in i
                                or "developer" in i or "register" in i or "create_account" in i):
            try:
                footers.append(float(e["center"][1]) / viewport_height)
            except Exception:
                pass
    footer = sorted(footers)[len(footers) // 2] if footers else None
    return {"email": email, "password": pwd, "signin": signin, "footer": footer}


def _consensus_form_fit(boxes: list[float], all_bands: list[float], e_mon: float, p_mon: float,
                        s_mon: float | None) -> tuple[float, float, float, float, float | None] | None:
    """Robustly assign the email/password INPUT BOXES by CONSENSUS instead of "first two bands".

    The (email, password) pair is drawn ONLY from FILLED boxes (`boxes`, from detect_form_field_fracs),
    which reject sparse title/helper/label TEXT. For each ordered box pair it fits the affine and keeps it
    ONLY if ay/by are in bounds — which alone rejects a title↔box pair (its gap is wrong → the affine
    blows past the bounds). Among the survivors it prefers the one whose predicted sign-in lands on a
    detected band (corroboration), then the pair with the most plausible email→password gap. `all_bands`
    supplies the sign-in corroboration. Returns (ay, by, cE, cP, cS_or_None) or None."""
    if len(boxes) < 2:
        return None
    exp_gap = p_mon - e_mon
    best = None   # (corroborated?0/1, score, ay, by, cE, cP, cS)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            cE, cP = boxes[i], boxes[j]
            if cP - cE < 0.05:
                continue
            ay, by = _affine_from(e_mon, p_mon, cE, cP)
            if not (_AY_MIN <= ay <= _AY_MAX and _BY_MIN <= by <= _BY_MAX):
                continue
            cS = None
            corr = 1                     # 1 = uncorroborated (worse), 0 = corroborated (better)
            score = abs((cP - cE) - ay * exp_gap)   # gap plausibility (0 is ideal)
            if s_mon is not None and all_bands:
                pred = ay * s_mon + by
                near = min(all_bands, key=lambda c: abs(c - pred))
                if abs(near - pred) < 0.05:
                    cS, corr, score = near, 0, abs(near - pred)
            key = (corr, score)
            if best is None or key < best[0]:
                best = (key, ay, by, cE, cP, cS)
    if best is None:
        return None
    _, ay, by, cE, cP, cS = best
    return ay, by, cE, cP, cS


def derive_login_vmap(image_bytes: bytes, anchors: dict) -> dict | None:
    """Derive the per-pose vertical map from a login camera frame + app_map monitor fractions.

    `anchors` = {email, password, signin?, footer?} monitor fractions. Bands are assigned to the form
    elements by a CONSENSUS fit (robust to the title/helper/label bands that otherwise contaminate a
    naive "first two bands" pick). When sign-in and footer are known AND measurable, returns a 4-KNOT
    piecewise map that also anchors the footer (fixing footer-link placement); otherwise the 2-point
    (email, password) affine. Returns {kind, knots, ay, by, ...} or None (caller keeps its configured
    calibration)."""
    e_mon, p_mon = anchors.get("email"), anchors.get("password")
    s_mon, f_mon = anchors.get("signin"), anchors.get("footer")
    if e_mon is None or p_mon is None:
        return None

    # FILLED boxes (global-threshold, "a box fills the column") are the email/password candidates — they
    # reject sparse title/helper TEXT. ALL bands (local-contrast, catches the dim Sign In button too) are
    # the sign-in corroboration set.
    boxes = detect_form_field_fracs(image_bytes)
    bands = detect_login_form_bands(image_bytes)
    for b in boxes:
        if all(abs(b - x) > 0.02 for x in bands):
            bands.append(b)
    bands.sort()

    fit = _consensus_form_fit(boxes, bands, e_mon, p_mon, s_mon)
    if fit is None:
        # Last-resort: the original 2-anchor affine (unchanged behaviour) — itself returns None on a
        # bad frame, so the caller keeps config. Never worse than before.
        res = derive_login_vertical(image_bytes, e_mon, p_mon)
        if res is None:
            return None
        return {"kind": "affine", "knots": [(e_mon, res["email_cam_frac"]), (p_mon, res["password_cam_frac"])],
                "ay": res["ay"], "by": res["by"],
                "email_cam_frac": res["email_cam_frac"], "password_cam_frac": res["password_cam_frac"]}

    ay, by, cE, cP, cS = fit

    # --- Full 4-anchor path (sign-in corroborated by a band + footer measurable) ---
    if s_mon is not None and f_mon is not None and cS is not None:
        fc = detect_footer_band(image_bytes)
        if fc is not None and fc > cS + 0.04:
            knots = [(e_mon, cE), (p_mon, cP), (s_mon, cS), (f_mon, fc)]
            if all(knots[k][1] < knots[k + 1][1] for k in range(3)) and all(-0.05 <= c <= 1.10 for _, c in knots):
                return {"kind": "vmap4", "knots": knots, "ay": ay, "by": by,
                        "email_cam_frac": cE, "password_cam_frac": cP,
                        "signin_cam_frac": cS, "footer_cam_frac": fc}

    # --- 2-anchor affine (consensus email/password) ---
    return {"kind": "affine", "knots": [(e_mon, cE), (p_mon, cP)], "ay": ay, "by": by,
            "email_cam_frac": cE, "password_cam_frac": cP}
