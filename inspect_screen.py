#!/usr/bin/env python3
"""
Standalone screen inspector — runs only the analyze_screen node and prints
every detected UI element with its coordinates, bounding box, and description.
Also saves an annotated PNG so you can visually verify coordinate accuracy.

Usage:
    python inspect_screen.py                          # default: login_page.png
    python inspect_screen.py products_page.png
    python inspect_screen.py cart_checkout_page.png
    python inspect_screen.py C:/full/path/to/any.png
"""
import sys
import io
import json
from pathlib import Path

# Force UTF-8 output so Unicode characters in element labels print correctly
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

SCREENSHOTS = Path("C:/Users/gsk54/Desktop/Robotics_Project/Kiosk_Screenshots_Latest")

arg = sys.argv[1] if len(sys.argv) > 1 else "login_page.png"
image_path = Path(arg) if Path(arg).is_absolute() else SCREENSHOTS / arg

if not image_path.exists():
    print(f"File not found: {image_path}")
    sys.exit(1)

print(f"\nInspecting: {image_path.name}")
print("=" * 72)

from vision_agent.nodes.analyze import analyze_screen

result = analyze_screen({
    "image_path": str(image_path),
    "screen_history": [],
})

analysis = result["screen_analysis"]

print(f"Screen ID  : {analysis['screen_id']}")
print(f"Description: {analysis['description']}")
print(f"Elements   : {len(analysis['elements'])} found")
print()

# ── Coordinate table ──────────────────────────────────────────────────────────
print(f"{'#':<3} {'ID':<28} {'TYPE':<10} {'LABEL':<24} {'BBOX (x1,y1,x2,y2)':<30} {'CENTER':<16} CONF")
print("-" * 122)
for i, el in enumerate(analysis["elements"], 1):
    b = el["bbox"]
    c = el["center"]
    bbox_str   = f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]"
    center_str = f"[{c[0]}, {c[1]}]"
    print(f"{i:<3} {el['id']:<28} {el['type']:<10} {el['label'][:22]:<24} "
          f"{bbox_str:<30} {center_str:<16} {el['confidence']:.2f}")

print()
print("Descriptions:")
for el in analysis["elements"]:
    print(f"  {el['id']}")
    print(f"    -> {el['description']}")

# ── Annotated image ───────────────────────────────────────────────────────────
img = Image.open(image_path).convert("RGB")
draw = ImageDraw.Draw(img, "RGBA")

COLOURS = [
    (255, 80,  80),   # red
    (80,  200, 120),  # green
    (80,  160, 255),  # blue
    (255, 180,  50),  # amber
    (200,  80, 255),  # purple
    (50,  220, 220),  # cyan
]

for i, el in enumerate(analysis["elements"]):
    colour = COLOURS[i % len(COLOURS)]
    x1, y1, x2, y2 = el["bbox"]
    cx, cy = el["center"]
    r = 8  # crosshair radius

    # Semi-transparent fill
    draw.rectangle([x1, y1, x2, y2], fill=(*colour, 40), outline=(*colour, 220), width=3)

    # Crosshair at center
    draw.line([cx - r, cy, cx + r, cy], fill=colour, width=3)
    draw.line([cx, cy - r, cx, cy + r], fill=colour, width=3)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=colour, width=2)

    # Label (id + coords)
    label = f"{el['id']}  [{cx}, {cy}]"
    tx, ty = x1, max(0, y1 - 22)
    draw.rectangle([tx, ty, tx + len(label) * 9, ty + 20], fill=(0, 0, 0, 180))
    draw.text((tx + 3, ty + 2), label, fill=colour)

out_path = Path("screenshots") / f"annotated_{image_path.stem}.png"
out_path.parent.mkdir(exist_ok=True)
img.save(out_path)

print(f"\nAnnotated image saved -> {out_path}")
print("Open it to visually verify that bounding boxes and centers align with the UI elements.")

print()
print("Raw JSON:")
print(json.dumps(analysis, indent=2))
