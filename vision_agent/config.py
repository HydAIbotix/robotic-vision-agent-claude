from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vision backend: swap to "bedrock" to use AWS — zero other code changes
    vision_backend: Literal["anthropic", "bedrock"] = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # AWS Bedrock (active only when vision_backend="bedrock")
    bedrock_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-sonnet-4-5-20251001-v2:0"

    # Storage: swap to "s3" for AWS — zero other code changes
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str = ""
    s3_prefix: str = "vision-agent"
    sqs_queue_url: str = ""  # SQS queue for robot image events

    # Robot backend — swap without touching agent code
    # demo        : pre-captured screenshots, no real interaction (default)
    # playwright  : Playwright drives a browser — proxy for real hardware tests
    # real        : physical robot arm hardware API
    robot_backend: str = "demo"
    kiosk_url: str = "http://localhost:5173"

    # Agent behaviour
    max_retries: int = 3
    screenshots_dir: str = "./screenshots"
    results_dir: str = "./results"

    # Vision confidence: elements below this score get a correction follow-up call
    coordinate_confidence_threshold: float = 0.85

    # Fast model used for validation (Haiku — binary yes/no, no element detection needed)
    anthropic_fast_model: str = "claude-haiku-4-5-20251001"

    # App Explorer coordinate cache — skip analyze_screen LLM call for known static screens
    app_map_path: str = "app_map.json"
    use_app_map_cache: bool = True

    # ── Hardware robot settings (active when robot_backend="real") ─────────────
    # Connection
    robot_ip: str = "192.168.1.100"
    robot_port: int = 8000
    robot_id: str = "R-01"
    default_kiosk_id: str = "K-01"

    # Coordinate spaces
    # viewport_* = what app_map learned in Playwright mode (pixels)
    # robot_camera_* = resolution returned by /capture from the real arm
    viewport_width: int = 1400
    viewport_height: int = 900
    robot_camera_width: int = 1920
    robot_camera_height: int = 1080

    # Physical kiosk screen dimensions (meters) — used for arm pose calculations
    screen_width_m: float = 0.400
    screen_height_m: float = 0.300

    # Timeouts (seconds)
    base_move_timeout_s: float = 60.0
    arm_move_timeout_s: float = 30.0
    card_op_timeout_s: float = 30.0
    robot_poll_interval_s: float = 0.5

    # Management API (FastAPI server for management frontend)
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    db_url: str = "sqlite:///./management.db"  # swap to postgresql://... for production


settings = Settings()
