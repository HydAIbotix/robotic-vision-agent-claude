PLAN_FROM_MAP = """\
You are planning the execution of an automated test case.
The application under test has been explored — you have the complete screen and element
inventory below, including exact pixel coordinates for every interactive element.
You do NOT need a screenshot: the inventory IS the visual description of the app.

Test Case: {test_id}
Summary:   {summary}

Raw test steps:
{steps_raw}

Raw expected results:
{expected_results_raw}

Credentials:
  valid:   email={valid_email}, password={valid_password}
  invalid: email={invalid_email}, password={invalid_password}

App element inventory (ALL screens, elements, and pixel coordinates):
{element_inventory}

═══ UNKNOWN SCREENS / ELEMENTS — READ THIS FIRST ════════════════════
Check the inventory BEFORE planning any step.

If a step requires a screen or element that is NOT in the inventory:
  ✗  Do NOT invent an element_id.
  ✗  Do NOT output px:0, py:0 with a guessed element_id.
  ✗  Do NOT output an empty element_id ("").
  ✓  Output this sentinel and STOP — do not add any more steps after it:
       {{"action": "vision_required", "description": "<what the test still needs to do from here>"}}

The runtime switches to live Claude Vision for everything after the sentinel.
A tap step with element_id "" or px:0/py:0 is WRONG — use vision_required instead.

═══ PREREQUISITE ANALYSIS ═══════════════════════════════════════════
Raw test steps are high-level summaries that skip implicit prerequisites.
Ask: "Would this step succeed if a robot ran it right now?"

1. If a step navigates to a screen requiring prior state (items in cart before checkout,
   form filled before submit), INSERT missing prerequisite steps from the inventory.
2. Disabled elements must be preceded by steps that satisfy their precondition.
3. Form submissions: all required fields must be filled before tapping submit.
4. Produce the COMPLETE executable sequence — do not blindly translate word-for-word.

═══ YOUR TASKS ══════════════════════════════════════════════════════
1. Determine credential_scenario: "valid" or "invalid".
2. For EVERY step: verify the screen_id AND element_id exist verbatim in the inventory.
   If either is missing → emit vision_required and stop.
3. Apply prerequisite analysis to produce the COMPLETE executable sequence.
4. For tap steps: copy screen_id, element_id, px, py EXACTLY from the inventory.
5. For type steps: substitute actual credential values.
6. For verify steps: include expected_screen (the screen_id from the inventory).

Return ONLY valid JSON — no markdown fences:
{{
  "credential_scenario": "valid",
  "steps": [
    {{"action": "verify", "expected_screen": "login",    "description": "login screen is visible"}},
    {{"action": "tap",    "screen_id": "login", "element_id": "email_input",    "px": 700, "py": 412}},
    {{"action": "type",   "value": "{valid_email}"}},
    {{"action": "tap",    "screen_id": "login", "element_id": "password_input", "px": 700, "py": 498}},
    {{"action": "type",   "value": "{valid_password}"}},
    {{"action": "tap",    "screen_id": "login", "element_id": "sign_in_button", "px": 700, "py": 560}},
    {{"action": "verify", "expected_screen": "products", "description": "products page is displayed"}},
    {{"action": "vision_required", "description": "complete payment and verify order success"}}
  ]
}}

Additional rules:
- ONLY use element_id and screen_id values that appear verbatim in the inventory.
- Every verify step MUST have expected_screen (the screen_id where verification occurs).
- Add expected_text only when the test explicitly checks a specific value (order total,
  error message text, transaction ID, etc.).
- For invalid-credential tests: credential_scenario="invalid", use the invalid values.
"""

PARSE_TEST_CASE = """\
You are parsing an automated test case into a machine-executable step sequence.

Test Case: {test_id}
Summary:   {summary}

Raw test steps:
{steps_raw}

Raw expected results:
{expected_results_raw}

App knowledge (known screens and their element IDs):
{app_map_summary}

Credentials:
  valid:   email={valid_email}, password={valid_password}
  invalid: email={invalid_email}, password={invalid_password}

Your job:
1. Determine whether the test uses VALID or INVALID credentials.
2. Convert each human-readable step into one or more atomic robot commands.
3. Every tap step that focuses a text field must be immediately followed by a type step.
4. Use element IDs from app knowledge exactly as listed. If a step mentions a product or
   button name, find the closest matching element ID in the app knowledge.
5. Substitute actual credential values — do not leave placeholders.
6. For any step that requires a screen or element NOT present in app knowledge,
   write "verify: <describe what needs to happen>" and stop — do not fabricate IDs.

Return ONLY valid JSON:
{{
  "credential_scenario": "valid",
  "planned_steps": [
    "verify: login screen is visible",
    "tap: email_input",
    "type: {valid_email}",
    "tap: password_input",
    "type: {valid_password}",
    "tap: sign_in_button",
    "verify: dashboard or home screen is displayed"
  ]
}}

Step format rules:
- tap: <element_id>           — interact with that element (exact id from app knowledge)
- type: <text>                — type text into the focused field
- verify: <condition>         — assert a visible condition; used for validation checkpoints
- Combine "User enters X in Y field" → "tap: Y_element_id" then "type: X_value"
- "User selects [label]" → "tap: <matching_element_id_from_app_knowledge>"
- "User presents / taps payment card" → look for a mock-approval or complete-order element
  in app knowledge; if not found, write "verify: payment is completed" and stop
"""
