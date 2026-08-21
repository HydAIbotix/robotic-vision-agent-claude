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
_keyboard_map: dict = {}   # populated by set_keyboard_map() after App Explorer runs
_progress_injected: bool = False  # True once the HUD overlay div has been created
_dom_to_screen_id: dict | None = None  # reverse lookup: dom_id → app_map screen_id


def _vp() -> tuple[int, int]:
    """Playwright viewport (w, h) from settings — the coordinate space app_map is learned in.

    SINGLE source of truth so the browser viewport, the on-screen-keyboard taps, and the app_map
    pixel space always agree. Default 1400×900. When the eventual test target is the REAL ROBOT,
    set VIEWPORT_WIDTH/HEIGHT to the kiosk's rectified-camera aspect ratio (e.g. 1920×1080, 16:9)
    BEFORE exploring, so the app renders the same layout the arm will photograph and the
    viewport→camera scale (real_robot._scale) is a clean proportional map with no aspect distortion."""
    from vision_agent.config import settings
    return settings.viewport_width, settings.viewport_height


def _kiosk_url() -> str:
    """The kiosk URL to open, with the shared card service appended when configured so the
    kiosk apps share balances across machines, and the forced screen layout appended so
    exploration always renders the SAME layout the physical kiosk shows (see
    settings.kiosk_screen_layout). No-op (bare kiosk_url) when both are unset."""
    from vision_agent.config import settings
    url = settings.kiosk_url
    svc = (getattr(settings, "card_service_url", "") or "").strip()
    if svc:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}cardServiceUrl={svc}"
    layout = (getattr(settings, "kiosk_screen_layout", "") or "").strip()
    if layout:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}screenLayout={layout}"
    # During EXPLORATION only (run_explorer.py sets explore_demo_card), pre-fill the RPS mock-card field
    # with the always-available demo card so the payment flow can be completed without a human/typed
    # card. Test execution never sets this flag, so real runs enter their own captured card unaffected.
    if getattr(settings, "explore_demo_card", False):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}demoCard=1"
    return url


def _load_dom_to_screen_cache() -> dict:
    """Build and cache a reverse lookup: normalized dom_id → app_map screen_id.

    The App Explorer names screens semantically (e.g. "login") while data-testid
    may use a different word (e.g. "signin-screen" → "signin"). This mapping
    bridges the two so verify steps compare apples to apples.
    Loaded once from app_map.json; invalidated by setting _dom_to_screen_id = None.
    """
    global _dom_to_screen_id
    if _dom_to_screen_id is not None:
        return _dom_to_screen_id
    try:
        import json
        from pathlib import Path
        from vision_agent.config import settings
        path = Path(getattr(settings, "app_map_path", "app_map.json"))
        if path.exists():
            data = json.loads(path.read_text())
            mapping: dict = {}
            for screen_key, screen_data in data.get("screens", {}).items():
                if not isinstance(screen_data, dict):
                    continue
                dom_id = (screen_data.get("dom_id") or "").strip()
                if dom_id:
                    mapping[dom_id] = screen_key
            _dom_to_screen_id = mapping
            return mapping
    except Exception:
        pass
    _dom_to_screen_id = {}
    return {}


def _ensure_page():
    global _pw, _browser, _page
    if _page is not None:
        # Verify the page is still alive — a previous crash may have left a stale
        # module-level reference while the underlying browser process was killed.
        try:
            _ = _page.url  # cheap property; raises TargetClosedError if page is gone
            return _page
        except Exception:
            print("  [PLAYWRIGHT] Stale page detected — reopening browser")
            try:
                if _browser:
                    _browser.close()
                if _pw:
                    _pw.stop()
            except Exception:
                pass
            _pw = _browser = _page = None

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
    _vw, _vh = settings.viewport_width, settings.viewport_height
    _page = _browser.new_page(
        viewport={"width": _vw, "height": _vh},
        has_touch=False,           # prevent touch-mode input focus from triggering OS keyboard
        device_scale_factor=1.0,   # screenshot pixels == viewport pixels, so coords are exact
    )
    print(f"  [PLAYWRIGHT] viewport {_vw}×{_vh}  (app_map coordinate space)")
    _page.goto(_kiosk_url())
    _page.wait_for_load_state("networkidle")
    print(f"\n  [PLAYWRIGHT] Browser opened  →  {_kiosk_url()}")
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
        if snap_type == "exact":
            print(f"  [PLAYWRIGHT] click({cx}, {cy})")
        else:
            print(f"  [PLAYWRIGHT] tap-snap {snap_type} → ({x},{y}) → ({cx},{cy})")
    else:
        page.mouse.click(x, y)
        print(f"  [PLAYWRIGHT] click({x}, {y}) [raw]")

    # Wait for React SPA route changes to finish rendering.
    # 1200ms timeout for the load state; for already-loaded SPAs this completes
    # immediately, so wait_for_timeout provides the real settle window.
    # 800ms handles slow checkout/cart navigations (add-to-cart toast can take ~600ms
    # to clear before the cart button tap registers on the correct target).
    try:
        page.wait_for_load_state("domcontentloaded", timeout=1500)
    except Exception:
        pass  # timeout fires on non-navigating clicks — that is expected and fine
    # Settle for React re-renders, toasts, AND apps that inject artificial navigation/action
    # delays (kiosk test rigs use ~900ms).  A shorter wait captures the screen BEFORE a delayed
    # popup/route renders, causing the agent to miss it and re-tap the same control.
    page.wait_for_timeout(1400)

    return {"success": True, "x": x, "y": y}


def tap_image_point(px: int, py: int) -> dict:
    """Tap a point in the last-captured image's pixel space (vision-derived coords). The browser
    screenshot IS the viewport, so image pixels == viewport pixels — delegate to tap() (which also snaps
    to the nearest interactive element, helpful for slightly-off vision coordinates)."""
    return tap(int(px), int(py))


def set_keyboard_map(kmap: dict) -> None:
    """Load the virtual keyboard coordinate map produced by App Explorer.

    After this is called, type_text() clicks each character on the virtual
    keyboard using page.mouse.click() — identical to a physical robot arm tap —
    instead of sending real keyboard events.  Call once at test-run startup
    after loading app_map.json.
    """
    global _keyboard_map
    _keyboard_map = kmap.get("keys", kmap)   # accept both {keys:{}} and flat {char:[x,y]}
    print(f"  [PLAYWRIGHT] keyboard map loaded ({len(_keyboard_map)} keys)")


def _click_key(page, char: str) -> bool:
    """Click a single key on the virtual keyboard. Returns True if the key was found."""
    needs_shift = char.isupper() and char.isalpha()
    if char == " ":
        lookup = "space"   # keyboard map stores space bar as "space", not " "
    elif char.isalpha():
        lookup = char.lower()
    else:
        lookup = char
    coords = _keyboard_map.get(lookup) or _keyboard_map.get(char)
    if not coords:
        return False
    vw, vh = _vp()   # keyboard coords are normalized fractions → scale by the current viewport
    # Tap shift before the key — the kiosk keyboard is ONE-SHOT: it auto-returns to
    # lowercase after typing exactly one uppercase letter (App.tsx line 2273-2274).
    # Do NOT tap shift a second time; that would re-enable uppercase for the next char.
    if needs_shift and "shift" in _keyboard_map:
        sc = _keyboard_map["shift"]
        page.mouse.click(int(sc[0] * vw), int(sc[1] * vh))
        page.wait_for_timeout(60)
    px = int(coords[0] * vw)
    py = int(coords[1] * vh)
    page.mouse.click(px, py)
    page.wait_for_timeout(60)   # key debounce — matches physical tap cadence
    return True


def type_text(text: str, clear_first: bool = True) -> dict:
    """Type into the focused input via keyboard events, then dismiss the virtual keyboard.

    page.mouse.click() on virtual keyboard keys fires mousedown which blurs the
    focused input — the keyboard closes before the click registers, so characters
    are lost.  page.keyboard.type() sends key events directly to the focused DOM
    element; React's onChange handler picks them up correctly regardless of whether
    a virtual keyboard overlay is open.

    The keyboard map IS still used to locate and tap the 'done' key after typing,
    which matches real robot-arm behaviour (arm physically taps Done to close keyboard).

    clear_first=True (default): sends Ctrl+A before typing so any pre-filled text
    in the field (e.g. sign-up form defaults) is selected and replaced, not appended.
    """
    page = _ensure_page()

    if clear_first and text:
        # Select all pre-existing text — replaced by the upcoming keyboard.type() call.
        page.keyboard.press("Control+a")
        page.wait_for_timeout(80)

    page.keyboard.type(text, delay=30)
    page.wait_for_timeout(200)

    # Dismiss the virtual keyboard.
    # IMPORTANT: prefer data-testid over coordinates — the LLM sometimes assigns
    # a slightly wrong 'done' key position that lands on an adjacent '-' key,
    # which appends '-' to the typed text and causes auth failures.
    kb_done = page.locator('[data-testid="keyboard-done"]')
    if kb_done.count() > 0:
        kb_done.click()
        page.wait_for_timeout(150)
    elif _keyboard_map:
        # Testid not found — fall back to coordinate from map
        done_coords = (
            _keyboard_map.get("done")
            or _keyboard_map.get("return")
            or _keyboard_map.get("enter")
        )
        if done_coords:
            vw, vh = _vp()
            page.mouse.click(int(done_coords[0] * vw), int(done_coords[1] * vh))
            page.wait_for_timeout(150)

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
    """Navigate back to the kiosk entry URL — call between test cases.

    Clears localStorage and sessionStorage first so the kiosk app starts
    unauthenticated (no session restore).  Without this, a logged-in kiosk
    SPA auto-redirects back to the products page on every reset, making
    login-page actions test the wrong screen.

    EXCEPTION: keys matching `settings.reset_preserve_storage_keys` are preserved across the reset,
    so smart-card DATA issued in an earlier test survives for a later top-up / balance / history test
    (cards live in the kiosk's localStorage when the shared card service is offline). This is why a
    card from TC-VPS-001 was previously "not issued" in TC-VPS-002 — the reset wiped it. UI/auth/nav
    state (session, orders, config) is NOT matched by the default list, so it still resets per test.
    """
    global _progress_injected
    from vision_agent.config import settings
    if _page is not None:
        preserve = [k.strip().lower() for k in (settings.reset_preserve_storage_keys or "").split(",") if k.strip()]
        _page.evaluate(
            """(preserve) => {
                const keep = {};
                if (preserve && preserve.length) {
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (k && preserve.some(p => k.toLowerCase().includes(p))) keep[k] = localStorage.getItem(k);
                    }
                }
                localStorage.clear(); sessionStorage.clear();
                for (const k in keep) localStorage.setItem(k, keep[k]);
            }""",
            preserve,
        )
        _page.goto(_kiosk_url())
        _page.wait_for_load_state("networkidle")
        _progress_injected = False  # page reload wiped the HUD div — re-inject on next update
        if preserve:
            print(f"  [PLAYWRIGHT] Reset to {_kiosk_url()}  (preserved storage keys ~ {preserve})")
        else:
            print(f"  [PLAYWRIGHT] Reset to {_kiosk_url()}")


def navigate_to_url(url: str) -> dict:
    """Navigate the browser to a specific URL — used to switch between kiosk apps when the
    robot moves between devices on a multi-device run.  Unlike reset_to_entry it does NOT
    clear storage, so the shared card cache / login session survive the switch."""
    global _progress_injected
    page = _ensure_page()
    try:
        page.goto(url)
        page.wait_for_load_state("networkidle")
        _progress_injected = False
        print(f"  [PLAYWRIGHT] Navigated to {url}")
        return {"success": True, "url": url}
    except Exception as e:
        print(f"  [PLAYWRIGHT] navigate_to_url error: {e}")
        return {"success": False, "error": str(e)}


def scroll_page(x: int, y: int, delta_y: int) -> dict:
    """Scroll the page at viewport position (x, y) by delta_y pixels (positive = down)."""
    page = _ensure_page()
    page.mouse.move(x, y)           # position the wheel over the scrollable area
    page.mouse.wheel(0, delta_y)    # Playwright wheel(deltaX, deltaY) — no x/y args
    page.wait_for_timeout(300)
    return {"success": True, "delta_y": delta_y}


def get_page_scroll_info() -> dict:
    """Return current scroll position and full page dimensions."""
    page = _ensure_page()
    return page.evaluate("""() => ({
        scrollTop:      window.scrollY,
        scrollLeft:     window.scrollX,
        scrollHeight:   document.documentElement.scrollHeight,
        scrollWidth:    document.documentElement.scrollWidth,
        viewportHeight: window.innerHeight,
        viewportWidth:  window.innerWidth,
    })""")


def get_dom_screen_id() -> str:
    """Generically detect the current screen using data-testid attributes.

    Algorithm (app-agnostic — works for any web app):
      1. Scan all [data-testid] elements that cover ≥ 25 % of the viewport.
         Those are screen-level sections, not small widgets.
      2. Prefer testids that contain "screen", "page" or "view" — common naming
         conventions for top-level route containers.
      3. Normalize the winning testid to a snake_case screen_id:
           "signin-screen"     → "signin"
           "store-info-screen" → "store_info"
           "main-page"         → "main"
           "kiosk-products"    → "kiosk_products"

    Returns the normalized screen_id, or "" when no large testid is found
    (falls back to perceptual hash + Claude vision in identify_result).
    """
    page = _ensure_page()
    try:
        testid: str = page.evaluate("""() => {
            const vpArea = window.innerWidth * window.innerHeight;
            // Two buckets with different thresholds:
            //   screen/page/view testids → 5 % (catches card-style screens like signin)
            //   everything else          → 25 % (avoids small widgets)
            const screenLike = [], others = [];
            for (const el of document.querySelectorAll('[data-testid]')) {
                const r = el.getBoundingClientRect();
                const area = r.width * r.height;
                if (area === 0) continue;
                const tid = el.getAttribute('data-testid') || '';
                if (!tid) continue;
                if (tid.includes('screen') || tid.includes('page') || tid.includes('view')) {
                    if (area >= vpArea * 0.05) screenLike.push({testid: tid, area: area});
                } else {
                    if (area >= vpArea * 0.25) others.push({testid: tid, area: area});
                }
            }
            const ranked = screenLike.length ? screenLike : others;
            if (!ranked.length) return '';
            ranked.sort((a, b) => b.area - a.area);
            return ranked[0].testid;
        }""")
    except Exception:
        return ""

    if not testid:
        # Fallback: derive screen_id from the URL path.
        # Works for any SPA using clean routing (e.g. /payment → "payment",
        # /card-payment → "card_payment").  Allows verification of screens
        # that exist in the kiosk but haven't been added to the app_map yet.
        try:
            url  = _ensure_page().url
            part = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
            if part and part not in ("", "index.html", "index"):
                testid = part
            else:
                return ""
        except Exception:
            return ""

    # Normalize to snake_case screen_id
    sid = testid
    for suffix in ("-screen", "-page", "-view", "screen", "page", "view"):
        if sid.endswith(suffix):
            sid = sid[:-len(suffix)]
            break
    sid = sid.lstrip("-").replace("-", "_")

    # Resolve to the canonical app_map screen_id via dom_id reverse lookup.
    # Handles cases where Explorer named a screen differently from its data-testid
    # (e.g. app_map "login" ↔ data-testid "signin-screen" → DOM sid "signin").
    dom_map = _load_dom_to_screen_cache()
    return dom_map.get(sid, sid)


def verify_current_screen(expected_screen_id: str, app_map: dict, save_path: str = "") -> dict:
    """DOM-based screen verification (playwright backend).

    Uses get_dom_screen_id() — no screenshot, no LLM call.
    Same speed as before; save_path is accepted but unused (no image captured).
    """
    actual = get_dom_screen_id()
    return {
        "actual_screen": actual,
        "match": bool(actual) and (actual == expected_screen_id),
        "method": "dom",
    }


def get_dom_element_centers() -> list[dict]:
    """Return all visible interactive elements with their text, center coords, and testid.

    Used by App Explorer's DOM coordinate correction step to fix cases where
    Claude's vision analysis misplaces elements (e.g. sidebar nav links placed
    ~250px off because the layout confuses the vision model).  The text from
    each DOM element is matched against Claude's element labels; when a clear
    text-match exists but the position differs significantly, the coordinate is
    corrected to the DOM-true value.
    """
    page = _ensure_page()
    try:
        return page.evaluate("""() => {
            const results = [];
            const sel = 'button, a, input, select, [role="button"], [role="link"]';
            for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                // Collect visible text: textContent for buttons/links, placeholder for inputs
                const text = (
                    el.textContent ||
                    el.getAttribute('placeholder') ||
                    el.getAttribute('aria-label') || ''
                ).trim().slice(0, 80);
                results.push({
                    text:   text,
                    // aria-label kept SEPARATELY so bare-symbol controls ("+"/"−") — whose textContent
                    // is too short to text-match — can still be identified by their semantic id via
                    // the DOM-correction token fallback (aria "Increase … quantity" + testid tokens).
                    aria:   (el.getAttribute('aria-label') || '').trim().slice(0, 120),
                    cx:     Math.round(r.left + r.width  / 2),
                    cy:     Math.round(r.top  + r.height / 2),
                    tag:    el.tagName.toLowerCase(),
                    testid: el.getAttribute('data-testid') || '',
                });
            }
            return results;
        }""")
    except Exception:
        return []


def focus_by_testid(testid: str) -> bool:
    """Focus a form field by its stable data-testid (deterministic, coord-independent).

    Typing relies on the right field being focused first. Focusing by pixel coordinate is fragile
    when the app_map coords are stale/state-dependent (e.g. the VPS top-up panel shifts ~112px
    depending on whether a reader box is open), so a type can land in the WRONG field. When the
    app_map element carries a testid we focus by DOM identity instead — immune to coordinate drift.
    Returns True only if a focusable field was found and focused; False → caller falls back to a tap.
    """
    if not testid:
        return False
    page = _ensure_page()
    try:
        loc = page.locator(f'[data-testid="{testid}"]')
        if loc.count() == 0:
            return False
        el = loc.first
        # Prefer the field itself; if the testid is on a wrapper, focus the input/textarea inside it.
        target = el
        try:
            inner = el.locator("input, textarea, select")
            if inner.count() > 0:
                target = inner.first
        except Exception:
            pass
        target.scroll_into_view_if_needed(timeout=1000)
        target.click(timeout=1500)   # full event chain → React onFocus fires
        print(f"  [PLAYWRIGHT] focus [data-testid={testid!r}]")
        return True
    except Exception as e:
        print(f"  [PLAYWRIGHT] focus_by_testid({testid!r}) failed: {e}")
        return False


def tap_by_testid(testid: str) -> bool:
    """Click a control by its stable data-testid (deterministic, coord-independent).

    Coordinate taps snap to the nearest interactive element within 80px, so an app_map coord that
    is stale by more than that (e.g. the VPS top-up buttons drifted ~112px) falls through to a RAW
    click on empty space and the button is silently MISSED — the action never fires yet the step
    reports success. When the app_map element carries a testid we click it by DOM identity instead.
    Includes the same post-click settle as tap() so React re-renders finish before the next verify.
    Returns True only if a single matching element was found and clicked; False → caller falls back.
    """
    if not testid:
        return False
    page = _ensure_page()
    try:
        loc = page.locator(f'[data-testid="{testid}"]')
        n = loc.count()
        if n == 0:
            return False
        if n > 1:
            # Ambiguous — don't guess; let the coordinate path (with its spatial snap) decide.
            print(f"  [PLAYWRIGHT] tap_by_testid({testid!r}) skipped — {n} matches (ambiguous)")
            return False
        loc.first.scroll_into_view_if_needed(timeout=1000)
        loc.first.click(timeout=1500)
        print(f"  [PLAYWRIGHT] tap [data-testid={testid!r}]")
    except Exception as e:
        print(f"  [PLAYWRIGHT] tap_by_testid({testid!r}) failed: {e}")
        return False
    # Same settle window as tap() — SPA route changes / toasts / injected kiosk delays.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=1500)
    except Exception:
        pass
    page.wait_for_timeout(1400)
    return True


def get_page_error_text() -> str:
    """Return the text of any VISIBLE error/alert banner on the page, else "".

    A `verify` that only asserts the screen id passes on identity alone — but the app can sit on the
    right screen while showing a FAILURE banner ("No card found", "…is not issued", "declined"). This
    reads error surfaces generically — elements whose data-testid contains "error", `[role="alert"]`,
    `aria-invalid`, or an `error`/`danger` class — and returns their combined non-empty text so the
    validation pipeline can FAIL the step. Deterministic DOM read, zero LLM. Errors that the app
    clears on success render empty → "" → no false failure.
    """
    page = _ensure_page()
    try:
        return page.evaluate("""() => {
            const sel = '[data-testid*="error" i],[data-testid*="alert" i],[role="alert"],' +
                        '[aria-invalid="true"],.error,.error-message,[class*="error" i],[class*="danger" i]';
            const seen = new Set(); const out = [];
            for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;           // not visible
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || +st.opacity === 0) continue;
                const t = (el.textContent || '').trim();
                if (!t || t.length > 200 || seen.has(t)) continue;       // empty / boilerplate / dup
                seen.add(t); out.push(t);
            }
            return out.join(' | ');
        }""") or ""
    except Exception:
        return ""


def navigate_to_screen(screen_id: str) -> bool:
    """Navigate to an authenticated screen via its sidebar/nav button — no login needed.

    Derives the expected nav button label generically from the screen_id:
      "categories"    → "Categories"
      "store_info"    → "Store Info"
      "order_history" → "Order History"

    Uses :has-text() selectors so it works regardless of exact DOM structure or
    whether the kiosk layout changes (sidebar, top nav, tabs — all work the same).
    Returns True if a matching clickable element was found and clicked.
    """
    # Generic derivation: underscore → space, title-case each word
    nav_text = screen_id.replace("_", " ").title()
    page = _ensure_page()
    try:
        for selector in (
            f'a:has-text("{nav_text}")',
            f'button:has-text("{nav_text}")',
            f'[role="link"]:has-text("{nav_text}")',
            f'[role="button"]:has-text("{nav_text}")',
        ):
            loc = page.locator(selector)
            if loc.count() > 0:
                loc.first.click()
                page.wait_for_timeout(500)
                return True
    except Exception as e:
        print(f"  [PLAYWRIGHT] navigate_to_screen('{screen_id}') error: {e}")
    return False


def update_explorer_progress(explored: int, total: int, current_action: str = "") -> None:
    """Show/update a floating HUD overlay in the kiosk browser during App Explorer runs.

    First call injects a fixed-position <div> at bottom-right. Subsequent calls
    update its content in-place. The overlay is pointer-events:none so it cannot
    interfere with any click or tap the explorer performs on the page itself.

    The div is re-injected automatically whenever a page navigation (reset_to_entry
    or initial load) wipes the DOM — checked on every call via a lightweight JS probe.
    """
    global _progress_injected
    if _page is None:
        return
    try:
        # Check whether the div still exists — a page navigation (reset_to_entry)
        # wipes the DOM and destroys the overlay even though _progress_injected is True.
        hud_exists: bool = _page.evaluate("() => !!document.getElementById('__explorer_hud')")
        if not hud_exists:
            _progress_injected = False

        if not _progress_injected:
            _page.evaluate("""() => {
                const d = document.createElement('div');
                d.id = '__explorer_hud';
                d.style.cssText = [
                    'position:fixed', 'bottom:12px', 'right:12px',
                    'z-index:2147483647',
                    'background:rgba(15,23,42,0.92)',
                    'color:#e2e8f0',
                    'border:1px solid #334155',
                    'border-radius:10px',
                    'padding:10px 14px',
                    'font:600 12px/1.6 monospace',
                    'max-width:280px',
                    'pointer-events:none',
                    'box-shadow:0 4px 16px rgba(0,0,0,0.5)',
                ].join(';');
                document.body.appendChild(d);
            }""")
            _progress_injected = True

        pct    = min(100, int(explored / total * 100)) if total else 0
        filled = round(pct / 5)          # out of 20 chars
        bar    = "█" * filled + "░" * (20 - filled)
        label  = current_action[:42] if current_action else ""

        _page.evaluate(
            """([pct, explored, total, bar, label]) => {
                const el = document.getElementById('__explorer_hud');
                if (!el) return;
                el.innerHTML =
                    '<div style="color:#7dd3fc;margin-bottom:2px">🔍 App Explorer</div>' +
                    '<div style="color:#a3e635;letter-spacing:1px">' + bar + '</div>' +
                    '<div>' + pct + '%  (' + explored + ' / ' + total + ' actions)</div>' +
                    (label ? '<div style="color:#94a3b8;font-size:10px">' + label + '</div>' : '');
            }""",
            [pct, explored, total, bar, label],
        )
    except Exception:
        pass   # never crash exploration because of the HUD overlay


def get_aria_snapshot() -> dict:
    """Return the ARIA accessibility tree of the current page as a nested dict:
    {role, name, value, checked, expanded, disabled, children:[...]}.

    Playwright removed page.accessibility.snapshot() (gone by 1.60), so we read the
    same Chromium-computed accessibility tree over CDP (Accessibility.getFullAXTree)
    and rebuild it in that shape.  Ignored (layout-only) nodes are flattened away —
    equivalent to the old interesting_only=True.  Zero LLM calls.
    Returns {} on error (caller falls back to Claude vision).
    """
    page = _ensure_page()

    def _tri(v):
        if v in (True, "true"):   return True
        if v in (False, "false"): return False
        return None   # 'mixed' / absent

    try:
        cdp = page.context.new_cdp_session(page)
        try:
            resp = cdp.send("Accessibility.getFullAXTree")
        finally:
            try:
                cdp.detach()
            except Exception:
                pass

        nodes = resp.get("nodes") or []
        if not nodes:
            return {}
        by_id = {n["nodeId"]: n for n in nodes}

        def _prop(n: dict, key: str):
            for pr in n.get("properties") or []:
                if pr.get("name") == key:
                    return (pr.get("value") or {}).get("value")
            return None

        def _build(node_id: str, depth: int = 0, is_root: bool = False) -> list:
            n = by_id.get(node_id)
            if not n or depth > 60:
                return []
            children: list = []
            for cid in n.get("childIds") or []:
                children.extend(_build(cid, depth + 1))
            role = (n.get("role") or {}).get("value", "") or ""
            name = ((n.get("name") or {}).get("value", "") or "").strip()
            # Flatten layout-only (ignored) and unnamed wrapper nodes — promote their
            # children.  Keeps the tree shallow and focused on named/interactive nodes,
            # mirroring the old interesting_only=True.  The consumer already requires a
            # name to consider a node, so nothing usable is dropped.
            if not is_root and (n.get("ignored") or not name):
                return children
            val = (n.get("value") or {}).get("value")
            return [{
                "role":     role,
                "name":     name,
                "value":    "" if val is None else str(val),
                "checked":  _tri(_prop(n, "checked")),
                "expanded": _tri(_prop(n, "expanded")),
                "disabled": _tri(_prop(n, "disabled")) is True,
                "children": children,
            }]

        roots = _build(nodes[0]["nodeId"], is_root=True)
        return roots[0] if roots else {}
    except Exception as e:
        print(f"  [PLAYWRIGHT] get_aria_snapshot error: {e}")
        return {}


def text_is_present(text: str, exact: bool = False) -> bool:
    """Check whether text is visible anywhere on the current page.

    Uses DOM text query — zero screenshot, zero LLM cost.
    Replaces Claude vision / OCR for text-content validation.
    """
    page = _ensure_page()
    try:
        loc = page.get_by_text(text, exact=exact)
        return loc.count() > 0
    except Exception:
        return False


def query_element_text(selector: str) -> str:
    """Return the visible text content of the first element matching selector.

    selector: CSS selector or data-testid pattern, e.g. '[data-testid="cart-total"]'
    Returns "" on miss or error.
    """
    page = _ensure_page()
    try:
        loc = page.locator(selector)
        if loc.count() > 0:
            return (loc.first.inner_text() or "").strip()
        return ""
    except Exception:
        return ""


def text_at_point(x: int, y: int) -> str:
    """Return the visible text of the top-most DOM element at viewport point (x, y).

    Fallback anchor for value validation when an app_map element has no test id —
    reads the element the user would see under that coordinate.  "" on miss/error.
    """
    page = _ensure_page()
    try:
        return (page.evaluate(
            "([x, y]) => { const el = document.elementFromPoint(x, y);"
            " return el ? (el.innerText || el.textContent || '').trim() : ''; }",
            [x, y],
        ) or "")
    except Exception:
        return ""


def get_element_bounding_box(selector: str) -> dict | None:
    """Return pixel bounding box for the first element matching selector.

    Returns {"x": ..., "y": ..., "width": ..., "height": ..., "cx": ..., "cy": ...}
    where cx/cy are the center coordinates.  Returns None on miss.
    """
    page = _ensure_page()
    try:
        loc = page.locator(selector)
        if loc.count() == 0:
            return None
        box = loc.first.bounding_box()
        if box is None:
            return None
        box["cx"] = round(box["x"] + box["width"] / 2)
        box["cy"] = round(box["y"] + box["height"] / 2)
        return box
    except Exception:
        return None


def set_demo_screens(paths: list[str]) -> None:
    """No-op in playwright mode — real screenshots taken after every action."""
    pass


def stop() -> None:
    global _pw, _browser, _page, _progress_injected
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None
    _progress_injected = False
    print("  [PLAYWRIGHT] Browser closed")
