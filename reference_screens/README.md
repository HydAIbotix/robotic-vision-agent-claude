# reference_screens — camera-domain screen templates (Tier-1, 0 LLM)

Reference images used by **template matching** (`vision_agent/vision/template_match.py`) to identify
which kiosk screen the robot is currently on — the real-robot Tier-1 screen identifier, 0 LLM.

## Naming

One file per screen, named **exactly `<screen_id>.png`** where `<screen_id>` is the app_map screen key:

```
login.png
products.png
payment.png
cart.png
smart_card_kiosk_station.png
```

The exact-stem match is deterministic (a file `product_detail.png` will never be mistaken for the
`products` screen). Older names that merely *contain* the screen id (e.g. `Login_page.png`) still work
as a fallback, but the exact name is preferred.

## How to build the library

Capture each screen with the **real robot's camera** (so matching is camera↔camera, the strongest
signal), one screen at a time:

- **UI:** Camera Vision Test page → capture → "Save as reference for screen …".
- **API:** `POST /api/vision-test/save-reference {"screen_id": "login"}` (captures the current arm
  frame and writes `login.png` here).
- **CLI:** `python capture_reference.py --screen login`

Navigate the robot to the target screen first (manually, or via the Robot API tester), then save.

## Notes

- Static-layout screens template-match best. Dynamic screens (live cart/total) still fall through to
  Claude for their variable content — that's expected.
- These are generated per-environment data; not every capture needs to be committed. Keep a good
  reference per screen for the kiosk(s) you test.
