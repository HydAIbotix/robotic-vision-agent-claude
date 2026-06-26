"""
Playwright robot backend — proxy between the vision agent and a live web kiosk.

Set ROBOT_BACKEND=playwright in .env to activate.
The browser opens automatically on first use and stays open for the test run.

Every function has the SAME signature as stubs.py and real_robot.py.
The agent code never changes; only this file is swapped in.

Transition path:
  demo (PNG files)  →  playwright (browser)  →  real (hardware arm)
  The agent sees identical input/output at every stage.
"""
import time
import atexit
from pathlib import Path

_pw    = None          # sync_playwright() handle
_browser = None
_page    = None


def _ensure_page():
    global _pw, _browser, _page
    if _page is not None:
        return _page

    from playwright.sync_api import sync_playwright
    from vision_agent.config import settings

    _pw      = sync_playwright().start()
    _browser = _pw.chromium.launch(
        headless=False,
        args=[
            "--disable-features=VirtualKeyboard",  # suppress Windows touch keyboard
            "--disable-touch-adjustment",
            "--force-device-scale-factor=1",        # prevent DPI scaling in screenshots
        ],
    )
    _page = _browser.new_page(
        viewport={"width": 1400, "height": 900},
        has_touch=False,           # prevent touch-mode input focus from triggering OS keyboard
        device_scale_factor=1.0,   # screenshot pixels == viewport pixels, so coords are exact
    )
    _page.goto(settings.kiosk_url)
    _page.wait_for_load_state("networkidle")
    print(f"\n  [PLAYWRIGHT] Browser opened  →  {settings.kiosk_url}")
    atexit.register(stop)
    return _page


def capture_screen(save_path: str) -> dict:
    """Take a screenshot of the live browser — replaces robot arm camera."""
    page = _ensure_page()
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=save_path, full_page=False)
    return {"success": True, "image_path": save_path, "timestamp": time.time()}


def tap(x: int, y: int) -> dict:
    """Click at (x, y) in the browser — replaces robot arm tap command.

    Uses DOM elementFromPoint to resolve the correct interactive element
    (handles the ~40 px y-offset that Claude's vision consistently produces).
    Returns the element's actual center coordinates, then uses page.mouse.click()
    so the full browser event chain fires (mousedown → focus → mouseup → click),
    which is required for React synthetic events like onFocus to trigger.

    When the exact point misses, snaps to the nearest interactive element within
    80 px — preferring <button> over <input> so "tap: sign_in_button" wins over
    a nearby password field that may be slightly closer in raw distance.
    """
    page = _ensure_page()
    coords = page.evaluate(f"""() => {{
        const isInteractive = (el) => {{
            const tag  = el.tagName.toLowerCase();
            const role = (el.getAttribute('role') || '').toLowerCase();
            return tag === 'button' || tag === 'a' || tag === 'input' ||
                   tag === 'select' || role === 'button' || role === 'link' ||
                   el.onclick != null;
        }};
        const center = (el) => {{
            const r = el.getBoundingClientRect();
            return [Math.round((r.left + r.right) / 2), Math.round((r.top + r.bottom) / 2)];
        }};

        // 1. Try the exact point
        const exact = document.elementFromPoint({x}, {y});
        if (exact && isInteractive(exact)) {{
            return [...center(exact), 'exact'];
        }}

        // 2. Snap: collect all interactive elements within 80 px,
        //    then prefer buttons over inputs (handles sign-in button vs password field
        //    when Claude's coordinate lands between the two).
        const sel = 'button, input, a, select, [role="button"], [role="link"]';
        const nearby = [];
        for (const el of document.querySelectorAll(sel)) {{
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
            const d  = Math.hypot(cx - {x}, cy - {y});
            if (d < 80) {{
                const isBtn = el.tagName.toLowerCase() === 'button' ||
                              (el.getAttribute('role') || '').toLowerCase() === 'button';
                nearby.push([el, d, isBtn]);
            }}
        }}
        if (nearby.length > 0) {{
            nearby.sort((a, b) => {{
                if (a[2] !== b[2]) return a[2] ? -1 : 1;   // buttons first
                return a[1] - b[1];                          // then by distance
            }});
            const [best, dist] = nearby[0];
            return [...center(best), 'snap:' + Math.round(dist) + 'px'];
        }}

        return null;  // fall through to raw mouse click
    }}""")

    if coords:
        cx, cy, snap_type = coords[0], coords[1], coords[2]
        # Real mouse click — fires full browser event chain so React onFocus etc. trigger.
        page.mouse.click(cx, cy)
        page.wait_for_timeout(300)
        if snap_type == "exact":
            print(f"  [PLAYWRIGHT] click({cx}, {cy})")
        else:
            print(f"  [PLAYWRIGHT] tap-snap {snap_type} → ({x},{y}) → ({cx},{cy})")
    else:
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        print(f"  [PLAYWRIGHT] click({x}, {y}) [raw]")

    return {"success": True, "x": x, "y": y}


def type_text(text: str) -> dict:
    """Type into the focused input — replaces robot arm keystroke.

    Uses page.keyboard.type() (real key events) which reliably triggers React's
    onChange on each keystroke. Then clicks the kiosk's "Done" button to dismiss
    its custom virtual keyboard so it doesn't overlap the next element to tap.
    """
    page = _ensure_page()
    page.keyboard.type(text, delay=30)
    page.wait_for_timeout(200)

    # Dismiss the kiosk's custom virtual keyboard via its "Done" button.
    # The button uses onMouseDown:preventDefault so focus stays on the input
    # while the keyboard closes — email/password state is preserved.
    keyboard_done = page.locator('[data-testid="keyboard-done"]')
    if keyboard_done.count() > 0:
        keyboard_done.click()
        page.wait_for_timeout(150)
        print(f"  [PLAYWRIGHT] keyboard-done clicked")

    print(f"  [PLAYWRIGHT] type({text!r})")
    return {"success": True, "text": text}


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
    page = _ensure_page()
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.mouse.move(x2, y2, steps=10)
    page.mouse.up()
    return {"success": True}


def reset_to_entry() -> None:
    """Navigate back to the kiosk entry URL — call between test cases."""
    if _page is not None:
        from vision_agent.config import settings
        _page.goto(settings.kiosk_url)
        _page.wait_for_load_state("networkidle")
        print(f"  [PLAYWRIGHT] Reset to {settings.kiosk_url}")


def set_demo_screens(paths: list[str]) -> None:
    """No-op in playwright mode — real screenshots taken after every action."""
    pass


def stop() -> None:
    global _pw, _browser, _page
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None
    print("  [PLAYWRIGHT] Browser closed")
