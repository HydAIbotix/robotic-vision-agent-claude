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
    # Kiosk display layout forced during Playwright EXPLORATION (and playwright test runs) via a
    # ?screenLayout=… query param, so app_map coordinates are always learned at the SAME layout the
    # physical kiosk shows — immune to stale browser localStorage. The robotics-kiosk-pos app supports:
    #   "arm-reachable" (default) — the ~40%-width centered box that fits the robot arm's reachable
    #                                area (the mode the physical kiosk runs in for real-robot tests).
    #   "standard"                — the legacy full-width kiosk layout (manual/pure-playwright demos).
    #   ""                        — do not append the param (use whatever the app/localStorage decides).
    # The query param wins over the app's localStorage, so this is deterministic. See CLAUDE.md
    # "Arm-reachable kiosk layout".
    kiosk_screen_layout: str = "arm-reachable"

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
    #   template_match_center_crop_x / _y — focus the correlation on the CENTER of the frame before
    #        scoring (fraction of width / height kept, centered). Real arm cameras return LOOSELY-framed
    #        frames: even the rectified /capture keeps desk/bezel/taskbar margins around the ~40%-centered
    #        arm-reachable kiosk box, and that shared background dominates a full-frame correlation — a
    #        real login frame then scored order_history/payment_successful ABOVE sign_in. Cropping BOTH
    #        the live frame and every reference to the central app region (validated: flips login from
    #        rank #4 → #1 while products stays #1) removes the shared margin so screen CONTENT decides.
    #        1.0×1.0 = full-frame (legacy). Applied ONLY to the real-robot camera template paths
    #        (playwright/demo use DOM/always-true). Best paired with a consistent close "observe" pose so
    #        references and live frames share geometry. Tune toward 1.0 if the /capture is tightly cropped
    #        to the screen (then the app already fills the frame and less/no crop is needed).
    template_match_center_crop_x: float = 0.6
    template_match_center_crop_y: float = 0.92

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
    # Overall deadline for ONE arm click sequence (hover PTP → linear descend → touch → ascend →
    # return).  The myCobot 280 is slow: a single Cartesian move can take 4-17s and a full click
    # sequence 25-45s (observed on hardware 2026-07-29 — the ascend alone took 17.3s, pushing the
    # total past the old 30s deadline so we aborted a click that had ALREADY touched the screen).
    # 60s covers a full slow sequence with margin; a genuinely stuck arm still fails (state "error"
    # is immediate, and a never-terminal arm times out at this deadline).  Configurable via
    # ARM_MOVE_TIMEOUT_S.
    arm_move_timeout_s: float = 60.0
    # Per-key budget for on-screen-keyboard typing. type_text batches N key taps into ONE
    # /screen/click, and the arm executes them SEQUENTIALLY — each key is its own hover→descend→
    # touch→ascend mini-sequence (~5-15s on the myCobot 280).  So the type poll deadline must scale
    # with the number of keys, NOT the old `+ len*0.1` (0.1s/key, which timed out mid-word).  Total
    # type deadline = arm_move_timeout_s + N * arm_key_tap_timeout_s.  Configurable via
    # ARM_KEY_TAP_TIMEOUT_S; a stuck arm still fails (state "error" is immediate).
    arm_key_tap_timeout_s: float = 15.0
    card_op_timeout_s: float = 30.0
    robot_poll_interval_s: float = 0.5
    # Arm-state polling (spec-aligned): a command is DONE "once state is no longer moving". If the
    # POST ack said 'moving' we wait to observe a moving sample before accepting a terminal state, so
    # a stale pre-command 'ready' can't be misread as completion; arm_settle_grace_s bounds that wait
    # (also the max wait for a command that never enters 'moving'). arm_status_tick_s is how often a
    # consolidated [ROBOT ARM] status line is pushed to the live monitor DURING a long move/type
    # (instead of dozens of raw GET lines).
    arm_settle_grace_s: float = 2.0
    arm_status_tick_s: float = 3.0
    # How often to poll /base/state while the AGV is driving to a kiosk. The base move is slow
    # (seconds→tens of seconds), so a 2s cadence gives readable live status without hammering the
    # controller. Separate from the fast arm poll (robot_poll_interval_s) which times sub-second taps.
    base_poll_interval_s: float = 2.0
    # Max time to wait for a SINGLE robot REST call to respond (HTTP request timeout). The physical
    # arm/AGV can be slow to answer; if a command gets no response within this window we abort and
    # fail the step gracefully (see run_vision_step's try/except → Tier-3/fail path). Kept small and
    # configurable so a hung robot never stalls a whole suite. Applies to every real-robot API call.
    robot_response_timeout_s: float = 2.0
    # Max time to keep RETRYING a state poll while the robot REST API is UNREACHABLE (connection
    # refused / read timeout on every GET). _poll otherwise swallows transient read errors and retries
    # until the (possibly long) command deadline — observed 2026-08-07: when the robot machine's API
    # was DOWN mid-run, a 19-key type poll (deadline 60 + 19*15 = 345s) kept retrying for the FULL
    # 345s before giving up. This caps the "API is down" case: once the poll has seen only connection
    # errors for this long, it fails fast with a clear "robot unreachable" error. A robot that is UP
    # and genuinely moving (successful 'moving' reads) is unaffected — it uses the full command
    # deadline. Configurable via ROBOT_UNREACHABLE_TIMEOUT_S.
    robot_unreachable_timeout_s: float = 60.0
    # When a real-robot ACTION fails with a genuine ROBOT/HARDWARE fault (a motion-planning failure
    # like DESCEND_LIN_FAILED / HOVER_FAILED, a click that did-not-land, an API timeout, or the API
    # being unreachable), STOP the run instead of handing off to Tier-3 vision. Tier-3 would just
    # drive the SAME faulted arm — which may be stuck and unable to return home — so retrying is
    # pointless and potentially unsafe. Coordinate/plan misses (wrong/absent coords, screen mismatch)
    # are NOT robot faults and still fall through to Tier-3. Real backend only (playwright/demo keep
    # the Tier-3 handoff). Set False to restore the old always-Tier-3 behaviour. See CLAUDE.md.
    robot_error_stops_run: bool = True
    # Save an annotated BEFORE screenshot (the last camera frame with a crosshair at the exact camera
    # pixel the arm will touch) and the AFTER frame (the /screen/click response image) for every real
    # tap, into the run's per-run screenshots folder with identifiable names (before_<cmd>_at_<u>-<v>.png
    # / after_<cmd>.jpg). Diagnostic aid for verifying tap accuracy; real backend only. Set False to skip.
    save_click_screenshots: bool = True
    # POST /capture is BLOCKING and runs the full perception pipeline — which per the spec MOVES the
    # arm to an inspection pose first, then does AprilTag detection + rectification. That arm move can
    # take several seconds (much longer than the 2s per-call timeout), especially right after a failed
    # tap when the arm is not already at the inspect pose. Give /capture its own generous read timeout
    # so the leading verify + Tier-3 handoff captures don't spuriously read-time-out. Configurable via
    # CAPTURE_TIMEOUT_S. The robot itself returns 504 if its internal capture genuinely times out.
    capture_timeout_s: float = 30.0

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
