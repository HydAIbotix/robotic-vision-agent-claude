SUGGEST_EXPLORABLE_ACTIONS = """\
You are systematically exploring a kiosk application to build a complete navigation map.
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
- Each navigation button or link = one action with a single tap step
- Each form submit = one compound action: fill all required fields THEN tap submit
- For forms with auth: generate TWO actions — one with valid credentials, one with invalid
- Use {{{{valid_email}}}}, {{{{valid_password}}}}, {{{{invalid_email}}}}, {{{{invalid_password}}}} as placeholders
- Quantity stepper + / - buttons: one action each (just tap, no credentials)
- "Add to Cart" buttons: include a preceding qty increment step first
- Skip static text, images, decorative elements
- Maximum 12 actions per screen
"""

IDENTIFY_RESULT_SCREEN = """\
A robotic arm just executed: "{action_description}"
From screen: {from_screen_id}

Known screens discovered so far:
{known_screens}

Examine the current screenshot (the result of that action).

Return ONLY valid JSON:
{{
  "screen_id": "short_snake_case_id",
  "description": "one sentence: what this screen is for",
  "is_new_screen": true,
  "transition_type": "navigation_success | error_modal | partial_update | no_change"
}}

- If the screenshot matches a known screen: set is_new_screen=false and use that screen's ID exactly
- If it's a new screen never seen before: coin a short snake_case ID
- transition_type: did we navigate to a new page, see an error, see a small UI update, or nothing changed?
"""
