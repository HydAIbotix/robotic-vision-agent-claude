"""
validate_map — post-exploration self-correction pass.

Runs once, automatically, after all screens have been explored and before the
app_map is written to disk.  Three checks, in order:

  1. Duplicate detection — two screen_ids that resolved to the same DOM testid
     (can happen when Claude names a screen differently on first vs later visit).
     Fix: merge transitions/elements into the screen with more elements; delete
     the other; rewrite all cross-screen transition references.

  2. Coordinate re-validation — navigate to each screen (playwright mode) and
     re-run _dom_correct_elements() to fix any coordinates that drifted or were
     missed during the initial exploration pass.

  3. Coordinate sanity — flag elements whose centers fall outside the viewport.
     These are logged as warnings and left for human review (not auto-deleted,
     since they may be scroll-offscreen elements intentionally captured).

Prints a human-readable correction report before the final save.
"""
import time
from app_explorer.state import ExplorerState
from app_explorer.nodes.explore_screen import _dom_correct_elements

_VIEWPORT_W = 1400
_VIEWPORT_H = 900


# ── 1. Deduplication ──────────────────────────────────────────────────────────

def _dedup_screens(app_map: dict) -> tuple[dict, list[str]]:
    """Merge screens that share the same dom_id.

    Keeps whichever screen_id has more elements.  Rewrites all transition
    targets that point at the duplicate so the graph remains consistent.
    Returns (updated_app_map, list_of_fix_strings).
    """
    fixes: list[str] = []
    screens = dict(app_map.get("screens") or {})

    # Group screen_ids by dom_id
    dom_id_groups: dict[str, list[str]] = {}
    for sid, sc in screens.items():
        did = sc.get("dom_id", "")
        if did:
            dom_id_groups.setdefault(did, []).append(sid)

    for dom_id, sids in dom_id_groups.items():
        if len(sids) < 2:
            continue

        # Sort: most elements first → canonical; rest are duplicates
        sids_sorted = sorted(
            sids,
            key=lambda s: len(screens[s].get("elements") or []),
            reverse=True,
        )
        canonical, *duplicates = sids_sorted

        for dup in duplicates:
            # Merge transitions from the duplicate into the canonical screen
            dup_transitions  = dict(screens[dup].get("transitions") or {})
            canon_transitions = dict(screens[canonical].get("transitions") or {})
            canon_transitions.update(dup_transitions)
            screens[canonical] = {**screens[canonical], "transitions": canon_transitions}

            # Rewrite all transition targets that pointed at the duplicate
            for sid in list(screens.keys()):
                sc = screens[sid]
                old_t = sc.get("transitions") or {}
                new_t = {k: (canonical if v == dup else v) for k, v in old_t.items()}
                if new_t != old_t:
                    screens[sid] = {**sc, "transitions": new_t}

            del screens[dup]
            fixes.append(
                f"Merged duplicate screen_id '{dup}' → '{canonical}'"
                f" (both resolved dom_id='{dom_id}';"
                f" '{canonical}' kept — had more elements)"
            )

    # Also rewrite element_transitions
    old_et = app_map.get("element_transitions") or {}
    new_et = {eid: (canonical if dest in {dup for _, dups in dom_id_groups.items() if len(dups) > 1 for dup in dups} else dest)
              for eid, dest in old_et.items()}
    # Simpler rewrite using a dup→canonical map built from the fixes:
    dup_to_canonical: dict[str, str] = {}
    for f in fixes:
        # Extract dup and canonical from fix string
        parts = f.split("'")
        if len(parts) >= 4:
            dup_to_canonical[parts[1]] = parts[3]
    new_et = {eid: dup_to_canonical.get(dest, dest) for eid, dest in old_et.items()}

    return {**app_map, "screens": screens, "element_transitions": new_et}, fixes


# ── 2. Coordinate re-validation ───────────────────────────────────────────────

def _navigate_to(screen_id: str, sc: dict, approach_paths: dict, app_map: dict, credentials: dict) -> bool:
    """Navigate the browser to `screen_id` for coordinate re-validation.

    Tries (in order):
      a) Already there — no-op.
      b) Sidebar nav click via navigate_to_screen().
      c) Full reset + approach-path replay.

    Returns True if the browser is now on the expected screen, False on error.
    """
    from vision_agent import robot
    from app_explorer.nodes.execute_action import _run_steps, _requires_valid_login

    dom_id = sc.get("dom_id", "")
    try:
        current_dom = robot.get_dom_screen_id()
    except Exception:
        return False

    if dom_id and current_dom == dom_id:
        return True  # already there

    known_screens = app_map.get("screens") or {}
    current_sid   = next(
        (sid for sid, s in known_screens.items() if s.get("dom_id") == current_dom),
        current_dom,
    )

    # Try sidebar nav (fast — no re-login)
    if _requires_valid_login(current_sid, approach_paths) and _requires_valid_login(screen_id, approach_paths):
        try:
            if robot.navigate_to_screen(screen_id):
                time.sleep(0.4)
                return True
        except AttributeError:
            pass

    # Full reset + replay
    try:
        from vision_agent import robot as _robot
        _robot.reset_to_entry()
        time.sleep(0.8)
        for past_action in approach_paths.get(screen_id, []):
            _run_steps(past_action["steps"], past_action["screen_id"], app_map, credentials)
            time.sleep(0.5)
        return True
    except Exception as e:
        print(f"  [VALIDATE] Cannot navigate to '{screen_id}': {e}")
        return False


def _revalidate_coordinates(app_map: dict, approach_paths: dict, credentials: dict) -> tuple[dict, list[str]]:
    """Navigate to every screen and re-run DOM coordinate correction.

    Only does anything in playwright mode — get_dom_element_centers() returns []
    in demo/real modes, so _dom_correct_elements() returns the elements unchanged.
    """
    from vision_agent import robot

    # Quick check: are we even in playwright mode?
    try:
        dom_els = robot.get_dom_element_centers()
    except AttributeError:
        return app_map, []
    if dom_els is None:
        return app_map, []

    fixes: list[str] = []
    screens = dict(app_map.get("screens") or {})

    for screen_id, sc in list(screens.items()):
        old_elements = list(sc.get("elements") or [])
        if not old_elements:
            continue

        ok = _navigate_to(screen_id, sc, approach_paths, app_map, credentials)
        if not ok:
            continue

        time.sleep(0.4)
        new_elements = _dom_correct_elements(old_elements)

        changed = sum(
            1 for old_el, new_el in zip(old_elements, new_elements)
            if old_el.get("center") != new_el.get("center")
            or old_el.get("testid") != new_el.get("testid")
        )

        if changed:
            screens[screen_id] = {**sc, "elements": new_elements}
            fixes.append(f"'{screen_id}': updated {changed} element(s) (coords or testid)")

    return {**app_map, "screens": screens}, fixes


# ── 3. Coordinate sanity ─────────────────────────────────────────────────────

def _sanity_warnings(app_map: dict) -> list[str]:
    """Return warnings for elements whose centers are outside the viewport bounds."""
    warnings: list[str] = []
    for sid, sc in (app_map.get("screens") or {}).items():
        for el in (sc.get("elements") or []):
            cx, cy = el.get("center") or [0, 0]
            if not (0 <= cx <= _VIEWPORT_W and 0 <= cy <= _VIEWPORT_H):
                warnings.append(
                    f"  WARN  {sid}/{el.get('id')}: center ({cx},{cy}) is outside"
                    f" {_VIEWPORT_W}×{_VIEWPORT_H} viewport"
                )
    return warnings


# ── Node entry point ─────────────────────────────────────────────────────────

def validate_map(state: ExplorerState) -> dict:
    """Self-correction pass: run after all screens explored, before final save."""
    app_map        = dict(state["app_map"])
    approach_paths = state.get("approach_paths") or {}
    credentials    = state.get("credentials") or {}

    screen_count_before = len(app_map.get("screens") or {})
    elem_count_before   = sum(
        len(sc.get("elements") or []) for sc in (app_map.get("screens") or {}).values()
    )

    print("\n  ══════════════════════════════════════════════════════")
    print("  [VALIDATE] Self-correction pass starting…")
    print(f"             {screen_count_before} screens  |  {elem_count_before} elements")
    print("  ══════════════════════════════════════════════════════")

    # 1. Deduplication
    app_map, dedup_fixes = _dedup_screens(app_map)
    for f in dedup_fixes:
        print(f"  [VALIDATE] FIX  — {f}")

    # 2. Coordinate re-validation (playwright only — no-op in demo/real)
    app_map, coord_fixes = _revalidate_coordinates(app_map, approach_paths, credentials)
    for f in coord_fixes:
        print(f"  [VALIDATE] FIX  — {f}")

    # 3. Sanity warnings
    warnings = _sanity_warnings(app_map)
    for w in warnings:
        print(f"  [VALIDATE]{w}")

    # Summary
    screen_count_after = len(app_map.get("screens") or {})
    elem_count_after   = sum(
        len(sc.get("elements") or []) for sc in (app_map.get("screens") or {}).values()
    )
    total_fixes = len(dedup_fixes) + len(coord_fixes)

    print("  ══════════════════════════════════════════════════════")
    print(f"  [VALIDATE] Done.  fixes={total_fixes}  warnings={len(warnings)}")
    print(f"             Screens: {screen_count_before} → {screen_count_after}"
          f"   |   Elements: {elem_count_before} → {elem_count_after}")
    print("  ══════════════════════════════════════════════════════\n")

    return {"app_map": app_map}
