"""
AppMap — the persistent knowledge graph of the application under test.

Produced once by the AppExplorer and consumed by every subsequent test run.
Stored as plain JSON so it can be inspected, version-controlled, and loaded
by the test runner without re-running exploration.
"""
import json
import time
from pathlib import Path
from typing import Optional
from typing_extensions import TypedDict
from vision_agent.state import ScreenElement


class AppScreen(TypedDict):
    screen_id: str
    description: str
    elements: list[ScreenElement]          # full analyzed element list
    transitions: dict[str, str]            # {action_key: resulting_screen_id}


class AppMap(TypedDict):
    app_name: str
    explored_at: str
    entry_screen: str
    screens: dict[str, AppScreen]          # keyed by screen_id


def empty(app_name: str, entry_screen: str) -> AppMap:
    return {
        "app_name": app_name,
        "explored_at": "",
        "entry_screen": entry_screen,
        "screens": {},
    }


def save(app_map: AppMap, path: str) -> None:
    out = {**app_map, "explored_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    print(f"\n  [APP MAP] Saved to {path}  ({len(out['screens'])} screens)")
    # Mirror the explorer's output (app_map + its co-located screenshots) to the durable object
    # store (MinIO/S3) when ARCHIVE_TO_OBJECT_STORE is on. No-op otherwise; failure-isolated.
    try:
        from ports.archive import archive
        archive(str(dest))
        archive(str(dest.parent / "screenshots"))
    except Exception:
        pass


def load(path: str) -> AppMap:
    return json.loads(Path(path).read_text())


def merge_explored_app(existing: Optional[dict], new_map: dict, app_id: str,
                       app_label: str = "", app_url: str = "") -> dict:
    """Merge a freshly-explored app's screens into the combined multi-app map.

    Each screen is tagged with its ``app_id`` and screens belonging to OTHER apps are
    preserved, so exploring a second kiosk app never wipes the first.  Re-exploring the
    same ``app_id`` replaces only that app's screens.  A top-level ``apps`` registry
    records each explored app (label, entry screen, screen count).

    When ``app_id`` is blank (legacy single-app), the new map replaces the old one —
    identical to the previous behaviour, so nothing changes for single-app users.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    new_screens = dict(new_map.get("screens") or {})

    if not app_id:
        return {**new_map, "explored_at": stamp}

    base    = dict(existing or {})
    screens = dict(base.get("screens") or {})
    apps    = dict(base.get("apps") or {})

    # Keep other apps' screens; drop this app's previous screens (fresh re-exploration).
    screens = {sid: sc for sid, sc in screens.items() if (sc.get("app_id") or "") != app_id}
    for sid, sc in new_screens.items():
        if sid in screens:
            print(f"  [APP MAP] ⚠ screen id '{sid}' already exists in another app — overwriting")
        screens[sid] = {**sc, "app_id": app_id}

    apps[app_id] = {
        "app_id":       app_id,
        "label":        app_label or new_map.get("app_name") or app_id,
        "entry_screen": new_map.get("entry_screen", ""),
        # Remember the URL this app was explored from so the whole lifecycle can reuse it and the
        # App Map view can show it (falls back to any previously-recorded url on re-explore).
        "url":          app_url or (apps.get(app_id) or {}).get("url", ""),
        "explored_at":  stamp,
        "screen_count": len(new_screens),
    }
    merged = {
        # Preserve every OTHER top-level key already on the combined map (keyboard_map, and any
        # future top-level data) so a multi-app merge never silently drops it.  Previously this
        # return listed only app_name/explored_at/entry_screen/screens/apps, which discarded the
        # top-level `keyboard_map` produced by the explorer — leaving real-robot type_text with no
        # keys to tap (observed: TC-RPS-001 typed nothing).
        **{k: v for k, v in base.items() if k not in ("screens", "apps")},
        "app_name":     base.get("app_name") or "Multi-App Environment",
        "explored_at":  stamp,
        # Prefer the just-explored app's entry over a stale seeded one; per-app entries live
        # in apps[app_id]. (The top-level value is only a sensible default for the UI.)
        "entry_screen": new_map.get("entry_screen") or base.get("entry_screen", ""),
        "screens":      screens,
        "apps":         apps,
    }
    # The virtual-keyboard map is environment-wide (one shared on-screen keyboard).  A fresh
    # exploration that mapped it wins; otherwise keep whatever the combined map already had.
    new_kb = new_map.get("keyboard_map")
    if new_kb:
        merged["keyboard_map"] = new_kb
    return merged


def remove_app(existing: Optional[dict], app_id: str) -> Optional[dict]:
    """Remove one app's screens + its registry entry from the combined multi-app map.

    Returns the updated map, or None when nothing remains (caller should delete the file).
    A blank app_id, or an app that owns every screen, clears everything → returns None.
    """
    if not existing or not app_id:
        return None
    base    = dict(existing)
    screens = {sid: sc for sid, sc in (base.get("screens") or {}).items()
               if (sc.get("app_id") or "") != app_id}
    apps    = {aid: a for aid, a in (base.get("apps") or {}).items() if aid != app_id}
    if not screens:
        return None
    return {
        **base,
        "screens":     screens,
        "apps":        apps,
        "explored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def scoped_to_apps(app_map: Optional[dict], app_ids) -> Optional[dict]:
    """Return a copy of the multi-app map containing ONLY the given apps' screens.

    The general form of ``scoped_to_app`` — accepts one or MORE app_ids so a CROSS-KIOSK test
    (e.g. an E2E flow that loads a card at VPS then buys at RPS) is planned against the UNION of
    exactly the kiosks it touches, and no others.  A single-kiosk test passes one id and behaves
    identically to before.  All top-level keys (keyboard_map, apps, etc.) are preserved; only
    ``screens`` is filtered and ``entry_screen`` is set to the FIRST requested app's entry.

    No-op (returns the map unchanged) when:
      - app_ids is empty, or
      - the map has no per-screen app_id tags (legacy single-app map), or
      - no screen matches any requested app_id (avoid an empty map that would break planning).
    """
    if not app_map:
        return app_map
    wanted = [a for a in ([app_ids] if isinstance(app_ids, str) else (app_ids or [])) if a]
    if not wanted:
        return app_map
    wanted_set = set(wanted)
    screens = app_map.get("screens") or {}
    tagged  = {sid: sc for sid, sc in screens.items() if (sc.get("app_id") or "") in wanted_set}
    if not tagged:
        return app_map
    apps = app_map.get("apps") or {}
    # entry_screen = the first requested app's own entry (fall back to any tagged screen).
    app_entry = ""
    for aid in wanted:
        if aid in apps and apps[aid].get("entry_screen") in tagged:
            app_entry = apps[aid]["entry_screen"]
            break
    if app_entry not in tagged:
        app_entry = next(iter(tagged))
    return {**app_map, "screens": tagged, "entry_screen": app_entry}


def scoped_to_app(app_map: Optional[dict], app_id: str) -> Optional[dict]:
    """Single-app convenience wrapper around scoped_to_apps (kept for existing call sites)."""
    return scoped_to_apps(app_map, app_id)


def prompt_summary(app_map: Optional[AppMap]) -> str:
    """Compact text representation for including in LLM prompts."""
    if not app_map:
        return "No app map available — agent will identify screens and elements dynamically from screenshots."
    lines = [f"App: {app_map['app_name']}  entry={app_map['entry_screen']}"]
    for sid, sc in app_map["screens"].items():
        lines.append(f"\n  [{sid}] {sc['description']}")
        els = "  |  ".join(
            f"{e['id']}({e['type']})" for e in sc.get("elements") or []
        )
        lines.append(f"    elements: {els}")
        for ak, nxt in (sc.get("transitions") or {}).items():
            lines.append(f"    {ak} --> {nxt}")
    return "\n".join(lines)


def element_inventory_for_prompt(app_map: Optional[AppMap]) -> str:
    """Rich per-element text format for Tier-2 test planning.

    Includes pixel coordinates, element types, labels, and descriptions so
    Claude can plan concrete tap/type steps without seeing any screenshot.
    This IS the visual description of the app — extracted once by App Explorer.
    """
    if not app_map:
        return ""
    lines = [
        f"App: {app_map.get('app_name', 'unknown')}",
        f"Entry screen: {app_map.get('entry_screen', 'unknown')}",
        "",
    ]
    # When the map spans multiple apps/kiosks, tell the planner which app each screen belongs to so
    # it can tag cross-kiosk steps with the right device and move between apps in the right order.
    _multi_app = len({(sc.get("app_id") or "") for sc in (app_map.get("screens") or {}).values()} - {""}) > 1
    for sid, sc in (app_map.get("screens") or {}).items():
        _app = f"  (app/kiosk: {sc.get('app_id')})" if (_multi_app and sc.get("app_id")) else ""
        lines.append(f"SCREEN: {sid}{_app}")
        lines.append(f"  Description: {sc.get('description', '')}")
        lines.append("  Interactive elements:")
        for el in sc.get("elements") or []:
            cx, cy = el.get("center") or [0, 0]
            lines.append(
                f"    - id={el['id']!r}  type={el.get('type', '?')}  "
                f"label={el.get('label', '')!r}  "
                f"coords=({int(cx)},{int(cy)})  "
                f"note={el.get('description', '')!r}"
            )
        transitions = sc.get("transitions") or {}
        if transitions:
            lines.append("  Navigation transitions:")
            for ak, nxt in transitions.items():
                lines.append(f"    {ak}  →  {nxt}")
        # Observed element dependencies (ground truth captured during exploration):
        # which action elements require prior state, and the recipe to satisfy it.
        deps = sc.get("dependencies") or []
        if deps:
            lines.append("  Observed prerequisites (MUST honor — discovered during exploration):")
            for d in deps:
                req = ", ".join(d.get("requires") or [])
                tag = " [CONFIRMED BY EXECUTION]" if d.get("observed") else " [inferred from screen]"
                lines.append(f"    '{d.get('element_id','')}' requires [{req}]{tag} — {d.get('reason','')}")
                pre = d.get("prerequisite_steps") or []
                if pre:
                    recipe = " → ".join(
                        f"{s.get('action_type','tap')}:{s.get('element_id','')}" for s in pre
                    )
                    lines.append(f"        prerequisite steps: {recipe} → (then tap '{d.get('element_id','')}')")
        lines.append("")
    return "\n".join(lines)


_STEPPER_HINTS = ("increase", "increment", "decrease", "decrement", "quantity", "qty", "_plus", "_minus")


def _looks_like_stepper(el_id: str, el_type: str) -> bool:
    return el_type == "stepper" or any(h in (el_id or "").lower() for h in _STEPPER_HINTS)


def _looks_like_add_to_cart(el_id: str) -> bool:
    low = (el_id or "").lower()
    return "add" in low and any(k in low for k in ("cart", "bag", "basket"))


def sane_dependency(dep: dict, elements: list) -> bool:
    """Sanity-check an inferred element dependency; reject logically-invalid ones.

    Guards against a recurring LLM hallucination: pairing a QUANTITY STEPPER (+/−) with a
    consumer that does not read a per-item quantity.  A stepper's only legitimate consumer is
    that item's Add-to-Cart (which reads the quantity).  A "proceed / checkout / pay" button
    only needs a NON-EMPTY cart — an aggregate flow state, satisfied by the add-to-cart, NOT a
    same-screen quantity increment.  So "proceed_to_payment requires increase_quantity" is
    invalid and would (wrongly) make the planner bump the quantity before checkout.

    This is a generic data-quality invariant, not app-specific logic.
    """
    types = {e.get("id"): e.get("type", "") for e in (elements or [])}
    consumer = dep.get("element_id", "")
    for r in dep.get("requires") or []:
        if _looks_like_stepper(r, types.get(r, "")) and not _looks_like_add_to_cart(consumer):
            return False
    return True


def get_element(app_map: Optional[AppMap], screen_id: str, element_id: str) -> Optional[dict]:
    """Return an element dict from the map, or None if not found."""
    if not app_map:
        return None
    sc = (app_map.get("screens") or {}).get(screen_id)
    if not sc:
        return None
    return next((e for e in (sc.get("elements") or []) if e.get("id") == element_id), None)


def version_hash(app_map: Optional[AppMap]) -> str:
    """Return a short hash representing the app_map's content version.

    Used as part of the plan cache key so cached plans are automatically
    invalidated when App Explorer re-runs and the map changes.
    """
    if not app_map:
        return "no_map"
    explored_at = app_map.get("explored_at", "")
    screen_ids  = ",".join(sorted((app_map.get("screens") or {}).keys()))
    import hashlib
    return hashlib.md5(f"{explored_at}|{screen_ids}".encode()).hexdigest()[:10]


# Module-level cache: explored_at timestamp → {screen_id: hex_hash_str}
_HASH_CACHE: dict[str, dict[str, str]] = {}


def get_phash_cache(app_map: Optional[AppMap]) -> dict[str, str]:
    """Return {screen_id: hex_hash} extracted from stored screen_hash values in app_map.

    The cache is keyed by explored_at so it auto-invalidates when Explorer re-runs.
    All validate calls in a single test run share the same dict — no repeated lookups.
    """
    key = ((app_map or {}).get("explored_at") or "")
    if key and key in _HASH_CACHE:
        return _HASH_CACHE[key]

    cache: dict[str, str] = {}
    for sid, sc in ((app_map or {}).get("screens") or {}).items():
        h = (sc or {}).get("screen_hash", "")
        if h:
            cache[sid] = h

    if key:
        _HASH_CACHE[key] = cache
    return cache
