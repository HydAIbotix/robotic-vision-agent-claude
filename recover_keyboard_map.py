"""Re-map the virtual keyboard from an existing capture and inject it into app_map.json.

Why this exists: the App Explorer maps the on-screen keyboard once per environment and stores the
result as the top-level ``keyboard_map`` in app_map.json.  A merge bug (fixed in app_map/store.py)
used to DROP that key on a multi-app save, leaving the real robot with nothing to type.  Rather than
forcing a full (expensive) re-exploration just to rebuild the keyboard, this one-shot utility runs
the SAME MAP_KEYBOARD vision prompt on the keyboard screenshot the explorer already captured
(screenshots/keyboard_map_*.png) and writes the result back into app_map.json.

Usage:
    python recover_keyboard_map.py                 # use the newest keyboard_map_*.png
    python recover_keyboard_map.py --image PATH     # use a specific screenshot
    python recover_keyboard_map.py --dry-run        # map + print, do NOT write app_map.json
"""
import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from langchain_core.messages import HumanMessage

from vision_agent.config import settings
from vision_agent.llm import get_llm, invoke_json, detect_image_media_type
import base64
from app_explorer.prompts import MAP_KEYBOARD


def _latest_keyboard_shot() -> str:
    shots = glob.glob(str(Path(settings.screenshots_dir) / "keyboard_map_*.png"))
    if not shots:
        return ""
    return max(shots, key=os.path.getmtime)


def main() -> int:
    ap = argparse.ArgumentParser(description="Recover the app_map keyboard_map from a screenshot.")
    ap.add_argument("--image", default="", help="keyboard screenshot (default: newest keyboard_map_*.png)")
    ap.add_argument("--dry-run", action="store_true", help="map + print only; do not write app_map.json")
    args = ap.parse_args()

    img = args.image or _latest_keyboard_shot()
    if not img or not Path(img).exists():
        print(f"✗ No keyboard screenshot found (looked in {settings.screenshots_dir}/keyboard_map_*.png). "
              f"Re-explore the kiosk to capture one, or pass --image.")
        return 2
    print(f"→ Mapping keyboard from: {img}")

    image_bytes = Path(img).read_bytes()
    b64        = base64.standard_b64encode(image_bytes).decode()
    media_type = detect_image_media_type(image_bytes)
    llm = get_llm()
    msg = HumanMessage(content=[
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": MAP_KEYBOARD},
    ])
    kb_data = invoke_json(llm, [msg], default={"keys": {}}, label="keyboard")
    keys = kb_data.get("keys", {})
    if not keys:
        print("✗ Claude returned no keys — the virtual keyboard may not be visible in this screenshot.")
        return 3

    print(f"✓ Mapped {len(keys)} keys: {sorted(keys.keys())}")
    for special in ("shift", "space", "backspace", "done", "return", "enter", "clear"):
        if special in keys:
            print(f"    {special:9s} → {keys[special]}")

    if args.dry_run:
        print("\n(dry-run) app_map.json NOT modified.")
        return 0

    map_path = settings.app_map_path
    if not Path(map_path).exists():
        print(f"✗ {map_path} does not exist — nothing to inject into.")
        return 4
    app_map = json.loads(Path(map_path).read_text(encoding="utf-8"))
    app_map["keyboard_map"] = {"keys": keys}
    Path(map_path).write_text(json.dumps(app_map, indent=2), encoding="utf-8")
    print(f"\n✓ Injected keyboard_map ({len(keys)} keys) into {map_path}")
    print("  The next real-robot run will load it via set_keyboard_map() and the arm can type.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
