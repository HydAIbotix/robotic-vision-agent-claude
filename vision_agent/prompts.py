ANALYZE_SCREEN = """\
Analyze this UI screenshot and identify every visible interactive element.
This may be a touchscreen kiosk, web app, mobile app, or any other UI.

Return ONLY valid JSON — no markdown fences, no explanation:
{
  "screen_id": "<descriptive snake_case identifier inferred from the screen content, e.g. login, product_list, shopping_cart, card_payment_reader, order_confirmation, checkout_summary, loyalty_dashboard>",
  "description": "<one sentence describing this screen's purpose>",
  "elements": [
    {
      "id": "<short snake_case id derived from the element label, e.g. sign_in_button, email_input, use_mock_approval_button>",
      "type": "<button|input|text|link|image|dropdown|stepper|checkbox|radio|toggle>",
      "label": "<the exact visible text, placeholder, or aria-label of this element>",
      "description": "<what happens when tapped, clicked, or typed into>",
      "bbox": [x1_norm, y1_norm, x2_norm, y2_norm],
      "center": [cx_norm, cy_norm],
      "confidence": 0.95
    }
  ]
}

Coordinate rules (all values NORMALIZED 0.0–1.0):
- 0.0 = left/top edge, 1.0 = right/bottom edge
- "center" is the EXACT tap point — the visual midpoint of the interactive hit area
- "confidence" rates certainty about the center coordinates:
    0.90–1.00 : boundary clearly visible, center unambiguous
    0.70–0.89 : visible but edges soft, partially occluded, or very small
    below 0.70 : inferred from context, coordinates are estimated

Content rules:
- Include ALL interactive elements: buttons, inputs, links, quantity ±, nav items, tabs, toggles
- Generate snake_case IDs that faithfully reflect the label text (no abbreviation or invention)
  e.g. label "Use Mock Approval / Complete Order" → id "use_mock_approval_complete_order_button"
- For long labels, include the most distinctive words, not a truncation
- Omit purely decorative text, dividers, and background images
- For quantity steppers include + and − as separate elements
- EXCLUDE on-screen / virtual keyboard keys — text is typed directly; keys are not needed
- Limit to the 25 most task-relevant elements if more are present
"""

CORRECT_ELEMENT_COORD = """\
During screen analysis I found this element with low coordinate confidence:

  Element ID : {element_id}
  Type       : {element_type}
  Label      : "{label}"
  Proposed center (normalized 0.0-1.0): [{cx}, {cy}]
  Confidence : {confidence:.2f}

Look carefully at the screenshot. Locate the element labeled "{label}" ({element_type}).
Is [{cx}, {cy}] the correct normalized tap center for this element?

Return ONLY valid JSON — no markdown fences:
{{
  "confirmed": <true|false>,
  "center": [cx_norm, cy_norm],
  "confidence": <0.0-1.0>,
  "reason": "<brief explanation — what is at the proposed point vs where the element actually is>"
}}
"""

PLAN_STEPS = """\
You control an automation agent (robotic arm or browser driver) that interacts with a UI.

Task: {task_description}

Current screen: [{screen_id}] {screen_description}

Available elements on this screen:
{elements}

Decompose the task into the minimum ordered atomic steps using ONLY these action forms:
  tap: <element_id>          — interact with an element by its exact id
  type: <text to enter>      — type text into the currently focused field
  verify: <condition>        — assert a visible condition or piece of text is present

STRICT RULES:
1. ONLY generate "tap:" steps for element IDs that appear verbatim in "Available elements" above.
   Do NOT invent element IDs, guess button names, or reference elements that are not listed.
2. If the task requires an element that is NOT visible on this screen, end your plan with:
   "verify: <describe what you need to see next>" — and stop. Do not add more steps after that.
3. Match element IDs exactly as listed — copy them character-for-character.
4. One action per array item. Return ONLY a JSON array of strings.
5. PRECONDITION REASONING (critical — you are often here because a previous step failed):
   Before tapping a "proceed", "checkout", "continue", "pay", "confirm", or "submit" button,
   check that its precondition is met on THIS screen. These buttons do nothing when the required
   state is missing, and tapping them again will fail exactly as before.
   - Cart/checkout with an empty cart or a "(0)" badge → first add an item. If a quantity
     stepper "+"/increment element exists, tap it to raise the quantity to at least 1, THEN
     tap add-to-cart, THEN proceed to checkout.
   - Submit/confirm with empty required fields → fill the fields first.
   Plan the prerequisite steps first, then the gated action. Do not repeat a step that just
   failed without first changing the state that made it fail.
   STALE ERROR RECOVERY: if a form shows an error that looks left over from a previous attempt
   (e.g. "enter a card number" / "invalid value") and you are re-entering a corrected value, the
   recovery is: tap the field, type the correct value, THEN tap the submit/pay/confirm button, and
   only after that verify the result. An error banner that predates your corrected input is often
   STALE — some apps clear it only once the corrected value is submitted. Never re-enter a value and
   then stop at the still-visible old error WITHOUT tapping submit again; and only conclude failure
   if the error remains AFTER you submit the corrected value.
6. CREDENTIALS (login / sign-in):
   - Use ONLY the exact email and password given in the Task above. They come from the test
     configuration. NEVER invent, guess, or reuse a placeholder/example email or password.
   - If a login screen is shown but the Task gives no credentials, do NOT attempt to log in —
     emit "verify: cannot log in — no credentials provided in task" and stop.
   - You normally should NOT be on a login screen mid-test. If you unexpectedly are, the earlier
     session may still be valid; prefer continuing the task over re-authenticating.

Example (note: "type:" values are illustrative — substitute real values from the Task):
["tap: search_input", "type: wireless headphones", "tap: search_button", "verify: results are shown"]
Example (empty-cart recovery — add item before checkout):
["tap: nexora_phone_increment_button", "tap: nexora_phone_add_to_cart_button", "tap: cart_checkout_button", "verify: cart screen shows one item"]
"""

VALIDATE_STEP = """\
A robotic arm just performed: {action_description}
Action type: {action_type}
Screen before action: [{screen_before}]

Examine the current screenshot (taken immediately after the action).

Guidance by action type:
- tap (navigation): did the screen transition to the expected next screen?
- tap (non-navigation, e.g. quantity +/-): did the UI update as expected?
- type: assume the text was entered. Do NOT report failure merely because an error/toast is
  visible — such a message is often STALE, left over from a PREVIOUS failed attempt (some apps keep
  the previous error on screen until the corrected value is re-submitted). Only fail a type if the
  field visibly rejected THIS input (e.g. the characters did not appear in the field).
- verify: is the specified text or element present?

STALE-ERROR RULE (important): an error message that was ALREADY on screen before this action is not
evidence that this action failed. For a submit/confirm/pay tap, judge success by the RESULT produced
AFTER the tap (screen advanced, success/confirmation shown, or the SAME error persists) — not by an
error that predates it. If a required value was just corrected and re-submitted, only conclude failure
when the error remains AFTER that submit.

Return ONLY valid JSON:
{{
  "success": <true|false>,
  "new_screen_id": "<screen_id of current screenshot>",
  "observation": "<one sentence: what changed or what went wrong>",
  "recovery_hint": <"<alternative to try>" | null>
}}
"""
