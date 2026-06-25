"""
Smoke tests for the vision agent using local kiosk screenshots.
Run: pytest tests/ -v

These tests call the real Claude API — set ANTHROPIC_API_KEY in .env.
"""
import os
import pytest
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SCREENSHOTS = Path("C:/Users/gsk54/Desktop/Robotics_Project/Kiosk_Screenshots_Latest")

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)


@pytest.fixture(autouse=True)
def mock_robot_screens(request, monkeypatch):
    """Each test can set demo_screens via indirect fixture or marker."""
    screens = getattr(request, "param", [])
    from vision_agent.robot import stubs as robot
    robot.set_demo_screens([str(s) for s in screens])
    yield
    robot.set_demo_screens([])


def _run(task: str, start: str, screens: list[str]) -> dict:
    from vision_agent.robot import stubs as robot
    robot.set_demo_screens([str(s) for s in screens])

    from vision_agent.agent import create_agent
    agent = create_agent()
    return agent.invoke({
        "task_description": task,
        "image_path": start,
        "screen_analysis": None,
        "planned_steps": [],
        "current_step_idx": 0,
        "step_results": [],
        "retry_count": 0,
        "screen_history": [],
        "decision_tree": {},
        "outcome": "running",
        "summary": "",
        "error_message": None,
    })


class TestScreenAnalysis:
    def test_login_screen_identifies_key_elements(self):
        """Claude must find email input, password input, and sign-in button."""
        from vision_agent.nodes.analyze import analyze_screen
        state = {
            "image_path": str(SCREENSHOTS / "login_page.png"),
            "screen_history": [],
        }
        result = analyze_screen(state)
        analysis = result["screen_analysis"]

        assert analysis["screen_id"] == "login"
        ids = {el["id"] for el in analysis["elements"]}
        types = {el["type"] for el in analysis["elements"]}
        # At minimum there should be two inputs and at least one button
        assert "input" in types
        assert "button" in types
        # Every element must have valid coordinates
        for el in analysis["elements"]:
            assert len(el["bbox"]) == 4
            assert len(el["center"]) == 2
            assert all(v >= 0 for v in el["bbox"])

    def test_products_screen_identifies_add_to_cart(self):
        from vision_agent.nodes.analyze import analyze_screen
        state = {
            "image_path": str(SCREENSHOTS / "products_page.png"),
            "screen_history": [],
        }
        result = analyze_screen(state)
        analysis = result["screen_analysis"]

        assert analysis["screen_id"] == "products"
        labels = {el["label"].lower() for el in analysis["elements"]}
        assert any("cart" in lbl for lbl in labels)


class TestLoginFlow:
    def test_full_login_flow_passes(self):
        screens = [
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "products_page.png",
        ]
        result = _run(
            task="Log in with email tester@kiosk.local and password Password123",
            start=str(SCREENSHOTS / "login_page.png"),
            screens=[str(s) for s in screens],
        )
        assert result["outcome"] == "passed"
        assert "login" in result["screen_history"]
        assert "products" in result["screen_history"]

    def test_decision_tree_is_populated(self):
        screens = [
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "login_page.png",
            SCREENSHOTS / "products_page.png",
        ]
        result = _run(
            task="Log in with email tester@kiosk.local and password Password123",
            start=str(SCREENSHOTS / "login_page.png"),
            screens=[str(s) for s in screens],
        )
        # Decision tree must have at least one entry
        assert len(result["decision_tree"]) > 0
