from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vision backend: swap to "bedrock" to use AWS — zero other code changes
    vision_backend: Literal["anthropic", "bedrock"] = "anthropic"
    anthropic_api_key: str = ""
    # Opus 4.8 everywhere reasoning matters — screen analysis, test-plan generation, and the
    # Tier-3 VisionAgent (analyze/plan/validate).  Sonnet previously mis-planned prerequisite
    # steps (e.g. incrementing product quantity before "Add to Cart"); Opus reasons about
    # those preconditions reliably.  Tier-1/2 execution uses 0 LLM calls, so the steady-state
    # cost is unchanged — Opus is only paid on exploration, planning, and Tier-3 fallback.
    anthropic_model: str = "claude-opus-4-8"
    # Kept as a distinct setting so exploration can be tuned independently; also Opus.
    anthropic_explorer_model: str = "claude-opus-4-8"

    # AWS Bedrock (active only when vision_backend="bedrock")
    bedrock_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-opus-4-8"

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
    # Optional shared card service URL. When set, it is appended to the kiosk URL
    # (?cardServiceUrl=…) so the kiosk apps share smart-card balances/transactions
    # across machines. Blank → apps use per-browser localStorage (single-machine default).
    card_service_url: str = ""

    # Agent behaviour
    max_retries: int = 3
    screenshots_dir: str = "./screenshots"
    results_dir: str = "./results"

    # Vision confidence: elements below this score get a correction follow-up call
    coordinate_confidence_threshold: float = 0.85

    # Short-response model tier (validation, conclusive verdict). Opus 4.8 for consistency —
    # every Claude call in the system uses the same model; this tier just caps output tokens lower.
    anthropic_fast_model: str = "claude-opus-4-8"

    # App Explorer coordinate cache — skip analyze_screen LLM call for known static screens
    app_map_path: str = "app_map.json"
    use_app_map_cache: bool = True

    # App Explorer mode:
    #   "claude"          — screenshot → Claude vision → elements (default; works for all backends)
    #   "playwright_aria" — ARIA accessibility tree → elements (playwright backend only; 0 LLM calls)
    # When robot_backend="real", exploration_mode is forced to "claude" automatically.
    exploration_mode: str = "claude"

    # ── Hardware robot settings (active when robot_backend="real") ─────────────
    # Connection. The AGV base and the arm may run on separate controllers/IPs, so each has its
    # own base URL. Enter e.g. http://192.168.1.101:8000. When a URL is blank that side falls back
    # to robot_ip:robot_port (single-controller setups keep working). "/api/v1" is auto-appended.
    robot_ip: str = "192.168.1.100"
    robot_port: int = 8000
    agv_url: str = ""   # mobile base (AGV) controller — serves /base/*
    arm_url: str = ""   # arm + camera + card controller — serves /capture, /arm/*, /screen/*, /card/*
    robot_id: str = "R-01"
    default_kiosk_id: str = "K-01"

    def _api_base(self, url: str) -> str:
        base = (url or f"http://{self.robot_ip}:{self.robot_port}").strip().rstrip("/")
        return base if base.endswith("/api/v1") else f"{base}/api/v1"

    def arm_api_base(self) -> str:
        """Base URL (…/api/v1) for arm, camera, screen and card endpoints."""
        return self._api_base(self.arm_url)

    def agv_api_base(self) -> str:
        """Base URL (…/api/v1) for the mobile-base (AGV) /base/* endpoints."""
        return self._api_base(self.agv_url or self.arm_url)

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
