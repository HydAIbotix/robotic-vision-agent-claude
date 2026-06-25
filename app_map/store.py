"""
AppMap — the persistent knowledge graph of the application under test.

Produced once by the AppExplorer and consumed by every subsequent test run.
Stored as plain JSON so it can be inspected, version-controlled, and loaded
by the test runner without re-running exploration.
"""
import json
import time
from pathlib import Path
from typing import Optional
from typing_extensions import TypedDict
from vision_agent.state import ScreenElement


class AppScreen(TypedDict):
    screen_id: str
    description: str
    elements: list[ScreenElement]          # full analyzed element list
    transitions: dict[str, str]            # {action_key: resulting_screen_id}


class AppMap(TypedDict):
    app_name: str
    explored_at: str
    entry_screen: str
    screens: dict[str, AppScreen]          # keyed by screen_id


def empty(app_name: str, entry_screen: str) -> AppMap:
    return {
        "app_name": app_name,
        "explored_at": "",
        "entry_screen": entry_screen,
        "screens": {},
    }


def save(app_map: AppMap, path: str) -> None:
    out = {**app_map, "explored_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    print(f"\n  [APP MAP] Saved to {path}  ({len(out['screens'])} screens)")


def load(path: str) -> AppMap:
    return json.loads(Path(path).read_text())


def prompt_summary(app_map: Optional[AppMap]) -> str:
    """Compact text representation for including in LLM prompts."""
    if not app_map:
        return "No app map available — agent will identify screens and elements dynamically from screenshots."
    lines = [f"App: {app_map['app_name']}  entry={app_map['entry_screen']}"]
    for sid, sc in app_map["screens"].items():
        lines.append(f"\n  [{sid}] {sc['description']}")
        els = "  |  ".join(
            f"{e['id']}({e['type']})" for e in sc.get("elements") or []
        )
        lines.append(f"    elements: {els}")
        for ak, nxt in (sc.get("transitions") or {}).items():
            lines.append(f"    {ak} --> {nxt}")
    return "\n".join(lines)
