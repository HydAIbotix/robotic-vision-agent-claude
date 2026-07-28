"""
capture_reference.py — save the CURRENT kiosk screen as a Tier-1 template reference.

Captures a frame from the robot arm camera (POST /capture) and writes it to the reference-template
folder (settings.template_ref_dir, default ./reference_screens) as "<screen_id>.png". Template
matching (vision_agent/vision/template_match.py) then uses it for 0-LLM real-robot screen identity.

Build the camera-domain library one screen at a time:
    1. Navigate the robot to the target screen (manually, or via the Robot API tester).
    2. Run this for that screen.

Usage:
    python capture_reference.py --screen login
    python capture_reference.py --screen products --type screen
    python capture_reference.py --list

Notes:
    • --type screen (default) = AprilTag-rectified fronto-parallel frame (what runtime uses). Prefer it.
    • --type raw    = unrectified sensor frame (diagnostic only; do NOT use as a template).
    • Overwrites an existing template for that screen. The arm URL comes from .env (ARM_URL).
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")   # Windows-friendly (repo convention)

import argparse
import base64
import re
import time
from pathlib import Path

import requests

from vision_agent.config import settings


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", (s or "").strip().lower()).strip("_-")


def _ref_dir() -> Path:
    d = Path(settings.template_ref_dir or "./reference_screens")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list() -> None:
    d = _ref_dir()
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    files = [f for f in sorted(d.iterdir()) if f.is_file() and f.suffix.lower() in exts]
    print(f"Reference templates in {d}  ({len(files)}):")
    for f in files:
        print(f"  • {f.stem:28s} {f.name}  ({f.stat().st_size} bytes)")
    if not files:
        print("  (none yet — capture some with --screen <id>)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Save the current kiosk screen as a Tier-1 template reference.")
    ap.add_argument("--screen", help="screen_id to save the current frame as (e.g. login, products)")
    ap.add_argument("--type", default="screen", choices=("screen", "raw"),
                    help="capture type (default: screen = AprilTag-rectified)")
    ap.add_argument("--list", action="store_true", help="list existing reference templates and exit")
    args = ap.parse_args()

    if args.list:
        _list()
        return 0

    slug = _slug(args.screen or "")
    if not slug:
        ap.error("--screen <screen_id> is required (or use --list)")

    base   = settings.arm_api_base()
    url    = f"{base}/capture"
    cmd_id = f"save-ref-{slug}-{int(time.time() * 1000)}"
    print(f"[capture] {url}  type={args.type}  screen={slug}")
    try:
        resp = requests.post(url, json={"cmd_id": cmd_id, "type": args.type}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        img_bytes = base64.b64decode(data["image_b64"])
    except Exception as e:
        print(f"[error] capture failed: {type(e).__name__}: {e}")
        return 1

    dest = _ref_dir() / f"{slug}.png"
    dest.write_bytes(img_bytes)
    w, h = data.get("width"), data.get("height")
    print(f"[saved] {dest}  ({w}x{h}, {len(img_bytes)} bytes)")
    print("        Re-run per screen to build the full library; verify on the Camera Vision Test page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
