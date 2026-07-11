"""
plan_normalize — deterministic, generic post-generation safety nets for structured plans.

Planners (both the UI `_TC_PLAN_PROMPT` and the runtime `PLAN_FROM_MAP`) turn human test steps
into structured plans.  A partially-charted screen can trick the LLM into "completing" a sub-task
with a plausible-but-wrong charted element instead of doing what the step actually requires.  The
observed case: a test says "pay with the SAME card issued earlier", the card-number INPUT on the
payment screen was never charted by the App Explorer (only the completion buttons were), so the
planner emitted `tap use_mock_card_button` and NEVER entered the captured card number — the payment
silently did nothing (an app_map tap always "passes"), and multi-payment flows desynced.

`normalize_captured_reuse` is a deterministic net for that class of bug (generic — any reused
runtime value, not just cards): when a completion/submit tap is meant to REUSE a value captured
earlier, but the plan never ENTERS that value on the tap's screen and the screen has no charted
input to receive it, the tap is rewritten to a `vision_required` step that enters `{{captured.NAME}}`
and completes — the runtime's live-vision path handles the uncharted input.  It is:
  • idempotent  — a step already `vision_required` (or already entering `{{captured.*}}`) is left alone;
  • conservative — fires ONLY when a capture actually preceded the tap, the description signals
    both reuse AND completion, the value is not entered on that screen, and the screen has no input
    element charted (so entering it genuinely needs vision) — minimising false positives;
  • backend-agnostic — it only edits the plan; execution (playwright + real robot) is unchanged.
"""
import re

# A step REUSES a specific value captured earlier (not a fresh/generic value) — signalled by the
# description. Kept deliberately specific ("same"/"issued"/"reuse"/"that card/code/number"/"captured")
# so a generic "use the mock card option to load $600" (a FIRST use, no reuse) does NOT match.
_REUSE_RE = re.compile(
    r"\b(same|identical|issued|reuse|re-?enter|already[- ]captured|captured|"
    r"that\s+(card|code|number|value)|generated\s+(card|code|number))\b",
    re.I,
)
# The tap is a payment / completion / submit that should CONSUME the value.
_COMPLETE_RE = re.compile(
    r"\b(pay|payment|mock\s*card|complete|confirm|checkout|check\s*out|submit|charge|process|"
    r"place\s+order|finish)\b",
    re.I,
)
# Element-id fallback signal for a completion control (when the description is terse).
_COMPLETE_ELEM_RE = re.compile(
    r"(pay|payment|mock.?card|complete|confirm|checkout|submit|charge|place.?order)",
    re.I,
)
# App-map element types that can RECEIVE a typed value.
_INPUT_TYPES = {"input", "textbox", "textarea", "field", "search", "email", "password", "number"}


def _screen_input_ids(app_map: dict | None, screen_id: str) -> list[str]:
    """Ids of input-like elements charted on this screen (empty if none / unknown screen)."""
    sc = ((app_map or {}).get("screens") or {}).get(screen_id) or {}
    return [e.get("id", "") for e in (sc.get("elements") or [])
            if (e.get("type") or "").lower() in _INPUT_TYPES]


def normalize_captured_reuse(plan: dict, app_map: dict | None) -> tuple[dict, list[str]]:
    """Rewrite reuse-of-captured-value completion taps that bypass the value into vision_required.

    Returns (plan, notes).  `plan` is mutated in place and also returned; `notes` is a list of
    human-readable strings describing each conversion (empty when nothing changed)."""
    if not isinstance(plan, dict):
        return plan, []
    steps = plan.get("steps") or []
    if not steps:
        return plan, []

    # Screens on which a {{captured.*}} value IS entered by a `type` step — those are correctly
    # planned, so a completion tap on the same screen is NOT the bug we target.
    typed_capture_screens: set[str] = set()
    for s in steps:
        if s.get("action") == "type" and "{{captured." in str(s.get("value", "") or ""):
            sid = s.get("screen_id")
            if sid:
                typed_capture_screens.add(sid)

    captured_so_far: list[str] = []   # capture_as names seen BEFORE the current step (ordering matters)
    notes: list[str] = []

    for idx, s in enumerate(steps):
        action = s.get("action")
        if action == "capture":
            nm = s.get("capture_as") or s.get("element_id")
            if nm:
                captured_so_far.append(nm)
            continue
        if action != "tap":
            continue
        if not captured_so_far:
            continue   # nothing captured yet → this tap cannot be reusing a captured value

        desc = str(s.get("description", "") or "")
        elem = str(s.get("element_id", "") or "")
        names_in_desc = [n for n in captured_so_far if n and n.lower() in desc.lower()]
        reuse = bool(_REUSE_RE.search(desc)) or bool(names_in_desc)
        completion = bool(_COMPLETE_RE.search(desc)) or bool(_COMPLETE_ELEM_RE.search(elem))
        if not (reuse and completion):
            continue

        sid = s.get("screen_id", "")
        # The value is already entered on this screen by a type step → correctly planned, leave it.
        if sid in typed_capture_screens:
            continue
        # The screen HAS a charted input for the value → the planner could/should type it; be
        # conservative and don't convert (avoid disturbing a genuinely-charted flow).
        if _screen_input_ids(app_map, sid):
            continue

        # → Bug pattern: a completion tap meant to reuse a captured value, but the value is never
        #   entered and the screen has no input charted to receive it. Convert to vision_required so
        #   the runtime enters {{captured.NAME}} with live vision, then completes.
        name = names_in_desc[0] if names_in_desc else captured_so_far[-1]
        placeholder = "{{captured.%s}}" % name
        new_desc = (
            f"Enter {placeholder} into the payment/card field on the {sid or 'current'} screen and "
            f"complete this step using that SAME captured value (the screen has no charted input for "
            f"it, so live vision must enter it). Original intent: {desc}"
        )
        # Drop the misbound charted element/coords; keep device/channel/screen_id for context/routing.
        for k in ("element_id", "px", "py"):
            s.pop(k, None)
        s["action"] = "vision_required"
        s["description"] = new_desc
        notes.append(
            f"step {idx + 1}: tap '{elem}' on '{sid}' → vision_required "
            f"(reuse captured '{name}'; screen has no charted input for it)"
        )

    return plan, notes
