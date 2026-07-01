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

Example:
["tap: email_input", "type: user@example.com", "tap: sign_in_button", "verify: dashboard is shown"]
"""

VALIDATE_STEP = """\
A robotic arm just performed: {action_description}
Action type: {action_type}
Screen before action: [{screen_before}]

Examine the current screenshot (taken immediately after the action).

Guidance by action type:
- tap (navigation): did the screen transition to the expected next screen?
- tap (non-navigation, e.g. quantity +/-): did the UI update as expected?
- type: assume success unless an error message or unexpected popup is visible
- verify: is the specified text or element present?

Return ONLY valid JSON:
{{
  "success": <true|false>,
  "new_screen_id": "<screen_id of current screenshot>",
  "observation": "<one sentence: what changed or what went wrong>",
  "recovery_hint": <"<alternative to try>" | null>
}}
"""
