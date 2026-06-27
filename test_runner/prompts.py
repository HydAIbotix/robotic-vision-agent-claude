PLAN_FROM_MAP = """\
You are planning the execution of a kiosk test case.
The kiosk has already been explored — you have the complete screen and element inventory below,
including exact pixel coordinates for every interactive element.
You do NOT need a screenshot: the inventory IS the visual description of the app, pre-extracted.

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

Your job:
1. Determine credential_scenario: "valid" or "invalid" (look for "invalid"/"wrong" in the test steps).
2. Map each human-readable test step to one or more machine steps using ONLY the elements listed above.
3. For tap steps: include the screen_id the element belongs to and its exact px/py from the inventory.
4. For type steps: substitute the actual credential values (not placeholders).
5. For verify steps: include the expected_screen id (from the inventory) and a human description.
   Use DOM screen comparison — no visual check needed.

Return ONLY valid JSON — no markdown fences:
{{
  "credential_scenario": "valid",
  "steps": [
    {{"action": "verify", "expected_screen": "signin",   "description": "login screen is visible"}},
    {{"action": "tap",    "screen_id": "signin", "element_id": "email_input",    "px": 700, "py": 412}},
    {{"action": "type",   "value": "{valid_email}"}},
    {{"action": "tap",    "screen_id": "signin", "element_id": "password_input", "px": 700, "py": 498}},
    {{"action": "type",   "value": "{valid_password}"}},
    {{"action": "tap",    "screen_id": "signin", "element_id": "sign_in_button", "px": 700, "py": 560}},
    {{"action": "verify", "expected_screen": "products", "description": "products page is displayed"}}
  ]
}}

Rules:
- Use ONLY element ids and screen ids that appear verbatim in the inventory above.
- Do not invent element ids or coordinates — copy them exactly from the inventory.
- A "tap" step that focuses a text field must be immediately followed by a "type" step.
- "User presents payment card" → tap the mock-approval button (look for it in the inventory).
- If a step's expected outcome is a specific screen, set expected_screen to that screen's id.
- For invalid-credential tests: set credential_scenario="invalid" and use the invalid values.
"""

PARSE_TEST_CASE = """\
You are parsing a kiosk test case into a machine-executable step sequence for a robotic arm.

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
1. Determine whether the test uses VALID or INVALID credentials (look for "invalid" in summary/description).
2. Convert each human-readable step into one or more atomic robot commands.
3. Every tap step that focuses a text field should be followed by a type step.
4. Use element IDs from the app knowledge above (e.g. "email_input", "sign_in_button").
   If a step mentions a product name, find the matching element ID from app knowledge.
5. Replace "configured email/password" with the actual credential values.

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
    "verify: products page is displayed"
  ]
}}

Step format rules:
- tap: <element_id>           — physically tap that element
- type: <text>                — type text into the currently focused field
- verify: <human description> — Claude will visually confirm this is true on screen
- Combine "User enters X in Y field" into two steps: "tap: Y_element_id" then "type: X_value"
- "User selects [ButtonLabel]" → "tap: element_id_of_that_button"
- "User presents payment card" → "tap: use_mock_approval_button" (demo mode)
- Keep verify steps: they drive validation at the end of each logical phase
"""
