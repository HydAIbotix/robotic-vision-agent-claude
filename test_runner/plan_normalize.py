"""
plan_normalize — deterministic, generic post-generation safety nets for structured plans.

Planners (both the UI `_TC_PLAN_PROMPT` and the runtime `PLAN_FROM_MAP`) turn human test steps
into structured plans.  A PARTIALLY-charted screen makes the LLM inconsistent for the SAME flow: on
a payment screen where the App Explorer charted the completion button (e.g. "Use Mock Card") but NOT
the transient card-number input, one plan emits `tap use_mock_card_button` + `vision_required(enter
the captured card + complete)` (works), while another emits a LONE `vision_required(enter + complete)`
(fragile — live vision then has to pick the completion button itself and can tap the WRONG one, e.g.
BOTH "Use Mock Card" AND "Start Card Reader Session", so the order silently fails).  Observed across
TC-E2E-001 (passes) vs TC-E2E-003 (fails) for an identical first-payment sub-flow.

`normalize_captured_reuse` makes EVERY reuse-of-a-captured-value payment converge to ONE canonical
shape — the SAME order proven live by TC-E2E-001 — so similar tests behave identically
("fix one → fixes all"):

    [ tap <charted completion button> ]    ← deterministic method selection (e.g. tap "Use Mock Card"),
                                             which reveals the mock-card entry field
    [ vision_required  enter + complete ]  ← live vision types {{captured.NAME}} into the (uncharted)
                                             field that appears and confirms the payment

The completion button is tapped FIRST and DETERMINISTICALLY (from the app-map element/coords), so live
vision never has to choose between two method buttons — the double method-button tap is impossible.
On the real RPS app, "Use Mock Card" must be tapped BEFORE the card number is entered (it initiates
the mock-card flow / reveals the field); entering first and tapping after does NOT complete the order.
The canonical shape is produced from ANY variant the LLM emits — a lone combined `vision_required`, a
lone reuse completion `tap`, the pair already emitted (either order), or a prior wrong-order
`[vision][tap]`.

It is:
  • idempotent  — a step already produced by this normaliser (marked `reuse_norm`) is left alone;
  • conservative — fires ONLY when a capture actually preceded the step, the description signals both
    reuse AND completion, the value is not entered on that screen, the screen has NO charted input for
    it, AND a charted completion button exists on that screen to tap deterministically;
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
# The step is a payment / completion / submit that should CONSUME the value.
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
# App-map element types that can be TAPPED to complete.
_BUTTON_TYPES = {"button", "link", "submit"}
# Positive completion signals (label/id/description) used to score a charted completion button.
_MOCKCARD_RE = re.compile(r"mock\s*card", re.I)
_COMPLETE_WORD_RE = re.compile(
    r"\b(complete|confirm|pay|payment|place\s*order|checkout|check\s*out|submit|charge|process|finish)\b",
    re.I,
)
_APPLY_RE = re.compile(r"\bapply\b", re.I)
# Buttons that AWAIT input (start a reader session, wait for a tap) rather than COMPLETE — these must
# never be chosen as the deterministic completion control.
_AWAIT_RE = re.compile(r"(reader|session|scan|await|start\s*card|tap\s*(a|your)?\s*card)", re.I)


def _screen_input_ids(app_map: dict | None, screen_id: str) -> list[str]:
    """Ids of input-like elements charted on this screen (empty if none / unknown screen)."""
    sc = ((app_map or {}).get("screens") or {}).get(screen_id) or {}
    return [e.get("id", "") for e in (sc.get("elements") or [])
            if (e.get("type") or "").lower() in _INPUT_TYPES]


def _element_center(e: dict) -> tuple[int, int] | None:
    """Pixel center of a charted element (from `center`, else derived from `bbox`)."""
    c = e.get("center") or []
    if len(c) >= 2:
        return int(c[0]), int(c[1])
    bb = e.get("bbox") or []
    if len(bb) == 4:
        return int((bb[0] + bb[2]) / 2), int((bb[1] + bb[3]) / 2)
    return None


def _find_completion_element(app_map: dict | None, screen_id: str) -> dict | None:
    """The charted button/link on this screen that COMPLETES a payment (e.g. "Use Mock Card"), or None.

    Scores candidates by completion signal in id+label+description; a pure await-input control
    (card-reader / session / "tap your card") is excluded so we never pick it as the completion."""
    sc = ((app_map or {}).get("screens") or {}).get(screen_id) or {}
    best, best_score = None, 0
    for e in (sc.get("elements") or []):
        if (e.get("type") or "").lower() not in _BUTTON_TYPES:
            continue
        hay = " ".join(str(e.get(k, "")) for k in ("id", "label", "description"))
        completion_signal = bool(_MOCKCARD_RE.search(hay) or _COMPLETE_WORD_RE.search(hay) or _APPLY_RE.search(hay))
        # A pure await-input button (no completion wording at all) can never be the completion.
        if _AWAIT_RE.search(hay) and not completion_signal:
            continue
        if not completion_signal:
            continue
        score = 0
        if _MOCKCARD_RE.search(hay):
            score += 3
        if _COMPLETE_WORD_RE.search(hay):
            score += 2
        if _APPLY_RE.search(hay):
            score += 1
        if _AWAIT_RE.search(hay):            # ambiguous (matches both) → de-prioritise vs a pure completion
            score -= 2
        if score <= 0:
            continue
        center = _element_center(e)
        if center is None:
            continue
        if score > best_score:
            best_score = score
            best = {"id": e.get("id", ""), "cx": center[0], "cy": center[1],
                    "label": e.get("label") or e.get("id", "")}
    return best


def _is_completion_tap(step: dict, B: dict) -> bool:
    return bool(B) and step.get("action") == "tap" and step.get("element_id") == B.get("id")


def _is_reuse_vision(step: dict, captured_names: list[str]) -> bool:
    """A vision_required step that REUSES a captured value to COMPLETE a payment."""
    if step.get("action") != "vision_required":
        return False
    desc = str(step.get("description", "") or "")
    names_hit = any(nm and nm.lower() in desc.lower() for nm in captured_names)
    reuse = bool(_REUSE_RE.search(desc)) or names_hit
    completion = bool(_COMPLETE_RE.search(desc))
    return reuse and completion


def _completion_tap_step(B: dict, screen_id: str, device: str) -> dict:
    """Deterministic tap on the charted completion button — tapped FIRST to start the mock-card flow."""
    step: dict = {"action": "tap", "channel": "robot", "reuse_norm": True}
    if device:
        step["device"] = device
    if screen_id:
        step["screen_id"] = screen_id
    step["element_id"] = B["id"]
    step["px"] = B["cx"]
    step["py"] = B["cy"]
    step["description"] = f"Tap '{B['label']}' to start the mock-card payment (reveals the card field)"
    return step


def _reuse_vision_step(name: str, screen_id: str, device: str) -> dict:
    """Live-vision step that enters {{captured.NAME}} into the revealed field and completes the order.

    Runs AFTER the deterministic completion-button tap, so vision only has to enter the card number
    and confirm in the mock-card form — it never has to choose between two method buttons."""
    placeholder = "{{captured.%s}}" % name
    step: dict = {"action": "vision_required", "reuse_norm": True}
    if device:
        step["device"] = device
    if screen_id:
        step["screen_id"] = screen_id
    step["description"] = (
        f"Enter {placeholder} into the card/payment input field that is now shown (the mock-card "
        f"payment was just started) and complete/confirm the payment with that SAME captured value. "
        f"If an error from a previous attempt is visible, ignore it (it is stale) — enter the value "
        f"and confirm; judge the result AFTER confirming."
    )
    return step


def normalize_captured_reuse(plan: dict, app_map: dict | None) -> tuple[dict, list[str]]:
    """Canonicalise reuse-of-captured-value payments to [deterministic completion tap] + [enter+complete vision].

    Returns (plan, notes).  `plan` is mutated in place and also returned; `notes` is a list of
    human-readable strings describing each canonicalisation (empty when nothing changed)."""
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
    out: list[dict] = []
    n = len(steps)
    i = 0

    while i < n:
        s = steps[i]
        action = s.get("action")

        if action == "capture":
            nm = s.get("capture_as") or s.get("element_id")
            if nm:
                captured_so_far.append(nm)
            out.append(s)
            i += 1
            continue

        # Already-canonical step produced by a previous run of this normaliser: inert (idempotent).
        if s.get("reuse_norm"):
            out.append(s)
            i += 1
            continue

        desc = str(s.get("description", "") or "")
        elem = str(s.get("element_id", "") or "")
        sid = s.get("screen_id", "") or ""
        device = s.get("device", "") or ""
        names_in_desc = [nm for nm in captured_so_far if nm and nm.lower() in desc.lower()]
        reuse = bool(_REUSE_RE.search(desc)) or bool(names_in_desc)
        completion = bool(_COMPLETE_RE.search(desc)) or bool(_COMPLETE_ELEM_RE.search(elem))

        qualifies = (
            bool(captured_so_far) and reuse and completion
            and sid not in typed_capture_screens
            and not _screen_input_ids(app_map, sid)
        )
        # Need a charted completion button on THIS screen to tap deterministically; without one we
        # cannot canonicalise (nothing to tap) — leave the step as the planner emitted it.
        B = _find_completion_element(app_map, sid) if qualifies else None

        if qualifies and B and action in ("vision_required", "tap"):
            name = names_in_desc[0] if names_in_desc else captured_so_far[-1]
            # Absorb an adjacent completion tap the LLM (or a prior wrong-order normalisation) emitted,
            # in EITHER position, so we can re-emit the canonical order [tap] then [vision].
            if out and _is_completion_tap(out[-1], B):
                out.pop()                                   # drop a preceding [tap B]  (e.g. TC-E2E-001 shape)
            if i + 1 < n and _is_completion_tap(steps[i + 1], B):
                i += 1                                      # drop a following [tap B]  (prior [vision][tap] order)
            # Absorb an adjacent reuse vision if THIS step was the tap (so we don't emit two visions).
            if action == "tap" and i + 1 < n and _is_reuse_vision(steps[i + 1], captured_so_far):
                i += 1
            out.append(_completion_tap_step(B, sid, device))    # 1) deterministic method tap FIRST
            out.append(_reuse_vision_step(name, sid, device))   # 2) THEN enter the captured value + complete
            kind = "vision_required" if action == "vision_required" else f"tap '{elem}'"
            notes.append(
                f"step {i + 1}: reuse {kind} on '{sid}' → [deterministic tap '{B['id']}'] + "
                f"[vision enters {{{{captured.{name}}}}} and completes] (tap first, matches TC-E2E-001)"
            )
            i += 1
            continue

        out.append(s)
        i += 1

    plan["steps"] = out
    return plan, notes
