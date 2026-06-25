ANALYZE_SCREEN = """\
Analyze this kiosk touchscreen screenshot and identify every visible interactive element.

Return ONLY valid JSON — no markdown fences, no explanation:
{
  "screen_id": "<login|products|cart|payment|order_history|success|unknown>",
  "description": "<one sentence describing this screen's purpose>",
  "elements": [
    {
      "id": "<short_snake_case_unique_id>",
      "type": "<button|input|text|link|image|dropdown|stepper>",
      "label": "<visible text or placeholder>",
      "description": "<what happens when tapped or typed into>",
      "bbox": [x1_norm, y1_norm, x2_norm, y2_norm],
      "center": [cx_norm, cy_norm],
      "confidence": 0.95
    }
  ]
}

Coordinate rules (all values NORMALIZED 0.0–1.0):
- 0.0 = left/top edge of image, 1.0 = right/bottom edge
- "center" is the EXACT tap point — the visual midpoint of the interactive hit area
- "confidence" measures certainty about the CENTER coordinates only:
    0.90–1.00 : element boundary clearly visible, center unambiguous
    0.70–0.89 : element visible but edges soft, partially occluded, or very small
    below 0.70 : element inferred from context, coordinates are estimated

Content rules:
- Include ALL interactive elements: buttons, inputs, links, quantity ±, nav items, tabs
- Unique snake_case IDs matching the label, e.g. "sign_in_button", "email_input"
- Omit purely decorative text, dividers, and background images
- For quantity steppers include + and - as separate elements
- EXCLUDE on-screen / virtual keyboard keys — the robot types text directly; individual keys are not needed
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
