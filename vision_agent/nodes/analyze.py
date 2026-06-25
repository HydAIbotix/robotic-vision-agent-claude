"""
analyze_screen node — loads the current screenshot and asks Claude to identify
every UI element with its type, label, description, bounding box, and tap coordinate.
"""
import base64
import json
from langchain_core.messages import HumanMessage
from vision_agent.state import VisionAgentState, ScreenAnalysis
from vision_agent.prompts import ANALYZE_SCREEN
from vision_agent.llm import get_llm
from vision_agent.storage import get_storage


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```" in text:
        # strip optional markdown code fence
        text = text.split("```")[1].lstrip("json").strip()
    return json.loads(text)


def analyze_screen(state: VisionAgentState) -> dict:
    image_bytes = get_storage().load(state["image_path"])
    b64 = base64.standard_b64encode(image_bytes).decode()

    llm = get_llm()
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": ANALYZE_SCREEN},
    ])
    response = llm.invoke([msg])
    analysis: ScreenAnalysis = _parse_json(response.content)

    # Append to screen_history only when the screen actually changes
    history = list(state.get("screen_history") or [])
    if not history or history[-1] != analysis["screen_id"]:
        history.append(analysis["screen_id"])

    return {"screen_analysis": analysis, "screen_history": history}
