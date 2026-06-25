from typing import Optional
from typing_extensions import TypedDict
from app_map.store import AppMap


class ExplorationStep(TypedDict):
    """One atomic robot action within an exploration sequence."""
    action_type: str          # "tap" | "type"
    element_id: str           # target element id
    value: Optional[str]      # text to type (type actions only)


class ExplorationAction(TypedDict):
    """
    A compound exploration unit: fill fields + tap submit, or a single tap.
    Executed as a unit so the explorer can observe one distinct outcome.
    """
    action_key: str                       # unique within screen, e.g. "tap_sign_in_valid"
    screen_id: str                        # which screen this action is executed on
    description: str                      # human-readable intent
    steps: list[ExplorationStep]          # ordered steps to perform
    credential_scenario: Optional[str]    # "valid" | "invalid" | None


class ExplorerState(TypedDict):
    # ── Input / Config ────────────────────────────────────────────────────────
    app_name: str
    entry_image_path: str
    credentials: dict              # {"valid": {"email": ..., "password": ...}, "invalid": {...}}
    # demo_navigation: "{screen_id}::{action_key}" → screenshot_path
    # "__default__" suffix gives the starting screenshot for a screen
    demo_navigation: dict
    app_map_path: str              # file to save the finished AppMap

    # ── Working state ─────────────────────────────────────────────────────────
    app_map: AppMap
    exploration_queue: list[ExplorationAction]
    explored_action_keys: list[str]    # prevents revisiting the same action
    current_image_path: str
    current_screen_id: str
    last_executed_action: Optional[ExplorationAction]
    last_result_is_new: bool
    last_result_screen_id: str

    # ── Output ────────────────────────────────────────────────────────────────
    complete: bool
