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


settings = Settings()
