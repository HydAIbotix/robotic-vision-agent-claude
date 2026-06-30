SUGGEST_EXPLORABLE_ACTIONS = """\
You are systematically exploring a touch-screen application to build a complete navigation map.
Your goal: identify every distinct UI path that can be triggered from this screen.

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

Rules:
- CRITICAL: ONLY generate actions using the elements listed above on THIS screen.
  Do NOT plan cross-screen flows. Each action must end after the FIRST navigation or form submit.
- Each unconditional navigation button or link = one action with a single tap step
- Each form submit = one compound action: fill all required fields THEN tap submit
- For forms with auth: generate TWO actions — one with valid credentials, one with invalid
- Use {{{{valid_email}}}}, {{{{valid_password}}}}, {{{{invalid_email}}}}, {{{{invalid_password}}}} as placeholders
- Stepper controls (+/-/increment/decrement/up/down arrows): one action per direction, tap only
- CONDITIONAL NAVIGATION (critical): Some navigation elements only trigger a screen transition
  when the application has the correct state. Reason from the element labels and descriptions:
    • A counter or badge showing zero or empty state: "Cart (0)", "0 items selected",
      "Basket: empty", "0 guests", "Amount: $0.00", "Nothing in queue"
    • A proceed/confirm action that logically needs prior input to be meaningful:
      "Proceed to Payment", "Confirm Booking", "Submit Order", "Place Bid", "Continue"
    • Any element whose own description implies a dependency on other elements on this screen
  For each gated element: examine the OTHER elements on this screen and identify which ones
  satisfy its precondition — quantity controls, add-item buttons, selection tiles, input
  fields, checkboxes, toggles, radio buttons, date pickers, etc. Generate ONE multi-step
  action that: (1) performs the prerequisite setup steps in logical order, (2) taps the
  gated element as the final step. Do NOT generate a bare single-tap for a gated element —
  it will not navigate and will record a misleading dead-end in the map. Always prioritise
  gated-navigation actions; include them even if the per-screen limit is otherwise reached.
- Skip static text, images, decorative dividers, background illustrations
- SKIP display-only widgets: status panels, info tiles, or metric displays whose label is a
  noun phrase with no action verb (e.g. read-only device status, health indicators,
  identifier labels, live statistics). They do not navigate. Only include a widget when it
  carries explicit action language ("Tap to configure", "Select", "Edit") or clearly leads
  to a different screen.
- SKIP globally-redundant actions: persistent navigation elements that always lead to the
  same fixed screen (sign-out, home, back) need only be recorded once across the whole app.
  Skip them on any screen where that destination is already in the navigation map.
- action_key naming: Use short verb+noun task-outcome names. Do NOT embed any screen name
  (current or any other) inside the action_key — this silently breaks deduplication logic.
  Good: "open_cart", "submit_valid_login", "proceed_to_payment", "increment_item_qty".
  Bad: "from_login_to_home", "checkout_from_products", "navigate_screen_a_to_b".
- Maximum 15 actions per screen
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
