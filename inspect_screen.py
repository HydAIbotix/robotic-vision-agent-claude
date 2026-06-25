#!/usr/bin/env python3
"""
Standalone screen inspector — runs only the analyze_screen node and prints
every detected UI element with its coordinates, bounding box, and description.

Usage:
    python inspect_screen.py                          # default: login_page.png
    python inspect_screen.py products_page.png
    python inspect_screen.py cart_checkout_page.png
    python inspect_screen.py C:/full/path/to/any.png
"""
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SCREENSHOTS = Path("C:/Users/gsk54/Desktop/Robotics_Project/Kiosk_Screenshots_Latest")

# Pick which screenshot to inspect
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

# Table header
print(f"{'#':<3} {'ID':<28} {'TYPE':<10} {'LABEL':<24} {'BBOX (x1,y1,x2,y2)':<26} {'CENTER':<14} {'CONF'}")
print("-" * 120)

for i, el in enumerate(analysis["elements"], 1):
    bbox_str = f"[{el['bbox'][0]}, {el['bbox'][1]}, {el['bbox'][2]}, {el['bbox'][3]}]"
    center_str = f"[{el['center'][0]}, {el['center'][1]}]"
    print(
        f"{i:<3} {el['id']:<28} {el['type']:<10} {el['label'][:22]:<24} "
        f"{bbox_str:<26} {center_str:<14} {el['confidence']:.2f}"
    )

print()
print("Descriptions:")
for el in analysis["elements"]:
    print(f"  {el['id']}")
    print(f"    → {el['description']}")

print()
print("Raw JSON (copy into your code or tests):")
print(json.dumps(analysis, indent=2))
