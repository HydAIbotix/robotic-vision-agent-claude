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

    # Storage backend. "local" for dev; "s3" now covers ANY S3-compatible object store —
    # AWS S3, MinIO (self-hosted), Google Cloud Storage or Azure Blob via their S3 gateways —
    # selected purely by s3_endpoint_url below. "minio"/"gcs"/"azure" are readable aliases for
    # the same S3 code path (see vision_agent/storage/__init__.py). Zero agent code changes.
    storage_backend: Literal["local", "s3", "minio", "gcs", "azure"] = "local"
    s3_bucket: str = ""
    s3_prefix: str = "vision-agent"
    sqs_queue_url: str = ""  # SQS queue for robot image events (AWS-only; unused on other clouds)

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
    # localStorage keys to PRESERVE across the between-test reset (reset_to_entry clears storage so
    # each test starts unauthenticated / not mid-flow). Any key whose name CONTAINS one of these
    # (case-insensitive, comma-separated) substrings is kept — so a smart card issued in one test is
    # still present for a later top-up / balance-check test when the shared card service is offline
    # (cards then live only in the kiosk's localStorage). Config-driven so no app-specific key is
    # hardcoded in the robot layer. Blank → clear everything (original behaviour).
    reset_preserve_storage_keys: str = "smart-cards,cardbalance"
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

    # Demo mock card for exploring the card-payment flow. The App Explorer needs to complete ONE mock
    # card payment to discover the payment/success/order_history screens, but the kiosk (RPS) requires
    # an ISSUED card. Rather than a fragile cross-kiosk "issue at VPS then pay at RPS" dance, the kiosk
    # app seeds an always-available DEMO smart card (the card analogue of the demo login) that this
    # number identifies. When `explore_demo_card` is on (set by run_explorer.py at startup — exploration
    # only, never test execution), the exploration URL gets ?demoCard=1 so the RPS mock-card field is
    # pre-filled with this number, and the walkthrough types it as a backup. Keep this in sync with
    # DEMO_SMART_CARD_NUMBER in the kiosk app's src/lib/storage.ts.
    demo_card_number: str = "4111111111110001"
    explore_demo_card: bool = False

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
    # AGV map position name sent as the /base/goto `target` for a "go home" / "return to base" step.
    # The robotics team's AGV map names its dock positions arbitrarily (e.g. "home-Aug-14-G37"); this
    # is decoupled from any kiosk_id join key. Blank/unset → the literal "home" (historical behaviour).
    # Editable from Robot Setup (PATCH /api/config/robot) and persisted to .env as AGV_HOME_TARGET.
    agv_home_target: str = "home"

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

    # Demo hold — seconds to keep the Playwright browser open AFTER the final step of a run, before
    # closing it, so a live demo can show the last screen. Applies ONLY to the playwright backend's
    # end-of-run teardown (robot.stop()); it does NOT affect execution timing, step logic, or the
    # demo/real backends. 0 = close immediately (legacy behaviour). Set PLAYWRIGHT_DEMO_HOLD_S in .env.
    playwright_demo_hold_s: float = 5.0

    # Demo display scale — device-scale-factor for the visible Playwright window, so the app renders
    # LARGE on a high-res demo monitor (e.g. 2.0 fills a 3840×2160 4K display from the 1920×1080
    # coordinate space). This scales ONLY the on-screen rendering: the CSS viewport (app_map coordinate
    # space) stays viewport_width×viewport_height, taps run in CSS-pixel space, and screenshots are
    # pinned to CSS resolution (scale="css"), so tap accuracy, template-match, OCR and Tier-3 vision are
    # byte-for-byte unaffected. 1.0 = legacy behaviour (no scaling). Set PLAYWRIGHT_UI_SCALE in .env.
    playwright_ui_scale: float = 1.0
    # Headless Chromium for Playwright exploration + test runs. Default False keeps the visible
    # demo window (HUD overlay, fullscreen) on a desktop with a display. Set PLAYWRIGHT_HEADLESS=true
    # for headless servers / containers with no X server (e.g. a cloud VM) — otherwise Chromium fails
    # to launch with "Missing X server or $DISPLAY". Tap/screenshot math is display-independent, so
    # headless is byte-for-byte equivalent for exploration and execution.
    playwright_headless: bool = False
    # robot_camera_* = a PRE-CALIBRATION SEED for the rectified /capture resolution, NOT the live
    #   value. Default 1280×720 = the Intel RealSense D405's native resolution (16:9). It is only used
    #   as the viewport→camera scale numerator BEFORE the first /capture; every /capture response then
    #   reports the ACTUAL rectified width/height and real_robot._scale switches to those measured
    #   dims (see the calibration note below), so this seed never affects a real tap. Editable on the
    #   Robot Setup page; update it if the camera model changes (cosmetic — calibration self-corrects).
    robot_camera_width: int = 1280
    robot_camera_height: int = 720

    # ── Camera tap calibration (per-axis AFFINE in FRACTION space) ──────────────────────────────
    # real_robot._scale maps an app_map viewport pixel → camera pixel. By default it assumes the
    # rectified /capture frame is a FAITHFUL full-screen image (camera_frac == monitor_frac). Measured
    # on hardware 2026-08-07, it is NOT: the arm /capture type=screen frame is a VERTICAL CROP of the
    # display (it spans only the AprilTag region, ~monitor rows 3..894 of 1080 — full width but partial
    # height), so an element's camera_frac_y is a linear-but-DIFFERENT function of its monitor_frac_y.
    # Concretely the email field (monitor y-frac 0.470) landed at camera y-frac 0.470 (tap on the
    # "Email" LABEL) when its TRUE camera y-frac was ~0.566 (the input box centre) — ~67px too high.
    # The mapping is AFFINE and stable for a fixed arm/camera/screen pose:
    #     camera_frac_axis = calib_a_axis * monitor_frac_axis + calib_b_axis
    # These 4 knobs apply that correction in _scale. DEFAULTS (a=1, b=0) are a NO-OP — byte-identical to
    # the old behaviour, so playwright/demo and any un-calibrated real setup are unchanged. Horizontal
    # measured faithful (a=1, b=0); only vertical needs correction on this rig. Values are FRACTIONS, so
    # they are resolution-independent (work whether /capture is 1405×579 or 1382×571). Re-derive with
    # `python calibrate_tap.py` after ANY change to the arm/camera pose or the kiosk screen. NB: a single
    # global calibration fits ONE physical setup — a multi-kiosk rig where each kiosk has a different
    # camera geometry would need per-kiosk values (future). ROOT cause is the robot rectification not
    # being a faithful full-screen deskew; this compensates for it on our side. See CLAUDE.md.
    camera_calib_ax: float = 1.0
    camera_calib_bx: float = 0.0
    camera_calib_ay: float = 1.0
    camera_calib_by: float = 0.0

    # Runtime SELF-CALIBRATION of the vertical mapping (real backend). A fixed camera_calib_ay/by fit to
    # one pose mis-places taps at the next pose, because the robot's rectified /capture crop VARIES per
    # arm/camera pose (captures of the same login screen have come back at aspect 2.42:1 AND 1.06:1). When
    # ON, the login screen's own email+password input boxes are detected in the live camera frame and the
    # per-pose vertical affine is derived from them (see vision/screen_calibrate.py), overriding
    # camera_calib_ay/by for that pose. Strictly bounded + confidence-gated: a low-confidence detection is
    # rejected and the configured camera_calib_ay/by is used instead, so this can never do worse than the
    # static calibration. Set False to force the static camera_calib_* only. Playwright/demo ignore it.
    auto_tap_calibration: bool = True

    # ── VIRTUAL-KEYBOARD tap coordinates (real backend) ──────────────────────────────────────────
    # The on-screen keyboard's keys are stored in the app_map keyboard_map as NORMALIZED viewport
    # fractions and are converted to camera pixels through the SAME per-pose calibration as every other
    # element (real_robot._scale_key defers to _scale). The keyboard occupies monitor y≈0.68–0.86, a range
    # the login 4-anchor vmap already brackets with its sign-in (≈0.72) and FOOTER (≈0.84) knots — the
    # footer knot lands almost exactly on the keyboard's bottom row — so the keyboard's VERTICAL mapping is
    # interpolated by the login vmap (this is why the footer-anchor fix also fixes keyboard accuracy), not
    # a separate calibration. Horizontal uses camera_calib_ax/bx (identity), correct when the operating
    # camera pose frames the arm-reachable box at the same fraction as the exploration viewport (the
    # documented "match the viewport aspect / observe pose" setup); a different zoom shifts off-centre keys
    # and is a pose/setup issue, with Tier-3 vision as the runtime fallback. No keyboard-specific knobs.

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
    # DEPRECATED / UNUSED for the AGV base (2026-08-18). The base move no longer has a client-side
    # deadline: AGV travel time is unknown and varies with distance/traffic, and a 60s deadline once
    # aborted a base that was still closing in (0.89m out) on a "go home" move — see CLAUDE.md
    # "AGV move: no self-abort / no client-side timeout". _poll_base now waits for the controller to
    # report a READY state, failing only on an "error" state or an unreachable controller. Field kept
    # so BASE_MOVE_TIMEOUT_S in an existing .env still parses; nothing reads it now.
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
    # (tens of seconds→minutes) and has NO client-side deadline, so a relaxed 10s cadence gives
    # readable live status without hammering the controller during a long drive. Separate from the
    # fast arm poll (robot_poll_interval_s) which times sub-second taps. Configurable via
    # BASE_POLL_INTERVAL_S.
    base_poll_interval_s: float = 10.0
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
    # A failed `verify` step is a test ASSERTION failure — the app is genuinely not in the expected
    # state. When True (default), a failed verify FAILS the test immediately instead of handing off to
    # Tier-3 vision, which would re-attempt the preceding actions (e.g. re-login) the test never asked
    # to retry (observed: TC-RPS-001 on a broken login looped through 3 re-login attempts). Action
    # steps (tap/type) that fail still hand off to Tier-3 to locate the element via vision — that
    # COMPLETES the step, it is not an outcome retry. Applies to ALL backends (playwright/real/demo).
    # Set False to restore the old always-Tier-3 behaviour. See CLAUDE.md.
    verify_failure_stops_run: bool = True
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

    # ── Auto-Repair agent (RAG + Claude code repair) ─────────────────────────
    # The self-healing arm of defect intelligence: retrieve the offending code from a local
    # Chroma vector index (built by repair_agent/parse_code_and_store.py — Chroma + HuggingFace
    # embeddings, kept exactly as the POC's ParseCodeAndStore.py), ask Claude for ONE minimal
    # find/replace patch, apply it, lint (unit test) + build, then prepare a PR branch. All paths
    # are config-driven (no hardcoded C:\ paths) so the module is portable across machines.
    #   repair_codebase_dir — the app under test that the agent edits AND opens a PR against. This is
    #        the LIVE kiosk app (a git clone with a GitHub remote), the same code the tests drive —
    #        NOT a copy — so a fix is real and PR-able. Relative paths resolve from the repo root.
    #   repair_docs_dir     — folder holding the design doc + test-case workbook artifacts (moved here
    #        from the POC's own folder); indexed alongside the code so the RAG has product context.
    #   repair_persist_dir  — where the Chroma index is persisted (gitignored generated data).
    #   repair_embedding_model — HuggingFace sentence-transformers model (unchanged from the POC).
    #   repair_pr_remote / repair_pr_base — git remote + base branch a prepared PR targets.
    repair_codebase_dir: str = "../Kiosk_App/robotics-kiosk-pos"
    repair_docs_dir: str = "./docs"
    repair_persist_dir: str = "./docs/chroma_code_db"
    repair_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Auto-run the repair agent when a test run has failures (the first failed test), and
    # auto-raise the PR (push branches + open GitHub's prefilled compare/PR page — `gh` is not
    # required). Both default ON per the demo; set to False to keep repair fully manual.
    auto_repair_on_failure: bool = True
    repair_auto_pr: bool = True
    repair_pr_remote: str = "origin"
    # GitHub token (repo scope) used to (a) authenticate `git push` of the fix branch and (b) CREATE
    # the PR via the GitHub REST API when `gh` isn't installed (the container has no gh). Blank →
    # open_pull_request falls back to returning the prefilled compare URL (no push/auto-create).
    # Set GITHUB_TOKEN in .env (gitignored) — never commit it.
    github_token: str = ""
    # Branch a prepared PR targets (merge-INTO). For the demo this is the isolated branch that
    # carries the intentional bug, so the fix produces a real, reviewable diff without ever touching
    # the working `arm-reachable-area` branch. Point REPAIR_PR_BASE at your real base branch for
    # production use (bugs that landed on that branch → the agent's fix PR merges back into it).
    repair_pr_base: str = "demo/rps-login-bug"

    # ── Auto-Repair DIAGNOSE model selection ──────────────────────────────────────────────────
    # The repair pipeline makes exactly ONE LLM call (DIAGNOSE → propose_patch). This toggle picks
    # which model that call TRIES FIRST; the other acts as an automatic backup, and the deterministic
    # demo rule (`_demo_fallback_patch`) is always the last resort. Set from the Configuration page.
    #   "claude" (default) — Claude Opus 4.8 primary; local LLM used only if Claude is unreachable.
    #   "local"            — the local Ollama model primary (used to TEST the backup path); Claude backup.
    # The local backend is Ollama (http, no cloud). It stays OFF unless explicitly selected or reached
    # as a fallback, and `langchain_ollama` is imported LAZILY — so nothing changes / no new hard
    # dependency until you use it. Requires `pip install langchain-ollama` + an Ollama server with the
    # model pulled (`ollama pull qwen2.5-coder:14b`).
    repair_llm_backend: str = "claude"          # claude | local
    repair_local_model: str = "qwen2.5-coder:14b"
    repair_local_base_url: str = "http://localhost:11434"
    repair_local_num_ctx: int = 8192            # context window for the local model (tokens)
    repair_local_timeout_s: int = 120           # per-call timeout for the local model
    # Hard wall-clock cap on EACH provider's DIAGNOSE call so a stuck/slow model (e.g. a cold local
    # Ollama load, or a hung request) can never freeze the repair — on timeout the chain moves to the
    # next provider, then the demo fallback. Keep < repair_local_timeout_s is fine; this is the outer
    # guarantee regardless of whether the provider honours its own timeout.
    repair_diagnose_timeout_s: int = 90

    # Management API (FastAPI server for management frontend)
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    db_url: str = "sqlite:///./management.db"  # swap to postgresql://... for production

    # ══════════════════════════════════════════════════════════════════════════════════════
    # CLOUD-AGNOSTIC DEPLOYMENT  (branch: cloud-agnostic-agent — see docs/CLOUD_AGNOSTIC_DECISION.md)
    # ──────────────────────────────────────────────────────────────────────────────────────
    # Every managed-AWS component the `aws-based` branch used has an open-source, run-anywhere
    # equivalent, selected here BY CONFIG ALONE (ports-and-adapters / hexagonal). Claude is
    # untouched — it stays a remote call via VISION_BACKEND (anthropic|bedrock). Defaults below
    # reproduce the pure-local MVP byte-for-byte, so NOTHING changes until a backend is switched:
    #   persistence : sqlite (default) | postgres   — relational metadata (db_url picks the engine)
    #   object store: local  (default) | s3|minio|gcs|azure — blobs, all via the S3-compatible API
    #   event bus   : memory (default) | redis      — cross-replica realtime fan-out (scale-out)
    #   tenancy     : single (default) | multi       — tenant_id isolation (pooled SaaS vs dedicated)
    #   runtime     : docker (default) | k8s         — same image; only the orchestrator differs
    # This is the agnostic analogue of AWS DynamoDB / S3 / API-GW-WebSocket / Step-Functions /
    # AgentCore, so the SAME codebase deploys on Azure, GCP, EC2 or on-prem with no lock-in.

    # --- Persistence (relational metadata: runs, results, defects, config, test cases) ---
    # db_url (above) already selects the SQLAlchemy engine. persistence_backend is a readable
    # alias surfaced in /health so the active store is obvious; keep it in sync with db_url.
    #   sqlite   → db_url = sqlite:///./management.db   (dev / single box, MVP default)
    #   postgres → db_url = postgresql+psycopg://user:pass@host:5432/kioskqa   (any cloud / RDS-equiv)
    persistence_backend: Literal["sqlite", "postgres"] = "sqlite"

    # --- Object store (blobs) — extra knobs, all INERT when blank (real AWS default cred chain) ---
    s3_endpoint_url: str = ""          # e.g. http://minio:9000  (blank = real AWS S3 endpoint)
    s3_region: str = ""                # e.g. us-east-1 / any (MinIO ignores it)
    s3_access_key_id: str = ""         # blank = fall back to the default provider cred chain
    s3_secret_access_key: str = ""
    s3_use_path_style: bool = True     # MinIO / most self-hosted S3 need path-style addressing
    # Mirror produced artifacts (app_map, exploration screenshots, cached plans, run results) to the
    # S3-compatible object store as the DURABLE record — the cloud-agnostic equivalent of writing to
    # S3. Playwright/OpenCV need LOCAL files, so local disk stays the working store and this archives
    # to MinIO/S3 at each activity boundary (see ports/archive.py). Uploads use the s3_* settings
    # above. Default off (pure-local MVP). Set ARCHIVE_TO_OBJECT_STORE=true to populate MinIO.
    archive_to_object_store: bool = False

    # --- Event bus (realtime WebSocket + cross-worker fan-out) ---
    # memory = in-process (single server, = MVP behaviour). redis = pub/sub so N API/worker
    # replicas behind a load balancer share live run/step/repair events (required for scale-out).
    event_bus_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = ""                # e.g. redis://redis:6379/0

    # --- Multi-tenancy ---
    # single = one implicit tenant (default_tenant_id) — the MVP behaviour. multi = tenant_id is
    # read per request (X-Tenant-Id header / JWT claim) and prefixes every object-store key and
    # DB row for isolation. Works identically whether customers share our cloud (pooled) or each
    # runs a dedicated deployment in their own cloud (tenant_id then separates their sub-teams).
    multi_tenant_enabled: bool = False
    default_tenant_id: str = "default"
    # Tenant resolution from a signed JWT (optional). When off, the tenant comes from the
    # X-Tenant-Id request header. When on, a `Authorization: Bearer <jwt>` token is decoded and the
    # tenant is read from `tenant_jwt_claim`; the header is the fallback when no token is present.
    # PyJWT is imported lazily, so this adds no dependency until enabled.
    tenant_jwt_enabled: bool = False
    tenant_jwt_secret: str = ""              # HS256 shared secret (or PEM public key for RS*)
    tenant_jwt_algorithms: str = "HS256"     # comma-separated allowed algorithms
    tenant_jwt_claim: str = "tenant_id"      # the claim carrying the tenant id
    tenant_jwt_audience: str = ""            # optional `aud` to verify (blank = don't verify aud)

    # --- Deployment / scaling (informational; consumed by /health + infra, not by hot paths) ---
    deployment_mode: str = "docker"    # docker | k8s   — which manifests you applied
    service_role: str = "all"          # all | api | worker — split roles when scaling processes out

    # --- Task queue (API↔worker split for horizontal scale-out; AgentCore-Runtime analogue) ---
    # inline = the API runs each suite in an in-process thread (MVP, single node). redis = the API
    # enqueues a job and a separate SERVICE_ROLE=worker process consumes it (decouples the tiers).
    task_queue_backend: Literal["inline", "redis"] = "inline"
    task_queue_key: str = "kioskqa:runs"

    # --- Orchestration (durable workflow engine; Step-Functions analogue) ---
    # inprocess = dispatch via the task queue above (default). temporal = run the suite as a durable
    # Temporal workflow/activity (survives worker restarts, automatic retries). Optional.
    orchestrator_backend: Literal["inprocess", "temporal"] = "inprocess"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "kioskqa"

    # --- Tracing / observability (CloudWatch-GenAI analogue; vendor-neutral) ---
    # none = off (MVP). otel = OpenTelemetry spans → any OTLP collector. langfuse = LLM-native traces
    # via the LangChain callback handler. Lazy imports; nothing added until selected.
    tracing_backend: Literal["none", "otel", "langfuse"] = "none"
    otel_exporter_endpoint: str = ""          # OTLP endpoint, e.g. http://otel-collector:4317
    otel_service_name: str = "kioskqa"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Semantic memory (Explorer screen memory / agent recall; AgentCore-Memory analogue) ---
    # none = off (MVP; app_map is the durable memory). chroma = local vector store. pgvector = a
    # Postgres + pgvector collection (shares the DB). Populated by the explorer when enabled.
    memory_backend: Literal["none", "chroma", "pgvector"] = "none"
    memory_collection: str = "kiosk_memory"
    memory_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    memory_persist_dir: str = "./docs/chroma_memory"   # chroma backend only
    pgvector_url: str = ""                              # blank → derive from db_url


settings = Settings()
