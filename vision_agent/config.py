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
    # Optional sampling temperature for Claude calls. Leave None (default) — Claude Opus 4.8 DEPRECATED
    # the `temperature` param and returns 400 if it is sent, so we must NOT pass it. Plan CONSISTENCY is
    # instead guaranteed by the content-based app_map version_hash (a cached plan is reused verbatim
    # across re-explores) + the "authenticate first" planner rule, so temperature is not needed for
    # determinism. Set a numeric value ONLY for an older model that still accepts it.
    llm_temperature: float | None = None

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
    demo_card_number: str = "0005322931"
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
    # EXCEPTION to the above: when a verify fails purely because we are on the WRONG SCREEN (a screen
    # mismatch, not a text/value assertion), that is almost always a MISSING NAVIGATION step in the
    # generated plan — recoverable by Tier-3 vision navigating to the objective from the current screen.
    # When True (default), such screen-only misses DO hand off to Tier-3 (generic recovery for any plan
    # gap / any app), while text/value mismatches on the RIGHT screen stay terminal (genuine assertions
    # Tier-3 can't fix by navigating). This is what makes Claude-planning + Tier-3 robust without any
    # app-specific step-injection. Set False to make every verify failure terminal.
    verify_wrong_screen_recovers: bool = True
    # OPTION C — the "gap vs defect" JUDGE (Claude vision). A wrong-screen verify is AMBIGUOUS: it can be
    # a recoverable missing-navigation GAP in the plan (bridge with Tier-3, above) OR a genuine app DEFECT
    # (the app refused/errored — e.g. an add-to-cart that popped a "Quantity Required" dialog instead of
    # advancing to the cart). Only a look at the screen can tell them apart. When True (default) and a
    # wrong-screen verify would otherwise be bridged, we ask Claude ONE strict question: is this a nav gap
    # or a real defect? A confident DEFECT verdict fails the test FAST (no Tier-3 replanning) so Auto-Repair
    # targets the real bug; anything else (gap, or unsure) bridges exactly as before → NO regression. This
    # uses the vision LLM (always Claude), so it is on the Claude path only. The deterministic, no-LLM
    # equivalents (Option A: classify the actual screen as a blocking/error state; Option B: bound the
    # bridge and self-terminate) are documented in CLAUDE.md for the local/air-gapped path. Set False to
    # restore the pure "always bridge a wrong-screen miss" behaviour.
    verify_defect_judge: bool = True
    # OPTION C tuning — fail a wrong-screen verify FAST (→ Auto-Repair) more readily, instead of letting a
    # Tier-3 bridge silently MASK a real defect by re-doing an action the plan already performed. Two levers,
    # both default ON (a deliberate choice: surface planted/real defects rather than auto-recover past them):
    #  (1) JUDGE CONFIDENCE — only BRIDGE a wrong-screen miss when the vision judge is AT LEAST this confident
    #      it is a recoverable navigation GAP. A lower-confidence "gap" (or any "defect") fails fast. Ordered
    #      high > medium > low; "high" = strictest (bridge only when sure). Lower it toward "low" to bridge
    #      more readily (closer to the pre-tuning always-bridge behaviour).
    #      DEFAULT "medium" (2026-09-24): a "high"-only bar failed a legit navigation gap that Tier-3 normally
    #      recovers — the RPS payment "Use Mock Card" reveal, where the judge (correctly) sees a healthy screen
    #      but is only MEDIUM-confident it's a gap. Blocking that made a verification RETEST fail where a normal
    #      run passes — the flow must NOT change just because it's a retest. "medium" bridges medium/high gaps
    #      (consistent normal↔retest recovery) while DEFECT verdicts (error/refusal popups) and LOW-confidence
    #      gaps still fail fast, so real/planted bugs (e.g. text-assertion failures, error popups) are unaffected.
    verify_bridge_min_confidence: str = "medium"
    #  (2) UNRESPONSIVE-INTERACTION RULE (deterministic, no LLM) — if the plan's immediately-preceding
    #      interaction (a tap/type with a known screen_id) was performed on the SAME screen the app is STILL
    #      showing at the failed verify, that interaction did NOT advance the flow (an unresponsive / blocked /
    #      refused control — e.g. add-to-cart that popped a popup and stayed on 'products'). Treat that verify
    #      as a DEFECT and fail fast, rather than bridging. Runs BEFORE the judge (cheap + authoritative — the
    #      screen factually didn't change). Set False to disable.
    verify_unresponsive_interaction_defect: bool = True
    # ── HUMAN-IN-THE-LOOP REVIEW (2026-09-21) — optional Approve/Reject gates at key stages ──────────
    # All default OFF: when off, every flow runs EXACTLY as before (zero behaviour change → no regression).
    # When on, the Studio shows Approve/Reject at that stage; a Reject captures a free-text reason that is
    # fed back to improve the NEXT attempt (and, for RCA, can retry in place). Toggle from the Studio
    # Configuration page (persisted to .env). See CLAUDE.md "Human review".
    human_review_explorer: bool = False    # gate after App Explorer finishes (blocks Test Plan until approved)
    human_review_test_plan: bool = False   # gate after a Test Plan is generated (reason feeds a Regenerate)
    human_review_rca: bool = False         # gate after the RCA verdict, BEFORE the code-fixing agent runs
    # A rejected exploration's reason, passed (env EXPLORE_REVIEW_FEEDBACK) into the NEXT explore subprocess
    # and folded into the explorer's action-suggestion prompt so it improves the output. Blank = no effect.
    explore_review_feedback: str = ""
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
    # The specific artifacts under repair_docs_dir that are indexed alongside the code (filenames,
    # relative to repair_docs_dir). Configurable so a deployment points at its OWN spec/tests without a
    # code change — e.g. the expanded POS keeps them in the mounted repo under docs/Expanded_Version.
    # repair_requirements_doc is optional (empty = none); design + test-cases keep the historical names.
    repair_design_doc: str = "Kiosk_POS_and_SmartCardStation_Production_Design.docx"
    repair_requirements_doc: str = ""      # optional 2nd .docx (e.g. RPS_VPS_Expanded_Requirements.docx)
    repair_test_cases: str = "kiosk_e2e_tests.xlsx"
    repair_persist_dir: str = "./docs/chroma_code_db"
    repair_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Auto-run the repair agent when a test run has failures (the first failed test), and
    # auto-raise the PR (push branches + open GitHub's prefilled compare/PR page — `gh` is not
    # required). Both default ON per the demo; set to False to keep repair fully manual.
    auto_repair_on_failure: bool = True
    repair_auto_pr: bool = True
    # After a green build, RE-RUN the failed test to VERIFY the fix before raising the PR — the PR is
    # opened ONLY if the retest passes (the user-requested "fix → retest → PR" loop). The retest is a
    # REAL run (a new TestRun visible in Results/history + the run summary), streamed live into an
    # overlay window that closes when it finishes. When True, the auto path prepares the PR during the
    # pipeline and opens it here after a passing retest; a failing/unrunnable retest leaves the branch
    # PREPARED (open it manually from the repair card) and never auto-raises. When False, behaviour is
    # byte-for-byte the pre-2026-09-24 flow (PR auto-opens right after the build). NOTE: the retest can
    # only PASS if the RUNNING app serves the fixed code (a dev server on the codebase, or a rebuild of
    # the app image from the fix branch) — otherwise it re-observes the same bug and the PR stays gated.
    repair_retest_before_pr: bool = True
    # Re-run the WHOLE original suite (in the operator's order) for the verification retest, not just the
    # one failed test — then gate the PR on THAT test now passing. Default True because E2E suites are
    # inter-dependent: a test often validates state created by EARLIER tests (e.g. TC-VPS-009 checks a card
    # balance/'PURCHASE' transaction that an earlier RPS payment test created, across the VPS+RPS+card-service
    # subsystems). Re-running the failing test alone can't reproduce that lifecycle, so a correct fix would
    # still "fail" the retest. False → re-run only the failing test (cheaper; correct only for independent
    # tests). Cost note: with the per-failure loop this re-runs the suite once per fixed test.
    repair_retest_full_suite: bool = True
    # REBUILD/redeploy the app-under-test with the just-applied fix BEFORE the verification retest, so the
    # retest browser actually hits the FIXED code (a retest is only meaningful against a build containing
    # the fix). DEFAULTS to the GCP-VM compose rebuild of the POS service — the POS there is a built nginx
    # image (Dockerfile: `npm run build` → COPY dist), and the build context is the working tree, which
    # carries the fix committed to the repair branch, so this picks it up. It runs in the POS repo
    # (repair_codebase_dir) through the shell (operator-trusted config, not user input), AFTER the fix is
    # committed.
    #   ⚠️ LOCAL DEV (`npm run dev`): set REPAIR_REBUILD_CMD="" (empty) in your local .env — the Vite dev
    #   server already serves the patched working tree live, so no rebuild is needed and running compose
    #   locally would be wrong. Empty also restores the pre-2026-09-24 retest behaviour exactly.
    repair_rebuild_cmd: str = "docker compose up -d --build pos"
    repair_rebuild_timeout_s: int = 900          # docker build + up can be slow on a small VM
    # After a rebuild, wait until the app URL answers before retesting (nginx needs a moment to come up).
    repair_rebuild_ready_timeout_s: int = 90
    # After the retest, RESTORE the app to the branch the run started on (checkout the PR base + rebuild),
    # so the suite's baseline is preserved and the repo isn't left on a throwaway repair branch — the fix
    # lives in the PR (the proper integration path). Only acts when repair_rebuild_cmd is set (the VM
    # path); the local dev-server path is untouched. Set False to LEAVE the fixed build deployed (e.g. to
    # show the now-passing app after a demo repair).
    repair_rebuild_restore: bool = True
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
    # AIR-GAP: when True, DIAGNOSE uses ONLY the local model (then the deterministic demo fallback) and
    # NEVER calls Claude — so no code or defect text ever leaves the environment, even if the local
    # model is slow, errors, or is unreachable (it fails to the demo rule, not to the remote model).
    # Requires the local stack (repair_llm_backend=local + an Ollama server). Default False keeps the
    # standard behaviour where Claude backs up the local model. Only meaningful with the local stack.
    repair_local_only: bool = False
    repair_local_model: str = "qwen2.5-coder:14b"
    repair_local_base_url: str = "http://localhost:11434"
    # Context window for the LOCAL diagnose model (tokens). The assembled DIAGNOSE prompt (rules +
    # failure + retrieved whole-function/design blocks) can be large; if it exceeds this, Ollama silently
    # TRUNCATES it and the model may never see the buggy code — so retrieve_context now trims the context
    # to fit the EFFECTIVE window (no truncation at any setting). Bigger = more context kept. ⚠ VRAM: the
    # KV cache lives on the GPU and grows with num_ctx. 14B/7B have ample headroom at 16384. A ~32B model
    # is large enough that this window is clamped for VRAM safety — see repair_local_num_ctx_cap_large.
    # Env-overridable via REPAIR_LOCAL_NUM_CTX. (Use vision_agent.llm.effective_local_num_ctx() to read
    # the value that is ACTUALLY sent to Ollama — it applies the large-model clamp below.)
    repair_local_num_ctx: int = 16384
    # VRAM guard for LARGE diagnose models (≥ ~30B). On a 24 GB L4, a 32B (~20 GB weights) plus a big KV
    # cache overflows the GPU → Ollama offloads layers to CPU → generation runs ~10× slower → the diagnose
    # times out (the exact failure seen with qwen2.5-coder:32b). So for a model whose name advertises ≥30B,
    # the effective num_ctx is capped to THIS value (default 12288 → ~3 GB KV + 20 GB weights ≈ 23 GB, on-
    # GPU). 14B/7B are never clamped. This pairs with OLLAMA_NUM_PARALLEL=0 (auto) in docker-compose so the
    # 32B loads with a single KV slot (see the compose comment). 0 disables the clamp.
    repair_local_num_ctx_cap_large: int = 12288
    # Per-call timeout for the local model. CPU inference on a small Llama (e.g. llama3.2:3b) is SLOW —
    # prompt prefill over a large retrieved context + a cold model load can take minutes — so this is
    # generous by default. It is BOTH the Ollama client timeout AND (via propose_patch) the local
    # provider's outer DIAGNOSE deadline, so the local model is never abandoned mid-answer. Lower it
    # only on a GPU box where inference is fast. (The remote Claude call keeps repair_diagnose_timeout_s.)
    repair_local_timeout_s: int = 600
    # DEBUG DUMP: when true, every DIAGNOSE call writes a plain-text file to repair_debug_dir capturing the
    # EXACT prompt sent to the model AND the model's raw response (+ parsed patch, reject reason, timing and
    # a GPU VRAM report) — the ground truth for "what did we send the model and what did it say" when a fix
    # looks wrong or the model stalls. Defaults ON so every repair (including the default Claude + Chroma
    # path) leaves a debug report at repair_debug_dir; set REPAIR_DEBUG_DUMP=false to silence the I/O.
    repair_debug_dump: bool = True
    repair_debug_dir: str = "./data/repair_debug"

    # ── Auto-Repair v2 — failure-anchored retrieval + precision context + verification ─────────────
    # A generic redesign so retrieval targets WHERE the test actually failed, not the test's title.
    # Root cause it fixes (from a live TC-RPS-003 dump): the failure text leads with the test's DESIGN
    # INTENT ("mock card PAYMENT succeeds"), so retrieval pulled 16 chunks of PAYMENT code while the real
    # bug was an ADD-TO-CART quantity=0 popup — the buggy code was in ZERO retrieved chunks, so no model
    # could fix it. These knobs make retrieval anchor on the FAILED step + OBSERVED symptom, tighten the
    # context for interaction bugs, and flag off-target patches. Defaults preserve the SPEC/value-bug path
    # (the working cross-kiosk demo) byte-for-byte; only interaction bugs get the tighter profile.
    repair_failure_anchor: bool = True           # P0a: add a PRIMARY retrieval lane from the failed step + OBSERVED symptom
    repair_verify_relevance: bool = True          # P0b: detect a patch that targets code unrelated to the symptom; one nudged retry
    # INTERACTION-ELEMENT retrieval lane. Anchors a retrieval lane on the UI ELEMENTS the test interacted
    # with around the failure — element/test-ids (pay_with_mock_card_button, mock_card_number_input) and
    # button labels of the tap/type steps. These map DIRECTLY to the code that renders/handles the control,
    # so they localise an INTERACTION bug (a disabled/renamed/removed control, a broken handler) that
    # symptom prose ("no transition occurred") alone does not surface. Additive + generic (any app's element
    # ids); a strict no-op when the steps carry no element identifiers. Set False to drop the lane.
    repair_interaction_anchor: bool = True
    # P1 precision — the INTERACTION profile (a wrong-screen / popup / unresponsive-control bug: code matters,
    # design-doc prose is mostly noise). SPEC/value failures (balance, transaction, cross-kiosk) keep the
    # existing generous profile (5 design docs, 2 general, 16-block cap) so that demo does not regress.
    repair_max_context_blocks_interaction: int = 8    # hard cap on chunks for an interaction bug (was a flat 16)
    repair_design_docs_interaction: int = 1           # design-doc chunks for an interaction bug (was 5)
    repair_general_docs_interaction: int = 1          # general (unfiltered) chunks for an interaction bug (was 2)
    # ── TWO-AGENT design: a distinct RCA agent + a code-fixing agent (2026-09-21) ─────────────────
    # The Auto-Repair pipeline is now TWO separate agents, for ALL models (Claude and local alike):
    #   1. RCA agent (rca_node) — reads ONLY the design/requirements docs + the test-case workbook (never
    #      source code). It decides: is the failure caused by a bug in the SPEC (design/requirements) or an
    #      INVALID test case? If so it FLAGS that and STOPS — nothing is passed to the fixer (you don't
    #      patch code to satisfy a wrong test). Otherwise it declares a CODE bug, localises the suspect area
    #      + search terms, and hands them to the fixer.
    #   2. Code-fixing agent (retrieve → diagnose) — retrieves the offending CODE from an efficient
    #      code-only vector RAG (Chroma), seeded by the RCA's localisation, plus the design doc as
    #      authoritative reference, then proposes the minimal patch.
    # repair_rca_phase now defaults TRUE (the RCA agent runs). NO-REGRESSION: the gate is CONSERVATIVE —
    # it only stops on a HIGH-confidence spec/test verdict; a code bug (the demo bugs) always passes through
    # to the fixer, whose retrieval is a SUPERSET of the pre-RCA lanes, so Claude's fix is never degraded.
    repair_rca_phase: bool = True
    # Whether the RCA verdict may STOP the pipeline before the fixer. True = a high-confidence spec/test-bug
    # verdict halts and reports (no patch). False = RCA is advisory-only (always localises + proceeds to the
    # fixer, never blocks) — the safe fallback if a deployment sees RCA over-flagging valid tests.
    repair_rca_gate: bool = True
    # Enrich the failure description handed to BOTH agents with a per-step EXECUTION TRACE (action +
    # method/screen/expected/actual + result + observation + any error/stack) and a bounded tail of the
    # run's console/application log. Gives a text model like qwen2.5-coder far more to diagnose from than
    # the failed assertion alone. Bounded so it never dominates the local model's context window.
    repair_failure_detail: bool = True
    # Attach the FAILED steps' screenshots to the model prompt when the model is MULTI-MODAL (Claude).
    # qwen2.5-coder and other local text models are text-only, so this is auto-skipped for them (a text
    # model can't see an image). Lets Claude diagnose complex visual issues (wrong element, layout, a popup
    # that didn't dismiss) from the actual screen, not just the text symptom. No effect on the local path.
    repair_use_screenshots: bool = True
    # CODE-only retrieval RAG. The code-fixing agent always retrieves code from an efficient VECTOR index
    # (Chroma) — msgraphrag's entity/community graph is reserved for DOCS + TEST CASES (the RCA agent).
    #   ""        (default) → auto: use Chroma for code whenever the doc/knowledge backend is "msgraphrag"
    #                         (so msgraphrag holds only docs/tests); otherwise use the main backend, so the
    #                         pure-Chroma default and the graphrag/Neo4j option are byte-for-byte unchanged.
    #   "chroma"/"graphrag" → force a specific code backend.
    repair_code_backend: str = ""
    # When the doc/knowledge backend is msgraphrag, index code into THIS separate Chroma code-only index
    # (kept apart from the main repair_persist_dir so the two never collide). Gitignored generated data.
    repair_code_persist_dir: str = "./docs/chroma_code_only_db"
    # msgraphrag indexes ONLY documents + test cases (NOT source code): the expensive entity/community
    # pipeline is where a graph adds value (spec-level reasoning), while pinpoint code retrieval is faster
    # and more precise from a plain vector index. Set False to revert to msgraphrag-indexes-everything.
    repair_msgraphrag_docs_only: bool = True

    # ── Auto-Repair RETRIEVAL backend (Chroma default; GraphRAG + Neo4j local option) ──────────
    # Picks HOW the offending code/spec is retrieved for a repair. Default reproduces the current
    # behaviour exactly; the alternative keeps ALL retrieval inside the customer's environment.
    #   "chroma"   (default) — HuggingFace sentence-transformer embeddings + Chroma (unchanged POC).
    #   "graphrag"           — a graph-RAG over Neo4j: the same LOCAL embeddings, stored as a vector
    #                          index PLUS a code graph (Chunk-[:PART_OF]->File) in Neo4j, with
    #                          file-neighbourhood expansion done as a graph query. Nothing leaves the
    #                          box. `langchain_neo4j` / `neo4j` are imported LAZILY (in graphrag_store),
    #                          so selecting "chroma" carries no new dependency and nothing changes.
    #   "msgraphrag"         — the REAL Microsoft GraphRAG pipeline: a LOCAL LLM reads the code + docs
    #                          and extracts entities/relationships, clusters them into communities and
    #                          writes community summaries, producing a knowledge graph (parquet +
    #                          LanceDB). Retrieval draws on the graph's text units + community reports.
    #                          `graphrag`/`pandas` are imported LAZILY (in ms_graphrag_store), so
    #                          selecting anything else carries no new dependency. See the Microsoft
    #                          GraphRAG block below.
    # The LOCAL auto-repair stack (for a customer who wants repair done entirely in-house, no code or
    # documents sent out) = a local retrieval backend ("graphrag" or "msgraphrag") + repair_llm_backend
    # ="local" (Ollama). "graphrag" + a lightweight Llama (REPAIR_LOCAL_MODEL=llama3.2:3b) is the
    # runnable CPU default; "msgraphrag" + a code model (qwen2.5-coder) is the higher-quality GPU option.
    # All are the SECONDARY/backup path; Chroma + Claude stays the primary, default combination.
    repair_retrieval_backend: str = "chroma"    # chroma | graphrag | msgraphrag
    # Neo4j connection (only used when repair_retrieval_backend == "graphrag"). Defaults suit a local
    # single-node Neo4j (e.g. `docker run -p7687:7687 -p7474:7474 neo4j`). Change the password.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4jpassword"
    neo4j_database: str = "neo4j"

    # ── Microsoft GraphRAG (real entity/community LLM-built graph) — repair_retrieval_backend="msgraphrag" ──
    # The genuine Microsoft GraphRAG indexing pipeline: an LLM reads the code + docs and EXTRACTS
    # entities/relationships, clusters them into communities (Leiden) and writes community summaries,
    # producing a knowledge graph that retrieval draws on. It is the heavier, higher-quality cousin of
    # the Neo4j graph-RAG option.
    #   CONSISTENCY (why this is ONE model): the graph is BUILT with the SAME local model that DIAGNOSES
    #   the fix — set repair_llm_backend="local" + repair_local_model=<the code model>, and leave
    #   graphrag_llm_model empty so GraphRAG reuses repair_local_model. GraphRAG reaches it through
    #   Ollama's OpenAI-compatible endpoint (graphrag_api_base, default = repair_local_base_url + "/v1").
    #   The indexing LLM is reasoning-heavy, so this stack is meant for a code model (qwen2.5-coder) and,
    #   for the larger sizes, a GPU. `graphrag` + `pandas` are imported LAZILY (in ms_graphrag_store) and
    #   only when this backend is selected, so the default carries no new dependency.
    graphrag_root_dir: str = "graphrag_workspace"     # GraphRAG workspace (input/output/cache); on the VM /app/data/graphrag
    graphrag_llm_model: str = ""                        # empty → use repair_local_model (ONE model for graph + fix)
    graphrag_embedding_model: str = "nomic-embed-text"  # Ollama embedding model GraphRAG uses when building the graph
    graphrag_api_base: str = ""                        # empty → repair_local_base_url + "/v1" (Ollama OpenAI-compatible)
    graphrag_api_key: str = "ollama"                   # placeholder; Ollama ignores it but the OpenAI client needs a non-empty key
    graphrag_search: str = "local"                     # local | global — retrieval strategy over the built graph
    graphrag_chunk_size: int = 1200                    # GraphRAG text-unit size (tokens) when it re-chunks the inputs
    graphrag_community_level: int = 2                  # community-hierarchy depth surfaced as extra context
    # After the pipeline builds the graph (parquet), also LOAD the entities/relationships/community
    # summaries into Neo4j so the graph is BROWSABLE in the Neo4j Browser (http://<host>:7474,
    # bolt://…:7687). Uses distinct labels (Entity/Community/RELATED) so it never clashes with the
    # graphrag(Neo4j) RAG store (RepairChunk/File). Best-effort: a down/unreachable Neo4j logs a warning
    # and does NOT fail the (expensive) build. Reuses the NEO4J_* connection settings above.
    graphrag_export_neo4j: bool = True
    # INCREMENTAL builds: when a graph already exists, run GraphRAG's `update` (re-extracts only NEW/
    # CHANGED docs and merges) instead of a full `index` rebuild. Chunk files are named by a CONTENT hash
    # (source + text, line-independent), so an unchanged function/doc keeps the same identity and is
    # skipped; only edited/added code + docs are re-processed (community detection still re-runs globally).
    # Set False to always do a full rebuild. The FIRST build is always full (nothing to diff against);
    # delete <graphrag_root_dir>/output to force a full rebuild.
    graphrag_incremental: bool = True
    # DOMAIN entity types for extraction — this is what makes the graph speak POS instead of the generic
    # default (organization/person/geo/event). GraphRAG's extraction prompt is parameterised by these, so
    # the model tags SmartCard/Transaction/KioskStation/Function/... nodes. Comma-separated (env-friendly).
    graphrag_entity_types: str = "Function,SmartCard,Transaction,Balance,KioskStation,Endpoint,Screen"
    # Prepend a SHORT POS-domain preamble to GraphRAG's own extraction prompt (best-effort: we augment the
    # installed default so the strict tuple format stays valid; if the default can't be located, we fall
    # back to entity_types only — still POS-typed). Set False to use the stock prompt unchanged.
    graphrag_domain_prompt: bool = True
    # WHOLE-FUNCTION retrieval: GraphRAG re-chunks code into ~1200-token text units, which can SPLIT a
    # function so the buggy line (e.g. a guard) and its symptom land in different units. When on, a search
    # hit is expanded to the ORIGINAL whole tree-sitter chunk (the full function/section) via the sidecar,
    # so the model sees the cause and the symptom together — matching the Chroma/Neo4j backends' behaviour.
    graphrag_whole_function: bool = True
    # Outer wall-clock cap on the REMOTE (Claude) DIAGNOSE call so a hung request can never freeze the
    # repair — on timeout the chain moves to the next provider, then the demo fallback. Claude is fast,
    # so 90s is ample. The LOCAL provider does NOT use this — it gets its own, much larger budget
    # derived from repair_local_timeout_s (see propose_patch), because a CPU Llama needs minutes and the
    # outer deadline MUST be ≥ its own client timeout or it would be killed before it can answer.
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
