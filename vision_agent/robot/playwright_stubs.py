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
    """Click at (x, y) in the browser — replaces robot arm tap command."""
    page = _ensure_page()
    page.mouse.click(x, y)
    page.wait_for_timeout(300)   # give React time to re-render
    print(f"  [PLAYWRIGHT] click({x}, {y})")
    return {"success": True, "x": x, "y": y}


def type_text(text: str) -> dict:
    """Fill focused input — replaces robot arm keystroke."""
    page = _ensure_page()
    # page.locator(":focus").fill() fires React's synthetic onChange correctly
    # and is ~10x faster than keyboard.type() with per-char delay.
    try:
        page.locator(":focus").fill(text)
    except Exception:
        page.keyboard.type(text, delay=30)
    print(f"  [PLAYWRIGHT] fill({text!r})")
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
