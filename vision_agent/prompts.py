ANALYZE_SCREEN = """\
Analyze this kiosk touchscreen ({width}x{height} pixels) and identify every visible interactive element.

Return ONLY valid JSON — no markdown fences, no explanation:
{{
  "screen_id": "<login|products|cart|payment|order_history|success|unknown>",
  "description": "<one sentence describing this screen>",
  "elements": [
    {{
      "id": "<short_snake_case_unique_id>",
      "type": "<button|input|text|link|image|dropdown|stepper>",
      "label": "<visible text or placeholder>",
      "description": "<what happens when tapped or typed into>",
      "bbox": [x1_norm, y1_norm, x2_norm, y2_norm],
      "center": [cx_norm, cy_norm],
      "confidence": 0.95
    }}
  ]
}}

Rules:
- bbox and center must be NORMALIZED values between 0.0 and 1.0:
    0.0 = left/top edge of the image,  1.0 = right/bottom edge
    Example: an element at pixel (954, 493) in a {width}x{height} image
             has center [{cx_example:.3f}, {cy_example:.3f}]
- Include ALL interactive elements (buttons, inputs, links, quantity ±, nav items)
- Assign unique ids: prefer the element text in snake_case, e.g. "sign_in_button"
- Omit purely decorative text or background images
- For quantity steppers, include the + and - as separate elements
"""

PLAN_STEPS = """\
You control a robotic arm that physically taps a kiosk touchscreen.

Task: {task_description}

Current screen: [{screen_id}] {screen_description}

Available elements:
{elements}

Decompose the task into the minimum ordered atomic steps using ONLY these forms:
  tap: <element_id>
  type: <text to enter>
  verify: <text or condition to confirm on screen>

Use element IDs exactly as listed. One action per step.

Return ONLY a JSON array:
["tap: email_input", "type: user@example.com", "tap: sign_in_button"]
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
