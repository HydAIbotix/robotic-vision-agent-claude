"""
Template-matching screen identification (Tier-1, 0 LLM).

Ported from the proven Agentic_Orchestrator vision layer
(`ObjectFinder-BaseFormat.find_template_image` + `login_happy_path.template_match_score`).

Why this replaces the legacy 16×16 average-hash (aHash) screen matcher
-----------------------------------------------------------------------
aHash reduces a frame to a 16×16 grayscale brightness fingerprint and compares by
Hamming distance. That encodes only coarse light/dark layout, so it CANNOT bridge the
browser↔camera domain gap: a real arm-camera photo (glare, blur, keystone, colour cast,
JPEG) of a screen lands 20-40 bits away from the crisp BROWSER screenshot the app_map
stored — every real frame reads as "no match" and falls to Claude vision.

Normalized cross-correlation (`cv2.matchTemplate` with `TM_CCOEFF_NORMED`) subtracts the
mean and normalizes the variance of BOTH images before correlating, so a uniform change in
brightness/contrast/colour cast (exactly the camera↔browser difference) barely moves the
score. Empirically, on a live arm-camera login photo it separates the correct screen
(≈0.65) from a wrong one (≈0.49) cleanly, where aHash gave 23 vs 25 (indistinguishable).

The template is resized to the page size first, so this is a WHOLE-FRAME layout correlation
(the reference's approach) rather than a sub-window search — appropriate because we already
know the frame is a full rectified screen and we're asking "which known screen is this?".

Pure functions + PIL/OpenCV only (both already project dependencies). No LLM, no network.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterable, Optional

import cv2
import numpy as np
from PIL import Image


# ── Core scoring (ported verbatim in spirit from login_happy_path.template_match_score) ──

def _to_bgr_from_bytes(image_bytes: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _to_bgr_from_path(path: str) -> Optional[np.ndarray]:
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def template_match_score(page_bgr: np.ndarray, template_bgr: np.ndarray) -> float:
    """Full-frame normalized-cross-correlation score in [-1.0, 1.0] (higher = more similar).

    Both images are converted to grayscale; the template is resized to the page's dimensions
    so the whole layout is correlated. Returns the peak `TM_CCOEFF_NORMED` value.
    """
    page_gray = cv2.cvtColor(page_bgr, cv2.COLOR_BGR2GRAY)
    tmpl_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
    ph, pw = page_gray.shape[:2]
    th, tw = tmpl_gray.shape[:2]
    if (th, tw) != (ph, pw):
        tmpl_gray = cv2.resize(tmpl_gray, (pw, ph), interpolation=cv2.INTER_AREA)
    result = cv2.matchTemplate(page_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(result)
    return float(max_val)


# ── Reference resolution ─────────────────────────────────────────────────────

def _stem_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", Path(name).stem.lower()) if t]


def resolve_template_ref(screen_id: str, template_ref_dir: str) -> Optional[str]:
    """Return a path to an override reference template for `screen_id` from `template_ref_dir`, or None.

    Two-pass, most-specific-wins (so one screen name being a prefix of another can never mis-assign):
      1. EXACT stem match — a file named exactly `<screen_id>.<ext>` (e.g. `login.png` → screen
         `login`). This is the recommended convention and is unambiguous.
      2. Token-membership fallback — the screen_id is one of the filename's alphanumeric tokens
         (e.g. `Login_page.png` → tokens {login, page} → screen `login`). Keeps older `*_page.png`
         names working. Prefix matching is intentionally NOT used, so `product_detail.png` can never
         be picked for screen `products`.
    """
    if not screen_id or not template_ref_dir:
        return None
    d = Path(template_ref_dir)
    if not d.is_dir():
        return None
    sid = screen_id.lower()
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    files = [f for f in sorted(d.iterdir()) if f.is_file() and f.suffix.lower() in exts]

    # Pass 1 — exact stem match (deterministic).
    for f in files:
        if f.stem.lower() == sid:
            return str(f)

    # Pass 2 — token-membership fallback.
    for f in files:
        if sid in _stem_tokens(f.name):
            return str(f)
    return None


def build_references(app_map: dict, template_ref_dir: str = "") -> dict[str, str]:
    """Return {screen_id: reference_image_path} for every screen that has a usable reference.

    Resolution order per screen:
      1. an override file in `template_ref_dir` whose name matches the screen_id (clean templates)
      2. the screen's app_map `reference_screenshot` (auto-captured during exploration)
    Screens with no existing reference file are omitted.
    """
    refs: dict[str, str] = {}
    for sid, sc in ((app_map or {}).get("screens") or {}).items():
        override = resolve_template_ref(sid, template_ref_dir)
        if override:
            refs[sid] = override
            continue
        ref_path = (sc or {}).get("reference_screenshot", "")
        if ref_path and Path(ref_path).exists():
            refs[sid] = ref_path
    return refs


# ── Ranking + verdict ────────────────────────────────────────────────────────

def rank_references(image_bytes: bytes, references: dict[str, str]) -> list[dict]:
    """Score the frame against every reference. Returns [{screen_id, score}] best-first.

    `references` maps screen_id → image path (see build_references). Unreadable references
    are skipped.
    """
    page = _to_bgr_from_bytes(image_bytes)
    rows: list[dict] = []
    for sid, path in references.items():
        tmpl = _to_bgr_from_path(path)
        if tmpl is None:
            continue
        try:
            rows.append({"screen_id": sid, "score": round(template_match_score(page, tmpl), 4)})
        except Exception:
            continue
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def identify_screen(
    image_bytes: bytes,
    references: dict[str, str],
    expected_screen: str = "",
    threshold: float = 0.55,
    margin: float = 0.06,
) -> dict:
    """Decide, from template scores, whether the frame is the expected screen (or which screen it is).

    Return contract (mirrors the legacy `_match_by_phash` so callers are unchanged):
      success (bool|None): True = on the expected screen (or, with no expected, a confident single
        match); False = confidently on a DIFFERENT known screen; None = inconclusive → LLM fallback.
      actual_screen (str): best-guess screen id.
      score (float): the score that drove the verdict.
      method (str): 'template_match' | 'template_mismatch' | 'template_inconclusive' | 'template_no_reference'.
      ranking (list): full [{screen_id, score}] best-first (for diagnostics).
    """
    ranking = rank_references(image_bytes, references)
    if not ranking:
        return {"success": None, "actual_screen": "", "score": None,
                "method": "template_no_reference", "ranking": []}

    best = ranking[0]
    second_score = ranking[1]["score"] if len(ranking) > 1 else -1.0

    if expected_screen:
        exp = next((r for r in ranking if r["screen_id"] == expected_screen), None)
        exp_score = exp["score"] if exp else -1.0
        best_other = max((r["score"] for r in ranking if r["screen_id"] != expected_screen),
                         default=-1.0)
        # MATCH — expected clears the floor and is the (near-)top scorer.
        if exp_score >= threshold and exp_score >= best_other - 1e-9:
            return {"success": True, "actual_screen": expected_screen, "score": exp_score,
                    "method": "template_match", "ranking": ranking}
        # MISMATCH — a different known screen clears the floor and clearly beats expected.
        if best_other >= threshold and best_other >= exp_score + margin:
            best_other_sid = next(r["screen_id"] for r in ranking if r["screen_id"] != expected_screen)
            return {"success": False, "actual_screen": best_other_sid, "score": best_other,
                    "method": "template_mismatch", "ranking": ranking}
        # Otherwise inconclusive → caller falls through to Claude vision.
        return {"success": None, "actual_screen": best["screen_id"], "score": exp_score,
                "method": "template_inconclusive", "ranking": ranking}

    # No expected screen — pure identification (Camera Vision Test).
    if best["score"] >= threshold and best["score"] >= second_score + margin:
        return {"success": True, "actual_screen": best["screen_id"], "score": best["score"],
                "method": "template_match", "ranking": ranking}
    if best["score"] >= threshold:
        return {"success": None, "actual_screen": best["screen_id"], "score": best["score"],
                "method": "template_inconclusive", "ranking": ranking}
    return {"success": None, "actual_screen": best["screen_id"], "score": best["score"],
            "method": "template_inconclusive", "ranking": ranking}
