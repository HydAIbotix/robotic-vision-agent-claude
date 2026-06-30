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

PREREQUISITE ANALYSIS — apply this to every app, not just kiosks:
Raw test steps are written by humans as high-level summaries. They are often abbreviated and skip
implicit prerequisites. Before translating each step, ask: "Would this step actually succeed if a
robot ran it right now, given only the steps that came before?"

Rules:
1. If a step navigates to a screen that REQUIRES prior state (e.g. cart, checkout, payment,
   confirmation), verify that state was established by an earlier step. If not, INSERT the
   missing prerequisite steps using elements from the app map above.
   Examples:
   - "Go to cart" / "Checkout" / "Proceed to payment" → items must be in the cart first.
     If no add-to-cart step exists before this, INSERT one (or more, if the app requires
     selecting a quantity/product first) using the relevant element from the app map.
   - "Submit order" / "Place order" → cart must be non-empty AND checkout must be initiated.
   - "Pay" / "Enter card" → must be on the payment screen, which requires a non-empty cart.
2. Any element that has a disabled or inactive state when a condition is not met must be
   preceded by the steps that satisfy that condition.
3. Form submissions: all required fields must be filled before tapping submit.
4. Do NOT blindly translate raw steps word-for-word. Produce the COMPLETE executable sequence.
   Infer and insert missing steps from the app map whenever the raw steps skip prerequisites.

YOUR TASKS:
1. Determine credential_scenario: "valid" or "invalid" (look for "invalid"/"wrong" in the test steps).
2. Apply prerequisite analysis (above) to produce the COMPLETE executable sequence.
3. Map each human-readable test step to one or more machine steps using ONLY the elements listed above.
4. For tap steps: include the screen_id the element belongs to and its exact px/py from the inventory.
5. For type steps: substitute the actual credential values (not placeholders).
6. For verify steps: ALWAYS include expected_screen (the screen_id from the inventory where verification
   should occur). Use DOM screen comparison — no visual check needed.

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
    {{"action": "verify", "expected_screen": "products", "description": "products page is displayed"}}
  ]
}}

Rules:
- Use ONLY element ids and screen ids that appear verbatim in the inventory above.
- Do not invent element ids or coordinates — copy them exactly from the inventory.
- A "tap" step that focuses a text field must be immediately followed by a "type" step.
- "User presents payment card" → tap the mock-approval button (look for it in the inventory).
- Every verify step MUST have expected_screen set to the screen_id where that verification occurs.
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
