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

    # OCR engine path (Camera Vision Test diagnostic only). pytesseract is a thin wrapper that shells
    # out to the Tesseract ENGINE binary — the pip package alone is not enough. If Tesseract is
    # installed but not on PATH (common on Windows), set this to its full path, e.g.
    #   C:\Program Files\Tesseract-OCR\tesseract.exe
    # Blank → auto-discover the common install locations. OCR is a 0-LLM legibility aid on the
    # diagnostic page only; the live automation reads text via Claude vision, so this never affects a run.
    tesseract_cmd: str = ""

    # Vision confidence: elements below this score get a correction follow-up call
    coordinate_confidence_threshold: float = 0.85

    # Short-response model tier (validation, conclusive verdict). Opus 4.8 for consistency —
    # every Claude call in the system uses the same model; this tier just caps output tokens lower.
    anthropic_fast_model: str = "claude-opus-4-8"

    # App Explorer coordinate cache — skip analyze_screen LLM call for known static screens
    app_map_path: str = "app_map.json"
    use_app_map_cache: bool = True

    # ── Tier-1 screen determination: template matching (0 LLM) ────────────────
    # Real-robot screen identity uses normalized cross-correlation template matching
    # (cv2.matchTemplate TM_CCOEFF_NORMED, full-frame) against a per-screen reference image —
    # far more robust to the browser↔camera domain gap (lighting/contrast/colour-cast/blur/JPEG)
    # than the legacy 16×16 aHash, which could not bridge it. See vision_agent/vision/template_match.py.
    #   use_template_screen_match — master toggle. True: template matching is the Tier-1 screen
    #        identifier for the real backend (aHash remains a fallback when no reference image exists,
    #        so setups without reference files never regress). False: legacy aHash-only behaviour.
    #   template_match_threshold  — minimum TM_CCOEFF_NORMED score (peak, -1..1) to accept a match.
    #   template_match_margin     — the winning score must beat the runner-up by at least this much
    #        (discrimination guard, so a globally-bright frame cannot false-match a different screen).
    #   template_ref_dir          — in-repo folder of clean per-screen reference templates. A file
    #        named exactly "<screen_id>.png" (e.g. "login.png") — or whose name contains the screen_id,
    #        e.g. "Login_page.png" — OVERRIDES that screen's app_map reference_screenshot. Build this
    #        camera-domain library with POST /api/vision-test/save-reference (or capture_reference.py).
    #        Missing folder → no overrides (app_map references are used), so this default never regresses.
    use_template_screen_match: bool = True
    template_match_threshold: float = 0.55
    template_match_margin: float = 0.06
    template_ref_dir: str = "./reference_screens"

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

    # ── Coordinate spaces (see the big note below for how a tap stays accurate) ──
    # viewport_* = the pixel space app_map coordinates are learned in (Playwright exploration).
    #   This is the ONE value that must be chosen to match the target: for a REAL-ROBOT target set
    #   it (via Robot Setup) to the kiosk's rectified-camera ASPECT RATIO so the app renders the same
    #   layout the arm photographs; for a Playwright-only target keep the default 1400×900.
    viewport_width: int = 1400
    viewport_height: int = 900
    # robot_camera_* = a PRE-CALIBRATION SEED for the rectified /capture resolution, NOT the live
    #   value. Default 1280×720 = the Intel RealSense D405's native resolution (16:9). It is only used
    #   as the viewport→camera scale numerator BEFORE the first /capture; every /capture response then
    #   reports the ACTUAL rectified width/height and real_robot._scale switches to those measured
    #   dims (see the calibration note below), so this seed never affects a real tap. Editable on the
    #   Robot Setup page; update it if the camera model changes (cosmetic — calibration self-corrects).
    robot_camera_width: int = 1280
    robot_camera_height: int = 720

    # Physical kiosk screen dimensions (meters). NOT consumed by our code — the ROBOT converts the
    # pixel (u,v) we send into a 3D stylus point using its OWN per-kiosk screen pose (AprilTag) and
    # physical dims from ITS /setup config. Kept here (and on Robot Setup) as forward-looking values
    # for when our setup() is wired to upload kiosk definitions. Their RATIO (0.4:0.3 = 4:3) also
    # documents the expected screen aspect the rectified image will have — match viewport_* to it.
    screen_width_m: float = 0.400
    screen_height_m: float = 0.300

    # ─────────────────────────────────────────────────────────────────────────────
    # HOW A TAP STAYS ACCURATE ACROSS CAMERA MODELS (no code change needed)
    # ─────────────────────────────────────────────────────────────────────────────
    # app_map stores each element's center as PIXELS in the exploration image space
    #   (viewport_width × viewport_height). At test time real_robot.tap(x, y) maps that to the
    #   camera's rectified pixel space via _scale(x, y):
    #       scale_x = measured_camera_width  / viewport_width      (per-axis, independent)
    #       scale_y = measured_camera_height / viewport_height
    #       u = x * scale_x ;  v = y * scale_y   → sent to POST /screen/click
    #   The robot then turns (u,v) into a 3D stylus point using its own AprilTag screen pose. So the
    #   ROBOT owns the pixel→metric step; WE only own the viewport→camera pixel step.
    #
    # WHERE THE CAMERA DIMENSIONS COME FROM — DYNAMICALLY, per camera, at runtime:
    #   capture_screen() reads width/height from EVERY /capture response and sets
    #   _calibration["scale_x"/"scale_y"] = measured_camera / viewport. That overrides the
    #   robot_camera_* seed above. A /capture always runs before the first tap (the leading verify
    #   or _ensure_localized), so a real tap uses MEASURED dims, never the seed. GET /robot/health
    #   captures + calibrates and shows the measured width/height/scale on the Robot Setup page.
    #
    # THEREFORE, TO SUPPORT A DIFFERENT CAMERA MODEL: change NOTHING in code. The rectified
    #   resolution is auto-measured and calibration adapts. Optionally update the robot_camera_*
    #   seed on Robot Setup to the new sensor's resolution (pre-calibration cosmetics only), then
    #   re-run Capture+Calibrate. The only accuracy-relevant knob is viewport_* — set it to the same
    #   ASPECT RATIO as the measured rectified frame (Robot Setup's "match viewport to camera" does
    #   this) so the Playwright-explored layout matches what the arm photographs. Per-axis scaling
    #   then absorbs any pure resolution difference exactly.
    # ─────────────────────────────────────────────────────────────────────────────

    # Timeouts (seconds)
    base_move_timeout_s: float = 60.0
    arm_move_timeout_s: float = 30.0
    card_op_timeout_s: float = 30.0
    robot_poll_interval_s: float = 0.5
    # How often to poll /base/state while the AGV is driving to a kiosk. The base move is slow
    # (seconds→tens of seconds), so a 2s cadence gives readable live status without hammering the
    # controller. Separate from the fast arm poll (robot_poll_interval_s) which times sub-second taps.
    base_poll_interval_s: float = 2.0
    # Max time to wait for a SINGLE robot REST call to respond (HTTP request timeout). The physical
    # arm/AGV can be slow to answer; if a command gets no response within this window we abort and
    # fail the step gracefully (see run_vision_step's try/except → Tier-3/fail path). Kept small and
    # configurable so a hung robot never stalls a whole suite. Applies to every real-robot API call.
    robot_response_timeout_s: float = 2.0

    # Per-test AGV positioning gate (real backend only). When True (default), the runner drives the
    # AGV to a test's kiosk BEFORE the test ONLY if the test's steps explicitly ask to move the base
    # (words like "move to", "go to", "navigate to", "drive to", referencing a kiosk/device/AGV/home).
    # A test that never mentions moving (e.g. a single-kiosk sign-in) leaves the robot where it is —
    # so the robot is assumed already parked at the target kiosk. Explicit `move` PLAN steps are
    # unaffected (they always drive the base). Set False to restore the old always-position behaviour.
    agv_move_requires_explicit_step: bool = True

    # Management API (FastAPI server for management frontend)
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    db_url: str = "sqlite:///./management.db"  # swap to postgresql://... for production


settings = Settings()
