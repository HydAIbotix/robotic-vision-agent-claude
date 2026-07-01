SUGGEST_EXPLORABLE_ACTIONS = """\
You are systematically exploring a touch-screen application to build a COMPLETE navigation map.
Your goal: find every distinct screen the user can reach.  Missing a screen is a critical failure —
test automation cannot cover what was not discovered.  Err on the side of MORE actions, not fewer.

Current screen: {screen_id}
Description: {screen_description}

Interactive elements:
{elements_text}

Available credentials for form testing:
  valid:   email={valid_email}, password={valid_password}
  invalid: email={invalid_email}, password={invalid_password}

Return ONLY valid JSON:
{{
  "explorable_actions": [
    {{
      "action_key": "short_snake_case_unique_key",
      "description": "what this action does and where it probably leads",
      "steps": [
        {{"action_type": "type", "element_id": "email_input", "value": "{{{{valid_email}}}}"}},
        {{"action_type": "type", "element_id": "password_input", "value": "{{{{valid_password}}}}"}},
        {{"action_type": "tap",  "element_id": "sign_in_button", "value": null}}
      ],
      "credential_scenario": "valid"
    }}
  ]
}}

═══ MANDATORY: FLOW-COMPLETION BUTTONS ════════════════════════════════════════════
Buttons that advance a multi-step user flow (checkout, payment, booking, order, purchase,
confirm, submit, proceed, continue, complete, place order) MUST be included — even if you
think they require prior state.  These are the most important screens in the app.

If the button appears visually active (not greyed out) on this screen, it is reachable now.
Generate ONE action to tap it directly.

If the button appears disabled or you see a counter/badge at zero:
  → Build a multi-step action: perform the prerequisite setup steps first (add item, fill
    form, select option), THEN tap the proceed/checkout/pay button as the final step.

NEVER omit a payment, checkout, proceed, confirm, or submit button.  The downstream test
suite depends on these screens being in the navigation map.

═══ CONDITIONAL NAVIGATION ════════════════════════════════════════════════════════
Some buttons only navigate when the app is in the right state:
    • Cart with 0 items: "Proceed to Payment" stays on the cart page
    • Form with empty fields: "Submit" shows an error, not a new screen
    • Booking with no date: "Confirm" is a no-op

For each gated element, identify which OTHER elements on this screen satisfy its precondition
(quantity controls, add-to-cart, selection tiles, date pickers, checkboxes, input fields)
and generate ONE multi-step action:
  (1) prerequisite steps in logical order
  (2) tap the gated element as the FINAL step

Do NOT generate a bare single-tap for a known gated element — it will stay on the current
screen and record a misleading dead-end.  Always include gated-navigation actions even if
the per-screen limit would otherwise be reached.

═══ GENERAL RULES ════════════════════════════════════════════════════════════════
- ONLY use elements listed above on THIS screen.  Do not plan cross-screen flows.
- Each action ends after the FIRST navigation or form submit.
- Each unconditional nav button/link = one action with a single tap step.
- Each form submit = compound action: fill required fields THEN tap submit.
- For auth forms: TWO actions — valid credentials + invalid credentials.
- Use {{{{valid_email}}}}, {{{{valid_password}}}}, {{{{invalid_email}}}}, {{{{invalid_password}}}} as placeholders.
- Stepper controls (+/-): one action per direction, tap only.
- Skip purely decorative elements (static text, dividers, background images).
- Skip read-only display widgets with no action verb (status panels, health indicators).
- SKIP globally-redundant nav elements already recorded elsewhere (sign-out, home, back).
- action_key: short verb+noun, no screen names embedded.
  Good: "open_cart", "submit_valid_login", "proceed_to_payment", "increment_item_qty"
  Bad:  "from_login_to_home", "checkout_from_products"
- Maximum 15 actions per screen (flow-completion buttons are exempt from this limit).
"""

MAP_KEYBOARD = """\
A custom virtual keyboard is visible on screen as part of a touch-screen application.
Map every tappable key to its center position as normalized coordinates (0.0–1.0,
where [0,0] is top-left and [1,1] is bottom-right).

Return ONLY valid JSON:
{
  "keys": {
    "a": [0.07, 0.72],
    "b": [0.42, 0.80],
    "0": [0.10, 0.65],
    "@": [0.15, 0.88],
    ".": [0.72, 0.88],
    "space": [0.45, 0.88],
    "backspace": [0.93, 0.80],
    "shift": [0.05, 0.80],
    "done": [0.92, 0.88]
  }
}

Rules:
- Include ALL visible keys: a–z, 0–9, and every symbol on screen (@, ., -, _, !, #, etc.)
- Use lowercase single characters as keys (e.g. "a" not "A")
- For multi-character keys use: "space", "backspace", "shift", "done", "return", "clear"
- Coordinates must be the CENTER of each key cap
- If a key is not visible, omit it — do not guess
"""

ANALYZE_AND_SUGGEST = """\
Analyze this touch-screen application screenshot. Return BOTH a screen analysis AND explorable actions in a single JSON response.

Credentials available:
  valid:   email={valid_email} / password={valid_password}
  invalid: email={invalid_email} / password={invalid_password}

Return ONLY valid JSON — no markdown fences:
{{
  "screen_id": "short_snake_case_id",
  "description": "one sentence: this screen's purpose",
  "elements": [
    {{
      "id": "short_snake_case_unique_id",
      "type": "button|input|text|link|image|dropdown|stepper",
      "label": "visible text or placeholder",
      "description": "what happens when tapped or typed into",
      "bbox": [x1_norm, y1_norm, x2_norm, y2_norm],
      "center": [cx_norm, cy_norm],
      "confidence": 0.95
    }}
  ],
  "explorable_actions": [
    {{
      "action_key": "short_snake_case_unique_key",
      "description": "what this action does",
      "steps": [
        {{"action_type": "type", "element_id": "email_input", "value": "{{{{valid_email}}}}"}},
        {{"action_type": "tap",  "element_id": "sign_in_button", "value": null}}
      ],
      "credential_scenario": "valid|invalid|null"
    }}
  ]
}}

Element rules (coordinates NORMALIZED 0.0–1.0):
- center = exact tap point, visual midpoint of the hit area
- confidence: 0.90–1.00 boundary clear; 0.70–0.89 soft/partial; below 0.70 inferred
- Include ALL interactive elements (buttons, inputs, links, steppers, nav items)
- Omit decorative text, dividers, background images
- EXCLUDE virtual keyboard keys — the robot uses a pre-mapped keyboard, not individual key taps
- Max 25 elements

Action rules:
- CRITICAL: ONLY generate actions using elements visible on THIS screen — no cross-screen flows
- Each navigation link/button = one action with a single tap step
- Each form submit = one compound action: fill all required fields THEN tap submit
- For forms with auth: TWO actions — one with valid credentials, one with invalid
- Use {{{{valid_email}}}}, {{{{valid_password}}}}, {{{{invalid_email}}}}, {{{{invalid_password}}}} as placeholders
- Max 12 actions
"""

BATCH_SCROLL_ELEMENTS = """\
Below are {count} screenshots of the SAME screen captured at different vertical scroll positions.
Each screenshot is labelled with its scroll_y offset (pixels scrolled from the top of the page).

For EACH screenshot, identify interactive elements that are visible.
Return normalized coordinates (0.0–1.0) relative to THAT screenshot's own viewport — do NOT add the scroll offset yourself; the caller will do that.

Return ONLY valid JSON — no markdown fences:
{{
  "screenshots": [
    {{
      "index": 1,
      "elements": [
        {{
          "id": "short_snake_case_id",
          "type": "button|input|link|select|stepper",
          "label": "visible text",
          "description": "what this element does",
          "bbox": [x1_norm, y1_norm, x2_norm, y2_norm],
          "center": [cx_norm, cy_norm],
          "confidence": 0.90
        }}
      ]
    }}
  ]
}}

Rules:
- "index" starts at 1 and matches the screenshot order above
- Omit screenshots where no interactive elements are visible
- Omit purely decorative elements, dividers, status text
- EXCLUDE virtual keyboard keys
"""

IDENTIFY_RESULT_SCREEN = """\
A robotic arm just executed: "{action_description}"
From screen: {from_screen_id}

Known screens discovered so far:
{known_screens}
{dom_screen_hint}
Examine the current screenshot (the result of that action).

Return ONLY valid JSON:
{{
  "screen_id": "short_snake_case_id",
  "description": "one sentence: what this screen is for",
  "is_new_screen": true,
  "transition_type": "navigation_success | error_modal | partial_update | no_change"
}}

- If the screenshot matches a known screen: set is_new_screen=false and use that screen's ID exactly
- If it's a new screen never seen before: coin a short snake_case ID (use the DOM hint if provided)
- transition_type: did we navigate to a new page, see an error, see a small UI update, or nothing changed?
"""
