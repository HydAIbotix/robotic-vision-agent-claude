# CLAUDE.md — robotic-vision-agent-claude

Guidance for Claude Code (and humans) working in this repo. This is the **backend** of a
two-repo system; the operator UI lives in the sibling repo `../kiosk-test-studio`.

> Note: `README.md` is **stale** — it documents only the original `vision_agent/` sub-agent and
> lists paths that no longer exist (e.g. a single `robot/stubs.py`, a `storage/` layout that has
> since changed). Trust this file and the code over the README.

---

## What this is

An autonomous QA system for **physical kiosk touchscreens**, driven by Claude vision. A robotic
arm (or a Playwright browser, or pre-captured screenshots, as stand-ins) taps and types on kiosk
screens. Claude "sees" each screen, identifies UI elements with pixel coordinates, plans test
steps, executes them, validates outcomes, and files defects. A FastAPI backend exposes all of this
to the Kiosk Test Studio frontend.

Three operational phases plus supporting agents:

1. **App Explorer** (`run_explorer.py` / `app_explorer/`) — autonomously crawls a kiosk app once,
   producing `app_map.json`: a knowledge graph of screens, elements, pixel coordinates, and
   transitions.
2. **Test Runner** (`run_tests.py` / `test_runner/`) — reads test cases from Excel/DB, plans and
   executes them against the app map.
3. **Orchestration & defects** — `supervisor/` runs many robots in parallel; `defect_agent/`
   auto-generates defect reports on failures; `api/` serves everything to the frontend.

---

## Tech stack

- **Python ≥ 3.11**, build backend `hatchling` (only `vision_agent` is packaged as a wheel).
- **Agent framework: LangGraph** (`langgraph>=0.2`) — five independent compiled `StateGraph`s.
- **LLM: Anthropic Claude, `claude-opus-4-8` uniformly.** No OpenAI. AWS path uses Bedrock
  (`ChatBedrockConverse`, `bedrock_model_id=anthropic.claude-opus-4-8`). Chosen by `VISION_BACKEND`
  (`anthropic` | `bedrock`).
- **Image processing:** `pillow`, `numpy`, `opencv-python-headless` (Canny edge-detection fallback
  for element discovery in `vision_agent/vision/detector.py`).
- **Web:** FastAPI + uvicorn on port **8001** (CORS `*`, WebSocket for live updates).
- **DB:** SQLite via SQLAlchemy 2.x ORM — `sqlite:///./management.db` (swappable to postgres).
- **Excel:** `openpyxl` (test-case import + sample generation).
- **Robot REST client:** `requests`.

> ⚠️ **Dependency gap:** `fastapi`, `uvicorn`, `sqlalchemy`, and `requests` are imported but **not
> declared** in `pyproject.toml`. `pip install -e .` alone will not bring up the API server. See
> "What needs to be built next".

---

## Layout

```
api/                     FastAPI management backend
  main.py                ALL REST + WebSocket routes (~1500 lines); runs suites in bg threads
  database.py            SQLAlchemy engine/session, init_db() + idempotent column migrations
  models.py              ORM tables → management.db
app_explorer/            Phase 1 — autonomous crawler (LangGraph)
  agent.py state.py prompts.py
  nodes/                 explore_screen(_aria) · execute_action · identify_result · validate_map · finalize_map
app_map/
  store.py               AppMap load/save, MULTI-APP merge/remove, element_inventory_for_prompt, version_hash
test_runner/             Phase 2 — test execution (LangGraph)
  agent.py state.py prompts.py plan_cache.py plan_normalize.py broadcaster.py
  nodes/                 load_test_case · parse_steps (3-tier planner) · run_vision_step · run_backend_step · conclusive_verdict · finalize_tests
  reader/excel_reader.py
defect_agent/            Auto defect intelligence (LangGraph): evaluate → defect_intelligence → publish
repair_agent/            Self-healing arm of defect intelligence — LangGraph StateGraph:
  agent.py state.py broadcaster.py   compiled graph + state + progress sink (callables out of state)
  nodes/                 retrieve · diagnose · apply(+guard) · unit_test · build · prepare_pr
  parse_code_and_store.py  RAG index — Chroma + HuggingFace over the LIVE app + docs (POC code, kept as-is)
  repair_failed_test.py    the tool/helper library the nodes call (RAG search, Claude patch, apply,
                           tsc/vite, PR prep/open/delete); `run_repair` is a thin graph-driving wrapper
supervisor/              Parallel multi-robot orchestrator — ThreadPoolExecutor (NOT LangGraph)
vision_agent/            Core Tier-3 vision sub-agent (original README subject)
  agent.py state.py config.py llm.py prompts.py screen_cache.py
  nodes/                 analyze · plan · execute · validate(_pipeline) · retry · finalize
  robot/                 hardware abstraction (see below)
  storage/               local.py (dev) · aws.py (S3 + SQS)
  vision/detector.py     OpenCV element-detection fallback
test_plans/              Cached Claude plans: <test_id>_<hash>.json
results/  screenshots/   Run output JSON + captured/annotated images
```

Root scripts: `run_explorer.py` (Phase 1), `run_tests.py` (Phase 2 single suite, `--tc TC-ID`),
`run_parallel.py` (Phase 2 multi-robot, `--config file.json`), `run_demo.py` (vision_agent alone on
local PNGs), `generate_test_cases.py` (writes 50 sample TCs to Excel), `inspect_screen.py`
(dev: run analyze_screen only, print + annotate detected elements).

---

## Key architectural decisions

- **Everything is config-driven; no hardcoding of environments.** `vision_agent/config.py` is the
  single pydantic-settings source, loaded from `.env`. Three backend toggles switch the whole
  system with **zero code changes**: `VISION_BACKEND` (anthropic|bedrock), `STORAGE_BACKEND`
  (local|s3), `ROBOT_BACKEND` (demo|playwright|real). `api/main.py` persists config edits back to
  `.env` via `_persist_env`.

- **Swappable robot abstraction.** All agent code calls `from vision_agent import robot;
  robot.tap(x, y)` — never a concrete backend. `robot/__init__.py` resolves the active backend at
  **call time** via PEP 562 `__getattr__` (from `settings.robot_backend`), so switching backends
  from the UI takes effect on the next call with no restart. Resolution order: primary backend
  module → `stubs.py` (shared fallback) → harmless no-op (for optional lifecycle hooks).
  - `stubs.py` — `demo`: pops pre-captured PNGs; also the shared-helper fallback module.
  - `playwright_stubs.py` — `playwright`: drives real Chromium, real DOM/ARIA, HUD overlay.
  - `real_robot.py` — `real`: physical arm + AGV over REST (non-blocking `POST → 202 {cmd_id}`,
    poll state until `idle`, timeout → abort). Viewport→camera coord scaling/calibration.
  - Documented progression: **demo (PNGs) → playwright (browser) → real (hardware)**, identical I/O.

- **Three-tier planning = cost control** (`test_runner/nodes/parse_steps.py` + `run_vision_step.py`):
  - **Tier 1** — cache hit from `test_plans/<id>_<hash>.json`, 0 LLM calls, if all element coords still valid.
  - **Tier 2** — one **text-only** Claude call, planning from `element_inventory_for_prompt` (no
    image); result cached. The app_map rendered as text *is* the "visual description".
  - **Tier 3** — legacy VisionAgent (text plan + per-step image LLM calls); fallback when there's no
    app_map or a runtime failure.
  - Steady-state execution is **0 LLM calls**; Opus is paid only on exploration, planning, and Tier-3.

- **`app_map.json` is a multi-app knowledge graph.** Screens are tagged with `app_id`; re-exploring
  one app merges/replaces only its screens (`merge_explored_app`/`remove_app`) without wiping
  others. Contains screens (elements: id/type/label/bbox/center/confidence, transitions,
  dependencies, reference screenshot) plus a `keyboard_map` used by `type_text`. It is **gitignored**
  (removed from tracking) — it's generated, per-environment data.

- **Five LangGraph agents, one thread orchestrator.** Nodes are pure `state -> dict` partial
  updates; routing via `add_conditional_edges` + small `_route_*` predicates. TestRunner nests
  VisionAgent as its Tier-3 fallback. The Auto-Repair agent (`repair_agent/agent.py`) is the fifth
  StateGraph: `retrieve → diagnose → guard → apply → unit_test → build → prepare_pr` with conditional
  edges for dry-run (stop after diagnose) and a dirty-repo guard (stop before apply). `api/main.py`
  runs TestRunner + DefectAgent + Auto-Repair in daemon threads and streams `test_started` /
  `step_result` / defect / `repair_started` / `repair_done` events over WebSocket. Each agent keeps
  callables out of graph state via a broadcaster mapping an id → callback
  (`test_runner/broadcaster.py` by run_id, `repair_agent/broadcaster.py` by repair_id).

---

## Data model (`api/models.py` → `management.db`)

`KioskConfig` · `AppMapRecord` · `TestCase` · `TestRun` (1→many `TestResult`) · `Defect` ·
`DeviceConfig` (physical device the robot visits: alias e.g. "TVM", linked kiosk_id, position x/y/θ) ·
`RobotEvent` (command telemetry).

## API surface (`api/main.py`, all under `/api`)

Health `GET /health` · Runs `GET/POST /runs` (`POST` executes `filter_tc` ids in the **given order**),
`GET /runs/{id}`, `WS /runs/{id}/ws`,
`GET /runs/{id}/defects`, `GET /runs/{id}/screenshots[/{file}]`, `PATCH /runs/{id}/verdict`
(human FAIL/PASS override) · Test cases `GET /test-cases`, `POST /test-cases/upload` (Excel) ·
Config `GET/PUT /config`, `PATCH /config/robot`, `PATCH /config/camera`, `PATCH /config/card-service`,
`GET/PUT/DELETE /config/device[/{alias}]`, `PUT /config/kiosk` · Robot `GET /robots`,
`GET /robot/health`, `POST /robot/test-call` (whitelisted proxy) · Camera Vision Test
`POST /vision-test/capture` (arm `/capture` type=raw|screen), `POST /vision-test/upload`,
`GET /vision-test/image/{file}`, `POST /vision-test/analyze` (aHash+OpenCV+OCR+optional Claude) · TC planning
`POST /tc-plan`, `DELETE /tc-plan/{id}` · Exploration `POST /explore` (spawns `run_explorer.py`),
`GET /explore/{id}`, `GET/PATCH /explore-config` · App map `GET/DELETE /app-map`,
`DELETE /app-map/{app_id}` · `POST /reset` (clears runs/results/plans/cases; preserves
exploration + config) · Auto-Repair `GET /repair` (dashboard list, newest first), `POST /repair`
(start job — auto-repair fires automatically on a failed run; manual start still available but the UI
no longer calls it), `GET /repair/{id}` (poll stages), `POST /repair/{id}/open-pr` (GATED push+PR,
confirm=true), `POST /repair/{id}/delete-pr` (delete the pushed fix branch → closes the PR, for
repeatable demos), `POST|GET /repair/index` (build/status of the Chroma RAG index).

---

## Running it

```bash
pip install -e .            # NOTE: also needs fastapi uvicorn sqlalchemy requests (see gap above)
cp .env.example .env        # set ANTHROPIC_API_KEY

python -m uvicorn api.main:app --host 0.0.0.0 --port 8001   # backend (usually launched by the studio)
python run_explorer.py      # Phase 1: build app_map.json
python run_tests.py --tc TC-VPS-001   # Phase 2: run a suite
python run_demo.py          # vision_agent alone on local PNGs
pytest tests/ -v
```

The frontend's `scripts/start-api.cjs` launches this backend automatically (uvicorn **without**
`--reload`, intentional) — see the sibling repo.

---

## What needs to be built next

1. **Implement `run_backend_step.py`** — API/DB validation channels are still a stub; the frontend
   already emits `channel: db|validation` steps.
2. **Real JIRA integration** — `defect_agent` writes placeholder `jira_key`/`jira_url`; wire to a
   real JIRA API.
3. **Hardware bring-up** — the `real` backend is untested against physical arm/AGV; validate the
   command/poll/calibration loop once hardware is available.
4. **Refresh `README.md`** to match the current multi-agent architecture.
5. **De-Windows the scripts** — `run_demo.py`, `run_tests.py`, `run_parallel.py`, `inspect_screen.py`
   contain hardcoded `C:/Users/gsk54/...` paths; make them relative/config-driven.

---

## Invariants, gotchas & open items

> Distilled from the full debugging log (2026-06-25 → 2026-08-18), now archived in
> [`docs/DEBUGGING_HISTORY.md`](docs/DEBUGGING_HISTORY.md) — read that for the full story behind any
> specific fix. Below are the still-true rules; read them before touching coordinate math, the robot
> backends, the explorer, or the Tier-3 path. Legend: ✅ solid · ⚠️ works but fragile / unverified
> live · 🔲 open.

### The #1 operational gotcha

- **The API server runs uvicorn WITHOUT `--reload` (intentional). RESTART the backend after ANY
  backend edit** — otherwise the running process serves stale code. A run showing an old timeout/
  behaviour is the tell it wasn't reloaded (the frontend's `start-api.cjs` launches it).

### Coordinate & tap pipeline (the fragile foundation — most bugs traced here)

- **Element coordinates ALWAYS come from the `app_map` (learned in the Playwright exploration
  viewport), scaled to the camera — in EVERY tier. Camera image quality NEVER affects tapping
  accuracy; it only affects screen/text VALIDATION (`verify` steps).**
- ✅ **Tap/type focus by STABLE testid first (playwright), coords are the fallback**
  (`run_vision_step._element_testid` → `robot.focus_by_testid` / `robot.tap_by_testid`). App_map
  coords can be **stale or state-dependent** — the VPS top-up panel's fields/buttons shift ~112px
  when a reader box opens, so a re-exploration can chart them in the wrong state. A coord tap snaps
  only within 80px → a larger drift falls through to a RAW click on empty space (button silently
  MISSED) or focuses the WRONG field (card number typed into the amount box) — yet the step still
  reports success. When the app_map element has a `testid` we focus/click by DOM identity (immune to
  drift); `tap_by_testid` skips on 0 or >1 matches so ambiguity falls back to the spatial snap. Real
  arm / demo return False → coordinate path unchanged (no regression).
- **Exploration viewport = the physical kiosk MONITOR's native resolution (1920×1080 for these
  kiosks), NOT the camera resolution.** The kiosks render an arm-reachable CENTERED fixed-px box
  (`clamp(620px,40vw,780px)`) whose fractional element positions are viewport-dependent, so the
  viewport must match the monitor the arm photographs. The camera resolution CLIPS the app (Sign In
  falls below the fold at ≤~600px tall) — do NOT use Robot Setup's "Match viewport to measured camera"
  for this layout. After changing the viewport: **re-explore both kiosks + regenerate plans** (cached
  px/py are stale). `.env` `VIEWPORT_WIDTH/HEIGHT`; `KIOSK_SCREEN_LAYOUT=arm-reachable` is forced on
  the explore URL.
- **`real_robot._scale`** maps app_map(viewport)→camera per-axis, then applies a per-axis affine
  calibration (`CAMERA_CALIB_*`). Horizontal is identity (box is centred); vertical is the drifting
  axis because `/capture type=screen` is a VERTICAL CROP whose extent varies per arm/camera pose.
- ✅ **Vertical tap mapping self-calibrates per pose** from the login form's own anchors
  (email/password/Sign In/footer → a monotonic piecewise map, `vision/screen_calibrate.py`), derived
  on the leading login `verify` capture and reused for the whole test (the base doesn't move mid-test).
  Gated by `AUTO_TAP_CALIBRATION` (default on), bounded, falls back to static `CAMERA_CALIB_AY/BY`
  (identity). `python calibrate_tap.py` sets a static AY/BY manually. **Re-derive after any
  arm/camera/screen pose change.**
- Virtual-keyboard keys map through the SAME `_scale` calibration (`_scale_key`); the footer/keyboard
  band is bracketed by the login vmap's sign-in + footer knots, so keyboard accuracy rides on the
  footer anchor. `type_text` batches all char taps + the on-screen `Done` key into one `/screen/click`.
- 🔲 The residual tap offset ultimately traces to the robot's `/capture type=screen` rectification not
  being a faithful full-screen deskew (robotics-side item), not our tap math.

### Tier architecture & the cost story

- **Tier 1** = plan-cache replay + template-match screen ID, **0 LLM calls**. **Tier 2** = ONE
  text-only Claude plan call from the `app_map`-as-text, cached back to Tier 1. **Tier 3** = full
  vision fallback (per-step image LLM call) only for uncharted screens / step failures. Steady-state
  execution is 0 LLM calls; Opus is paid only on exploration, planning, and Tier-3.
- ✅ **Screen identity on real-camera frames = TM_CCOEFF_NORMED template matching**
  (`vision/template_match.py`), which REPLACED aHash (aHash could not bridge the camera↔browser domain
  gap). References in `reference_screens/`; a per-axis center-crop (`TEMPLATE_MATCH_CENTER_CROP_*`,
  ~0.6×0.92) focuses correlation on the centred app box. A template MISMATCH is downgraded to
  inconclusive → Claude vision stays the wrong-screen authority. aHash/OCR/OpenCV remain only weak
  fallbacks on camera frames.
- Two runtime Claude uses: a screen-identity verify fallback (~$0.02–0.03) and a full Tier-3 step
  (~$0.04–0.06). Tier-3 reads element coords OFF the camera image — so it uses `tap_image_point`
  (camera-space, no `_scale`), never `robot.tap` (which would double-scale).
- **Camera-domain references** (a per-screen camera-capture pass after exploration, via Camera Vision
  Test → "Save reference" or `python capture_reference.py --screen login`) are what make Tier-1
  template match 0-LLM on the real robot; browser refs score below the floor → Claude fallback.
- Claude vision media-type is sniffed from magic bytes (`llm.detect_image_media_type`) — the real arm
  `/capture` returns JPEG, browser screenshots PNG.

### Hardware ready-state conventions (real backend)

- **The AGV base signals arrival with `state:"ready"` (NOT the arm's `idle`) and does not echo our
  `cmd_id`.** `_BASE_READY_STATES` = {ready, idle, arrived, done, reached}; base polling / health /
  `check_state` all treat these as arrived/healthy; state is authoritative (no cmd_id gate).
- ✅ **Base move: NO client-side timeout, NO self-abort** — poll `/base/state` every 10s
  (`BASE_POLL_INTERVAL_S`) until a ready state or `error`; a down controller fails via the HTTP error.
  (A prior 60s deadline + `/base/abort` was stopping the base ~1m short of home.) Live distance-
  remaining status streams via a push sink registered BEFORE the per-test positioning move.
- **The arm poll is state-authoritative** ("not moving" = done; `error` = fail-fast → recover via
  abort→home). The arm also settles to `ready` (`_ARM_TERMINAL_STATES`, `_ARM_HEALTHY_STATES`). Click
  success is read from `click_result.completed/total`; `DESCEND_LIN_FAILED`/`HOVER_FAILED` = a physical
  reach/planning limit → the step fails and on the real backend a robot fault STOPS the run (no Tier-3
  wandering), gated by `ROBOT_ERROR_STOPS_RUN`.
- **`/base/goto` needs `target`** (the AGV map's named position), NOT `kiosk_id`. Per-device
  `DeviceConfig.position_name` decouples the AGV-map name from the `kiosk_id` join key; `AGV_HOME_TARGET`
  for "go home". The arm/card APIs (`/screen/click`, `/card/*`) legitimately carry `kiosk_id`.
- Per-test AGV positioning only drives the base when the test's steps contain an explicit move phrase
  (`_test_wants_agv_move`, `AGV_MOVE_REQUIRES_EXPLICIT_STEP`) — a single-kiosk test with no move step
  won't drive the AGV.
- Timeouts: `robot_response_timeout_s` (2s/HTTP call), `capture_timeout_s` (30s — `/capture` moves the
  arm first), `arm_move_timeout_s` (60s), per-key `arm_key_tap_timeout_s` (15s),
  `robot_unreachable_timeout_s` (60s fail-fast when the API is down).

### Explorer / execution rules

- **Exploration needs `ROBOT_BACKEND=playwright`** (it crawls a live DOM; the `real` backend has no
  browser and would hit the physical arm camera). Set it back to `real` + restart for a hardware run.
  Exploration is a fresh subprocess that reads `.env` each time; its logs go to `explorer_logs/`.
- **`kiosk_id` is the single join key** across KioskConfig ↔ DeviceConfig ↔ TestCase inference ↔
  app_map `app_id` / plan scoping / template refs — keep all in sync (`[[kiosk-id-join-key]]`).
- Plans are scoped to the test's kiosk set (`store.scoped_to_apps`); a cross-kiosk E2E sees the union.
  The browser follows the robot across kiosks via a ground-truth router (a `verify` switches to the
  kiosk that owns its `expected_screen`). A stale `expected_screen` is rescued by a strict Claude
  intent-judge (`verify_intent_satisfied`) rather than hard-failing.
- Runtime plan-normalization (`plan_normalize.py`) rewrites captured-value reuse into the canonical
  `[tap completion button] + [enter+complete vision]` order on cache load. Dynamic values captured at
  runtime (`{{captured.NAME}}`) are read via a Claude-vision `capture` step (authoritative for transient
  values the explorer can't chart).
- DOM coordinate-correction (`explore_screen._dom_correct_elements`) snaps vision estimates to real DOM
  centres by text, then by a single-distinctive-or-≥2 shared identity-token match against testid/aria
  (with plus→increase / minus→decrease direction synonyms for steppers). This is why re-exploration
  fixes the add-to-cart / stepper coordinates.
- ✅ **DOM backfill for vision-missed controls** (`explore_screen._backfill_unmatched_dom`, runs right
  after DOM-correction, playwright only): when TWO controls share the SAME visible label (e.g. the VPS
  card station renders **two** "Tap Real Card" buttons — `station-arm-real-card` issues a NEW card,
  `station-topup-tap` tops up an EXISTING one), Claude's vision emits a single element and the second is
  LOST from the app_map. Backfill re-reads the DOM and appends any interactive node with a stable
  `data-testid` that no vision element claimed (dedup-guarded by testid + 24px proximity, id derived from
  the testid e.g. `station-topup-tap`→`topup_tap`). Conservative — testid'd buttons/inputs/links only, so
  it never displaces a mapped element. This is what makes a re-exploration capture the top-up reader.
- `app_map.json`, `reference_screens/*.png`, `camera_captures/`, `coordinate_exports/`, and
  `screenshots/exploration_*` are gitignored generated per-environment data. Each exploration's raw
  shots go to `screenshots/exploration_<app_id>_<ts>/`; annotated shots stay in `screenshots/annotated/`.
- Per-run results are preserved under `results/<run_id>/` (`results.json` + `run.log` +
  `run_console.log`); each real tap saves a before(crosshair)/after screenshot pair.

### Open / unverified

- ⚠️ **The real-robot arm/camera/card paths are still not fully verified against physical hardware.**
  The AGV `/base/*` path has run live; arm sign-in (TC-RPS-001) reaches email/password taps + typing
  but is gated by robot-side MoveIt reach/planning limits (keep the base close; the app is shrunk to the
  arm-reachable box). Card ops are unwired into the `real` backend, but the VPS **card-station reader**
  flows (TC-VPS-009/010/011) DO run under `playwright` against a real USB HID reader — see the card-station
  progress note below.
- 🔲 `run_backend_step.py` (API/DB validation channels) is still a stub; real JIRA integration pending.
- ⚠️ Per-kiosk plan scoping + kiosk-URL lifecycle are unit-tested, but each new hardware run is the real
  end-to-end test. Requires the user re-steps: re-explore per kiosk, align Device Map alias→kiosk_id,
  regenerate plans, restart the backend.

### Auto-Repair, plans & cloud-agnostic invariants (distilled from the progress log)

> The change-by-change history behind these rules is archived in
> [`docs/PROGRESS_LOG.md`](docs/PROGRESS_LOG.md). The rules below are the *still-true* essence.

- **LLM temperature is DEPRECATED on Claude Opus 4.8** — sending it 400s. `llm_temperature`
  defaults to **None** and `vision_agent/llm._temp_kwargs()` OMITS the param unless a numeric value
  is set. Plan consistency does NOT come from temperature — it comes from the content-based
  `version_hash` (below) + the auth-first / navigation-completeness prompt rules.
- **`app_map.version_hash` is CONTENT-based** (screen ids + sorted element ids; NO `explored_at`
  timestamp, coords excluded) — a re-explore that charts the SAME structure reuses cached plans
  verbatim; it re-plans only when a screen/element is actually added or removed. Same test →
  same plan.
- **Plan gaps self-heal via Tier-3, generically — never inject app-specific steps.** A
  wrong-SCREEN `verify` failure with no text/value assertion (`verify_wrong_screen_recovers`)
  BRIDGES the missing-navigation gap with bounded vision, then RESUMES the 0-LLM structured plan and
  stops at the objective. Text/value mismatches on the RIGHT screen (e.g. TC-VPS-009 `'PURCHASE'`)
  stay TERMINAL. Tier-3 shares the plan's exact input values (`_plan_input_values`) so a recovered
  field uses e.g. the issued card `0005322931`, never an invented placeholder like `1234`.
- **⚠️ Wrong-screen verify = GAP-vs-DEFECT judge (Option C, Claude vision, 2026-09-21).** A screen-only
  verify miss is AMBIGUOUS: a recoverable missing-nav GAP (bridge + resume) vs a genuine app DEFECT (the
  app refused/errored — e.g. TC-RPS-003's add-to-cart popping a "Quantity Required" dialog). Before
  bridging, `run_vision_step` asks Claude ONE strict question (`classify_wrong_screen_failure` in
  `vision_agent/nodes/validate_pipeline.py`): a confident **defect** verdict fails the test FAST (marks
  `sr["verify_defect"]` → the outer handoff is terminal) so Auto-Repair targets the real bug; **gap** (or
  any uncertainty/error) bridges exactly as before → **no regression** to working recoveries. Gated by
  `verify_defect_judge` (default True); runs only on the `_screen_only` path (text/value assertions were
  already terminal). Vision is always Claude, so no multimodal gate is needed.
  - **TUNED so real defects fail fast instead of being masked (2026-09-21, both default ON).** A Tier-3
    bridge can silently RECOVER past a real bug by re-doing the failed interaction (observed: the quantity
    popup auto-dismissed to a normal `products` screen, so the judge saw nothing wrong and bridged). Two
    levers now push borderline cases to fail fast: **(1) judge confidence** — the judge also returns
    high/medium/low, and a wrong-screen miss BRIDGES only on a `gap` verdict at least `verify_bridge_min_
    confidence` (default `high`); a lower-confidence `gap` or any `defect` fails fast. **(2) unresponsive-
    interaction rule** (`verify_unresponsive_interaction_defect`, deterministic, no LLM, runs first): if the
    app is STILL on the screen where the plan's last interaction (a tap/type with a `screen_id`) ran, that
    interaction did not advance the flow → a blocked/refused control → defect. Both are configurable; lower
    the confidence bar toward `low` (or disable the rule) to bridge more readily (closer to always-bridge).
    Helpers `_conf_at_least` / `_last_interaction_screen`; `classify_wrong_screen_failure` now returns
    `(category, confidence, observation)` and stays default-safe (`("gap","high",…)` on error → bridges).
  - **DEFERRED — local/no-LLM equivalents (revisit when Auto-Repair must run air-gapped).** *Option A:*
    classify the ACTUAL screen deterministically — a `*_popup` / `*_required` / `*_error` / error-banner
    state ⇒ defect (terminal); a neutral other screen ⇒ gap (bridge). *Option B:* keep the bridge but
    BOUND it and self-terminate — if Tier-3 can't reach the expected screen within its bounded segment,
    fail terminally instead of limping forward. A+B together give a no-LLM defect gate for the local path;
    add them behind the same `verify_defect_judge`-style flag when that scenario arrives.
- **Single VM = in-process execution.** `TASK_QUEUE_BACKEND=inline` + `EVENT_BUS_BACKEND=memory`
  (byte-identical to the local MVP). The Redis API/worker split + pub-sub is ONLY for multi-replica
  K8s scale-out (`deploy/k8s/`); on ONE box a missing `SERVICE_ROLE=worker` leaves runs stuck at
  "pending".
- **Auto-Repair has THREE retrieval backends, config-only** (`repair_retrieval_backend`): `chroma`
  (default — HuggingFace `all-MiniLM-L6-v2` + Chroma), `graphrag` (Neo4j vector index + a
  `(:RepairChunk)-[:PART_OF]->(:File)` code graph), `msgraphrag` (the REAL Microsoft GraphRAG
  entity/relationship/community pipeline). LLM via `repair_llm_backend` (`claude` default | `local`
  Ollama); `repair_local_only=true` air-gaps the diagnose. Switch with `LOCAL_REPAIR=graphrag`
  or `LOCAL_REPAIR=msgraphrag` in `start-all.sh`, or compose recipes A/B/C — no code change,
  defaults reproduce Chroma + Claude byte-for-byte. `msgraphrag` uses ONE local model for BOTH
  graph-build and code-fix (`graphrag_llm_model` empty → falls back to `repair_local_model`);
  the graph exports to Neo4j (`graphrag_export_neo4j`) and builds are INCREMENTAL
  (`graphrag_incremental` → GraphRAG `update`).
- **⚠️ Rebuild the RAG index on the ON-DISK branch before running a repair.** The index
  reflects code at BUILD time; a rebuild done while a branch was transiently fixed leaves the buggy
  line unindexed → a wrong fix, or "Patch find-text was not found" (a branch/index mismatch),
  NOT a retrieval-logic bug. Rebuild via the Studio button / `POST /api/repair/index` (lock-proof,
  in-place — no restart needed).
- **⚠️ The running POS the browser hits is the COMPILED `dist` baked into its nginx image at
  `docker build` time — NOT the live source.** (POS `Dockerfile` = 2-stage: `npm run build` →
  `COPY --from=build /app/dist`.) `git status` shows the SOURCE branch, which can DIVERGE from the running
  image after a branch switch without `docker compose up -d --build`. To confirm what's actually running:
  rebuild the POS from the branch + hard-refresh, or behaviour-probe. (The planted quantity bug
  `if (quantity <= 1)` is an off-by-one: qty starts 0 → Add disabled; `+` once → 1 → popup; `+` twice → 2
  → adds — and a wrong-screen cart verify recovering via `method=tier3_bridge` is itself evidence the bug is
  present and Tier-3 masked it.)
- **Repair patch-quality ceiling is MODEL choice, not config.** A 3B general model can't reliably
  root-cause a reasoning-heavy bug or emit a precise unique patch; use a code-specialised model
  (`qwen2.5-coder:14b/32b`) on a GPU, or Claude. Demo the local path on the simpler planted bugs
  (login `-bug`, products `0`); reserve the hard cross-kiosk transaction bug for a strong model.
  BUT first rule out RETRIEVAL: a buggy chunk that is indexed yet never surfaced (vector similarity
  buries it under design-intent/navigation vocabulary — seen on TC-RPS-003's `quantity <= 1` guard)
  is a ranking miss, not a model failure. `retrieve_context` now runs a **lexical re-rank** over a
  wider candidate pool (`_POOL_MULT`×top_k) that PROMOTES ≤2 code chunks sharing the most distinctive
  tokens (identifiers/numbers/quoted values) with the failure — additive, backend-agnostic (Chroma +
  msgraphrag), a strict no-op when nothing clears `_SIGNAL_MIN`.
- **⚠️ Retrieval anchors on the FAILURE POINT, not the test title (Auto-Repair v2).** The failure text
  leads with the test's DESIGN INTENT; a test that dies EARLY (e.g. a *payment* test failing at
  *add-to-cart*) would otherwise retrieve the wrong feature's code — the model never sees the bug (live
  root cause of the TC-RPS-003 miss: 16 retrieved chunks were all payment, the add-to-cart bug in none).
  Fixes: (1) **failure-point lane** — a PRIMARY retrieval query from the `[FAILED HERE]` step + `OBSERVED`
  symptom + assertions (`repair_failure_anchor`); interleaved with the action + intent lanes so the
  cross-kiosk VALUE demo still gets its persistence/spec code. (2) **Bug-class routing** (`_bug_class`,
  scored on the failure POINT): a `spec` failure (balance/transaction/cross-kiosk) keeps the generous
  profile (5 design docs, 16-cap — NO regression); an `interaction` failure (wrong screen/popup) uses a
  tight code-heavy profile (`repair_*_interaction`, ~8-cap, 1 design) that drops doc boilerplate.
  (3) **Patch verification** (`repair_verify_relevance`): score the patch's overlap with the failure-POINT
  signals; 0 overlap while a ≥3-overlap chunk WAS in context ⇒ off-target ⇒ one nudged retry (keeps the
  better-of, never dead-ends); 0 overlap and nothing relevant retrieved ⇒ logged as a RETRIEVAL miss (not
  a model error). (4) **RCA localisation** (`repair_rca_phase`, opt-in) — one LLM call yields search terms
  for a targeted lane before the fix. Screenshot/vision analysis is Claude/multimodal ONLY (qwen-coder is
  text-only). Detail → `docs/PROGRESS_LOG.md`; team write-up → `docs/Auto_Repair_v2_Code_Review.html`.
- **⚠️ Auto-Repair is TWO SEPARATE AGENTS (v3, 2026-09-21), for ALL models.** Graph:
  `rca → retrieve → diagnose → apply → unit_test → build → prepare_pr`. (1) **RCA agent**
  (`repair_agent/nodes/rca.py`, `run_rca`) reads ONLY docs + test cases (never code) → `{verdict:
  code_bug|spec_bug|test_invalid, confidence, rationale, suspect, search_terms}`; a HIGH-confidence
  `spec_bug`/`test_invalid` STOPS the pipeline (`rca_stop` → job status `rca_stopped`) — never patch code to
  satisfy a wrong spec/test. (2) **Code-fixing agent** (`retrieve → diagnose`) retrieves CODE seeded by the
  RCA's `rca_query`. `REPAIR_RCA_PHASE` defaults **True**; NO-REGRESSION via a CONSERVATIVE gate
  (`_rca_should_stop`: stop only on HIGH-confidence spec/test) + a code retrieval that is a SUPERSET of the
  pre-RCA lanes, so a code bug (every demo bug) proceeds to the unchanged fixer and Claude's result is
  preserved. `REPAIR_RCA_GATE=false` → advisory-only. **Retrieval is SPLIT** (`search_code`/`search_docs`):
  with `msgraphrag` + `repair_msgraphrag_docs_only` (default) the graph holds only docs/tests and a separate
  Chroma **code-only** index (`repair_code_persist_dir`, built by `build_code_index`, refreshed by the same
  "Rebuild index") serves the fixer; pure-Chroma/graphrag are unchanged. **Lexical matching is sub-token
  exact** (`_subtokens`): `action` no longer matches inside `transaction` (the guard-defeating false positive)
  while `cart` still matches `onAddToCart` — generic. **Screenshots** of the failed steps go to the Claude
  prompts (`repair_use_screenshots`, multimodal only; qwen stays text); failure text carries a per-step
  EXECUTION TRACE + console-log tail (`repair_failure_detail`). Detail → `docs/PROGRESS_LOG.md` (2026-09-21).
- **⚠️ RCA + fixer are GENERIC across bug types & apps (2026-09-21 later).** The prompts no longer assume a
  POS/kiosk domain ("the application under test") and the RCA taxonomy spans FIVE categories, so different
  failures route correctly: `code_bug` (→ fixer, the only one that patches code), `spec_bug` (requirements
  bug — STOP), `test_invalid` (STOP), `environment` (page-not-loading / network / API / timeout / config /
  deploy — STOP; a code patch can't fix infra), `unknown` (→ fixer to verify). `_RCA_STOP_VERDICTS`
  {spec_bug, test_invalid, environment} halt only at HIGH confidence (conservative — code_bug/unknown never
  regress). The diagnose prompt now asks for `confidence` + `root_cause` and is told to prefer an
  evidence-backed HIGH-confidence fix over fabricating a change when the retrieved context lacks the cause
  (surfaced on `RepairPatch`/`diagnose.inputs`, shown in the report). **Interaction-element retrieval lane**
  (`repair_interaction_anchor`, `_interaction_query`): anchors a lane on the element/test-ids + button
  labels the test tapped/typed around the failure (e.g. `mock_card_number_input`, `pay_with_mock_card_button`,
  `Use Mock Card`) — the strongest localiser for a disabled/renamed/removed control or broken handler that
  symptom prose misses. Additive + generic; empty → lane skipped (no regression). Live gap it closes: the
  disabled mock-card input was never retrieved, so Claude invented a plausible-but-wrong `alreadyResolved`
  fix. Detail → `docs/PROGRESS_LOG.md`.
- **Auto-Repair report has an EXECUTIVE SUMMARY (default) + technical view (2026-09-21 later).** The 📋
  Detailed-report window opens on a leadership-facing **Executive summary** — outcome hero, KPI tiles (root
  cause, files/lines changed, build), a colour-coded pipeline stepper, an "evidence examined" bar chart
  (docs/code/screenshots), root-cause + fix **confidence rings**, and the fix at a glance — every chart
  links back to its **Technical details** section. Toggle switches views. Dependency-free inline SVG/CSS
  (no chart lib). "Code-Fixing Agent" naming is under review (alternatives proposed).
  - **Root-cause tile reflects the VERIFIED OUTCOME, not a non-stopping advisory (2026-09-21).** An RCA
    verdict is only advisory when it did NOT stop the pipeline (medium/low confidence). If a fix was applied
    AND the build passed AND RCA did not stop (`patchVerified`), the ROOT CAUSE tile shows **Code bug** (+
    the diagnose `root_cause`), with a muted note recording RCA's initial read — so the headline never
    contradicts "Bug fixed & verified" (observed: RCA said `test_invalid` medium → proceeded → code fixed,
    but the tile wrongly showed "Invalid test case"). The technical RCA section still shows RCA's actual
    verdict. Report window sets an explicit `color: var(--text)` so all box text is legible (rebuild the
    STUDIO container to deploy — a backend-only `up -d --build app` doesn't).
  - **⚠️ A fail-fast defect's reason must reach the failure text.** `run_vision_step` PREPENDS the defect
    reason (unresponsive-interaction / judge) to the verify observation instead of dropping it — the bland
    "Wrong screen: expected X got Y" alone misled the RCA agent into `test_invalid`; the real symptom ("the
    interaction did not advance — an unresponsive/blocked control") steers it to `code_bug`.
- **Diagnose "what did we send / get" is dumpable — ON by default (2026-09-21).** `REPAIR_DEBUG_DUMP`
  writes the EXACT prompt + each provider's RAW response (+ parsed patch, RCA verdict, reject reason,
  timing, `ollama ps` VRAM) to `repair_debug_dir` (`./data/repair_debug`, host-mounted; container
  `/app/data/repair_debug`) per DIAGNOSE call — captures even the timed-out "(no output)" case, and fires
  on the DEFAULT Claude + Chroma path too (the dump loop covers every provider incl. `claude`). Now
  **defaults True** (`repair_debug_dump`, compose `REPAIR_DEBUG_DUMP:-true`) so a report always lands;
  set `REPAIR_DEBUG_DUMP=false` to silence the I/O.
- **Auto-Repair "Detailed report" window (customer-facing, 2026-09-21).** The Studio Auto-Repair card has
  a **📋 Detailed report** button opening a floating window (minimize/maximize/close) that walks every
  step: what the RCA agent read + concluded, the retrieved code chunks, and — the centrepiece — EXACTLY
  what was sent to Claude to diagnose (the failure description, the failed-step **screenshots**, and the
  retrieved code+doc context) and the patch it returned, then apply/test/build/PR. Backend feeds it via a
  new `diagnose` stage field `inputs` (`{failure, context, screenshots[basenames]}`, added in
  `repair_agent/nodes/diagnose.py`); screenshots load from the run via `run_id` (`runScreenshotUrl`). Pure
  add — reads only data already on the job, no new endpoints.
- **⚠️ APPLY is resilient to the RAG chunk ≠ on-disk-text gap (2026-09-21).** Tree-sitter indexes an INNER
  node, so a chunk stores `addToCart = (…) => {` while the file has `  const addToCart = (…) => {`. The
  model faithfully copies the chunk, so its byte-exact `find` isn't on disk → the old apply died with a
  misleading "index is stale — rebuild" (retrieval + diagnose were both CORRECT; this was the live
  TC-RPS-003 blocker AFTER the fix was found). `apply_patch` now falls back to `_resilient_replace`: a
  WHOLE-LINE match tolerating per-line indentation + a dropped leading declaration keyword
  (`const`/`let`/`export`/`async`/…), requiring a UNIQUE block AND equal find/replace line counts, then
  rebuilding the block from the REAL file lines so only the changed fragment moves. Refuses (returns None →
  the same error, improved wording) on any ambiguity — never edits the wrong line. `_locate_file` uses the
  same tolerance for its file-search fallback. Generic; no reindex needed.
- **⚠️ `_bug_class`/signal tokens read the SYMPTOM, not our appended guidance (2026-09-21).**
  `_failure_text_for` appends a "Fix the ROOT CAUSE… persisted or shared across screens/kiosks (a balance, a
  transaction)…" instruction tail. `_failure_point_text` now truncates the `[FAILED HERE]` segment at
  `OBSERVED:`/`Failing assertions:`/`Fix the ROOT CAUSE` (OBSERVED + assertions are captured separately), so
  that boilerplate no longer leaks in. Without it, an interaction bug (add-to-cart popup) was scoring the
  boilerplate's spec-vocab and mis-routing to the `spec` context profile (harmless here — retrieval still
  found the bug — but wrong, and it polluted the lexical re-rank signals). A genuine spec failure still
  routes `spec` from its own OBSERVED/assertion text.
- **GPU on GCE (msgraphrag / local models):** `g2-standard-8` = 1× NVIDIA L4 24GB; needs the
  NVIDIA driver (570 for kernel 6.8) + nvidia-container-toolkit, and **Secure Boot OFF** on Shielded
  VMs (blocks the unsigned module). `docker-compose.gpu.yml` reserves the GPU for `ollama`
  (`start-all.sh GPU=1`); `OLLAMA_CONTEXT_LENGTH=8192` so community-report prompts aren't truncated.
- **⚠️ BUILD-vs-DIAGNOSE parallelism split (one L4, one Ollama).** Ollama reserves KV cache =
  `num_parallel × num_ctx` PER LOADED MODEL. Hard-setting `OLLAMA_NUM_PARALLEL` high (e.g. 8) for a
  fast 14B graph build ALSO applies it to the 32B diagnose → KV overflows 24GB → CPU offload →
  ~10× slower → the diagnose TIMES OUT (`repair_local_timeout_s+30`). Fix: **`OLLAMA_NUM_PARALLEL=0`
  (auto, now the default)** — Ollama sizes slots per model by free VRAM (14B → up to 4 = parallel
  build; 32B → 1 = safe). Belt-and-braces: a ≥30B diagnose model's `num_ctx` is auto-clamped to
  `repair_local_num_ctx_cap_large` (12288) via `vision_agent.llm.effective_local_num_ctx()` (14B keeps
  16384; the context fit-trim reads the same effective value). A local timeout logs `ollama ps` and
  warns loudly on `⚠ OFFLOAD`. Only pin a high NUM_PARALLEL when builds/diagnoses run on SEPARATE GPUs.
- **GCP firewall single-IP rules go STALE.** Rules scoped to a laptop `/32` source-range break when
  the IP rotates (studio/neo4j/adminer "not accessible" though the ports listen fine). Update
  `--source-ranges` to the current IP; datastore ports (5432/6379/9000/9001) stay VM-internal.

### Recent milestones

Detailed history → [`docs/PROGRESS_LOG.md`](docs/PROGRESS_LOG.md). Design/deploy references:
`docs/CLOUD_AGNOSTIC_DECISION.md`, `docs/CLOUD_AGNOSTIC_DEPLOY.md`, `docs/GCP_Cost_Estimate.md`,
`docs/Auto_Repair_GraphRAG_Demo_Walkthrough.docx`.

- **2026-08-21 → 08-25 · false-PASS hardening + the Auto-Repair agent.** VPS card-station
  reader tests fixed (testid-first tap/type, error-banner verify guard, localStorage-preserving
  reset); `repair_agent/` self-healing pipeline (retrieve → diagnose → apply →
  tsc/vite → PR) became the 5th LangGraph agent; run-ordering + read-only Agentic View added.
  Demo bug branches created off `main`.
- **2026-08-25 · repair retrieval quality.** Cured the relabel-loop: jargon-stripped +
  code-type-filtered + two-lane (intent/action) search, dedicated design-spec retrieval, a
  snippet-truncation fix, and an embedding-model singleton (warm retrieve ~0.1s).
- **2026-09-09 · cloud-agnostic foundation** [`cloud-agnostic-agent`]. Ports-and-adapters:
  Postgres, MinIO/S3-compatible, Redis bus/queue, tenancy (row + blob isolation, JWT), Temporal,
  tracing, pgvector memory — all config-gated; defaults reproduce the pure-local MVP.
- **2026-09-10 → 09-11 · first live GCP VM deploy.** Whole compose stack on GCE + 4 deploy
  fixes; single-VM runs go IN-PROCESS; data-store browsing via Adminer/MinIO console; noVNC
  live-browser viewer.
- **2026-09-12 · deterministic plans + Tier-3 bridge-and-resume.** Content-based `version_hash`;
  Tier-3 bridges only the off-plan gap then resumes the 0-LLM plan, stops at the objective, and
  shares the plan's input values.
- **2026-09-13 → 09-15 · Auto-Repair LOCAL stack** (data-sovereignty backup): GraphRAG
  (Neo4j) + Ollama Llama, air-gap toggle, patch-quality guards — verified on the GCE CPU VM.
- **2026-09-16 · REAL Microsoft GraphRAG backend** (3rd retrieval option, ONE local model).
  Verified live on a GCE `g2-standard-8` (L4) with `qwen2.5-coder:14b`: GraphRAG 3.1.2 schema, GPU
  wiring, Neo4j export, incremental builds, POS-domain entity types + whole-function retrieval.
- **2026-09-18 · repair robustness: retrieval re-rank + VRAM split + debug dump.** Three fixes after
  the 32B-on-L4 diagnose timed out: (1) **lexical re-rank** in `retrieve_context` promotes the buggy
  chunk that vector similarity buried (TC-RPS-003 `quantity <= 1`), backend-agnostic + additive;
  (2) **build-vs-diagnose parallelism split** — `OLLAMA_NUM_PARALLEL=0` (auto) + a ≥30B `num_ctx`
  clamp (`effective_local_num_ctx()`) so a 32B diagnose stays on-GPU while 14B builds stay parallel,
  plus an `ollama ps` offload warning on timeout; (3) **`REPAIR_DEBUG_DUMP`** writes the exact
  prompt + raw model response per diagnose. No-regression: full suite unchanged (same 8 pre-existing
  vision/template fixture failures). Detail → `docs/PROGRESS_LOG.md`.
- **2026-09-20 · Auto-Repair v2 — failure-anchored retrieval + precision + verification (P0→P3).**
  The `REPAIR_DEBUG_DUMP` from 09-18 proved the real bug: retrieval anchored on the test TITLE fed the
  model the WRONG feature's code (payment, not add-to-cart), so no model could fix it. Generic redesign:
  failure-POINT retrieval lane, bug-class context routing (spec = unchanged/no-regression; interaction =
  tight), symptom-relevance patch verification with one nudged retry, and an opt-in RCA localisation pass.
  8 new unit tests (`tests/test_repair_retrieval.py`) prove the fix on the exact live failure; full suite
  128 passed + same 8 pre-existing fixture failures. Team write-up: `docs/Auto_Repair_v2_Code_Review.html`.
- **2026-09-21 · Auto-Repair v3 — two separate agents (RCA + code-fixing) + retrieval split + generic fixes.**
  The 09-20 dump proved retrieval STILL missed the buggy code (all payment chunks) AND the verify guard was
  defeated by a lexical substring false positive (`action` ⊂ `transaction`). Redesign: (1) a real **RCA agent**
  that reads only docs+tests and decides code_bug/spec_bug/test_invalid — a high-confidence spec/test verdict
  STOPS before the fixer; (2) **retrieval split** — msgraphrag holds only docs/tests, code comes from a
  separate efficient Chroma index; (3) **generic sub-token lexical matching** (kills the `action`⊂`transaction`
  class of false positive); (4) **screenshots** attached for Claude (multimodal only); (5) richer failure
  detail (execution trace + console log) for text models like qwen; (6) `REPAIR_RCA_PHASE` on by default with a
  conservative gate so Claude+Chroma never regresses. Frontend: two-agent pipeline UI + Auto-Repair window
  minimize/maximize/close. Full suite 133 passed + same 8 pre-existing failures. Detail → `docs/PROGRESS_LOG.md`.
- **2026-09-21 (later) · gap-vs-defect judge + debug-dump-on + Detailed Report window.** Three follow-ups:
  (1) **Option C** — a Claude-vision judge (`classify_wrong_screen_failure`) decides whether a wrong-screen
  verify is a recoverable nav GAP or a genuine app DEFECT; a confident defect fails FAST (no Tier-3
  replanning) so Auto-Repair fires on the real bug (e.g. TC-RPS-003's Quantity-Required popup), while gaps
  bridge exactly as before (`verify_defect_judge`, default on; A/B no-LLM equivalents documented for the
  local path). (2) **`REPAIR_DEBUG_DUMP` now defaults True** so the debug report always lands — the default
  Claude+Chroma path wasn't writing one because the flag was off. (3) **Detailed Report window** — a
  customer-facing 📋 walkthrough of every repair step incl. exactly what was sent to Claude (failure text,
  screenshots, retrieved code/doc context) and the patch returned; backend adds `diagnose.inputs`. Full
  suite 133 passed + same 8 pre-existing failures; studio build clean. Detail → `docs/PROGRESS_LOG.md`.
- **2026-09-21 (later still) · apply resilience + failure-point boilerplate hygiene.** A VM TC-RPS-003
  re-run had CORRECT retrieval + diagnose (Claude found the `quantity <= 1` guard and the right fix) but
  the patch failed to apply: the Tree-sitter chunk stored `addToCart = …` while the file has `const
  addToCart = …`, so the byte-exact `find` wasn't on disk. Fix: `_resilient_replace` — a whole-line,
  indentation- + declaration-keyword-tolerant apply (unique block + equal line counts required) that
  rebuilds from the real file lines; no reindex. Also: `_failure_point_text` now strips the appended "Fix
  the ROOT CAUSE… balance/transaction…" guidance so `_bug_class` + the lexical signals read the real
  symptom (the add-to-cart bug was mis-routing to the `spec` profile — harmless here, but wrong). 3 new
  tests; 136 passed + same 8 pre-existing failures. Detail → `docs/PROGRESS_LOG.md`.
- **2026-09-21 (latest) · generic RCA/fixer + interaction-element retrieval + Executive Summary report.**
  A VM re-run fixed the add-to-cart bug (PR raised) but mis-diagnosed a SECOND planted bug (a disabled
  mock-card input): its buggy control was never retrieved, so Claude invented a plausible-but-wrong fix.
  Generic (not point-fix) response: (1) app-agnostic RCA prompt + 5-category taxonomy (code/spec/test/
  **environment**/unknown) so infra/network/page-load + requirement/invalid-test failures route correctly
  and never get a code patch; (2) an **interaction-element retrieval lane** (`_interaction_query`) anchored
  on the element/test-ids the test tapped/typed; (3) diagnose prompt asks for `confidence`+`root_cause` and
  is told not to fabricate when the context lacks the cause; (4) Studio Detailed-report gains an **Executive
  Summary** default view (charts + KPIs, links to technical sections). 5 new tests; 138 passed + same 8
  pre-existing failures; studio build clean. Detail → `docs/PROGRESS_LOG.md`.
- **2026-09-21 (latest+) · relevance-centred snippet truncation — the retrieved bug must reach the model.**
  A VM re-run proved the mock-card bug (`setMockMode(false)` at `App.tsx:2484`) was IN a retrieved chunk
  (`PaymentScreen`, 2344–2527 ≈ 7.5 KB) but the flat `page_content[:4000]` prompt cut dropped it — so Claude
  guessed (it even self-reported `confidence: low`). Two-part generic fix, no reindex: (1) the per-chunk
  prompt cap is backend-aware — **Claude gets 14 KB/chunk (whole functions; its window is ~200K)**, local
  keeps 4 KB; (2) `_focus_snippet` centres an over-cap CODE chunk on the densest failure/interaction-signal
  window (sliding max-sum) so a deep, low-signal bug line survives even on the tight local budget. Design
  docs keep the plain head-cut. No-op when a chunk fits or its tail has no signal (no regression).
  **Index note:** a rebuild is still required after any branch switch (the index reflects BUILD-time code),
  but it does NOT fix truncation — that was a prompt-render bug, not staleness.

### Human review, plan thumbnails & report legibility

- **⚠️ HUMAN-IN-THE-LOOP review gates (2026-09-21) — 3 optional Approve/Reject checkpoints, ALL default OFF.**
  Toggled from the Studio **Configuration → Human Review** section (`PATCH /api/config/human-review`,
  persisted to `.env`; settings `human_review_explorer/test_plan/rca`). OFF ⇒ every flow runs byte-for-byte
  as before (no regression). (a) **App Explorer** (`human_review_explorer`): after an explore finishes the
  App Explorer page shows Approve/Reject; **Test Plan generation is blocked** (Studio localStorage
  `explorer_approved`) until approved; a Reject reason is stored per-app and passed to the NEXT explore
  (`/explore` `review_feedback` → env `EXPLORE_REVIEW_FEEDBACK` → `settings.explore_review_feedback` →
  appended to the explorer's `SUGGEST_EXPLORABLE_ACTIONS` prompt). (b) **Test Plan**
  (`human_review_test_plan`): Approve/Reject under a generated plan; a Reject reason is folded into the
  **Regenerate** (`/tc-plan` `review_feedback` appended to the planning prompt, `force=true`). (c) **RCA**
  (`human_review_rca`): the repair pipeline PAUSES after the RCA verdict, BEFORE the code-fixing agent —
  the RCA node sets `awaiting_review` → `_route_after_rca` ends → job status `awaiting_rca_review`; the
  Studio shows the verdict + Approve/Reject; **Approve** resumes via `POST /api/repair/{id}/rca-review`
  (re-invokes `run_repair` with `rca_override` = the reviewed verdict, so RCA is not re-run and the fixer
  runs); **Reject** re-runs RCA with the reason folded in (`review_feedback`) and pauses again — the
  code-fixing agent is never called until an Approve. Resume context lives on the job (`_resume`);
  `_store_repair_result` centralises the fold-in (auto + manual paths). Wiring never fires unless the flag
  is on, so Claude+Chroma is unchanged by default.
- **Test-plan step thumbnails (2026-09-21).** In the Studio Test Plan, a click/type step
  (`isInteractionStep`) shows a thumbnail of the ANNOTATED exploration screenshot for its `screen_id`
  (newest `screenshots/annotated/<screen>_<ts>.png`, served by `GET /api/screenshots/annotated/{file}`,
  URL via `annotatedScreenshotUrl`); click → lightbox. Pure add — no new backend data.
- **Report box legibility.** A native `<button>`/`<input>` RESETS text colour, so a stepper/label inside one
  is invisible on the dark theme unless it sets `color` explicitly (this bit the Executive-Summary pipeline
  stepper). Report code/output boxes use a legible `MONO` stack + explicit `color: var(--text)`; the report
  window container also sets `color`. Rebuild the **studio** container to deploy a studio change.

### Never

- **Never hardcode credentials anywhere** (a literal `user@example.com` in a prompt once caused a login
  loop). Thread intake credentials through `cred_hint`; `.env` holds a real `ANTHROPIC_API_KEY` and is
  gitignored — never expose or commit it.

---

## How the Auto-Repair agent works (retrieve → diagnose → apply → verify → PR)

The end-to-end flow, worked against the TC-RPS-001 login example. Only DIAGNOSE calls Claude;
retrieval is pure Chroma + HuggingFace.

**0. Trigger — the failed run, not a button.** On a failed run, `api/main.py`'s completion path
(gated by `auto_repair_on_failure`) spawns `_run_auto_repair(...)` for the FIRST failed test, mints a
`repair-<8hex>` job, broadcasts `repair_started` (pops the standalone window), and calls `run_repair`.
`_failure_text_for(tr)` turns the failed `TestResult` into a plain-English defect:
`"{test_id} failed. {summary}. {vision_summary} Failing steps: {≤3 failed-step observations}"`. That
raw string flows through the whole pipeline.

**1. RETRIEVE (RAG, no Claude)** — `repair_agent/repair_failed_test.py::retrieve_context`:
- `_retrieval_query()` strips test-harness jargon from the failure before embedding — the test id
  (`TC-RPS-001`) and words like `failed/failure/test/step/expected/observed/actual`. Those match the
  test WORKBOOK far more than the code (measured: buggy chunk rank ~11 → 0 once stripped). Only the
  **vector query** is cleaned; the **full raw failure** still goes to Claude.
- Two similarity searches over Chroma, embedded by HuggingFace `all-MiniLM-L6-v2`: a code-filtered one
  `search(rq, k=4, where={"type":{"$in":["code_block","code_file"]}})` (source only — lockfiles/doc
  can't crowd out code) plus `search(rq, k=2)` unfiltered for product context. Concatenated
  **code-first**, deduped by `(source, start_line, content-prefix)`, capped at `top_k+2`.
- Hits are whole logical units: at index time `parse_code_with_tree_sitter` uses a Tree-sitter TS query
  capturing function/class/method/arrow-fn nodes → each is one `code_block` doc tagged
  `source/type/start_line/end_line`. For the login failure, rank 0 is the entire `SignInScreen` function
  (App.tsx ~1285–1354) — the block holding `user.password !== `${password}-bug``.

**2. DIAGNOSE (the one Claude call)** — `propose_patch` formats `_DIAGNOSE_PROMPT` and makes a single
`invoke_json(get_llm(), …)` (Opus 4.8). Claude receives: the instruction to use ONLY the retrieved
context and return one JSON `{file_path, find, replace, explanation}` with the smallest possible fix,
**preserving every other guard/clause on the line** (this is why it keeps `!user ||` and only edits the
password comparison); `FAILED TEST / DEFECT:` = the full raw failure; `RETRIEVED CODE CONTEXT:` = the
4–6 hits rendered `Context N / File / Type / Lines / Snippet`. It returns e.g.
`find:"user.password !== `${password}-bug`" → replace:"user.password !== password"`. If Claude returns
nothing usable, `_demo_fallback_patch()` deterministically matches the planted bug (both `password` and
`normalizedPassword` forms) — the only non-Claude reasoning path.

**3. APPLY** — `apply_patch`/`_locate_file`: prefer Claude's `file_path` if inside `CODEBASE_DIR`, else
the unique file containing `find`; the `find` string must occur **exactly once** (0/≥2 → refuse), then a
single `replace(find, replace, 1)`.

**4. TEST → 5. BUILD** — TEST = `node_modules/.bin/tsc -b` (the app ships no unit runner; eslint is
deliberately excluded — thousands of pre-existing parser errors). BUILD = `npm run build`
(`tsc -b && vite build`), the real gate; `result["success"] = build.ok`.

**6. PR PREP (+ auto-open)** — `prepare_pr` refuses if `_repo_blocked_reason()` (mid-merge/rebase/
conflicts), then `git checkout -B repair/<test>-fix-<repairid8>` (unique per run) + a **pathspec commit**
(`git commit … -- <file>`, only the fixed file) + captures the diff. With `auto_pr` on and a green build,
`open_pull_request` pushes base + head to `origin` and (no `gh`) returns GitHub's prefilled
`/compare/<base>...<head>?expand=1` URL. Every stage calls `_emit(progress_cb, …)` → merged into the
in-memory job the dashboard/standalone window polls.

**Essence:** a jargon-stripped, code-type-filtered semantic search hands Claude the exact offending
function; Claude returns one minimal find/replace that never touches unrelated guards; the agent applies
it under uniqueness + repo-state guards, then `tsc`/`vite build` is the objective verdict before a
single-file PR.

---

## AWS production-readiness (deferred)

Cloud migration is deferred until the local-lab real-robot E2E run works. The full assessment is
archived in [`docs/AWS_READINESS.md`](docs/AWS_READINESS.md): lift-and-shift is ~80% / days via the
existing `VISION_BACKEND=bedrock` / `STORAGE_BACKEND=s3` / postgres toggles; the full event-mesh
topology (MSK/IoT Core/Lambda/SageMaker/Fargate/Redis) is ~30–40% / weeks; the diagram's "SageMaker
endpoint" maps to a **Bedrock Claude call**, not a self-hosted trained model (our training-free
advantage). Every cloud hook must stay behind a `*_backend` toggle defaulting to local. See
`[[aws-readiness-2026-07-20]]`.

> **POC vs production — not a contradiction.** The above is about the *production* migration (deferred
> until the real-robot E2E run). A separate, near-term **demo/POC** track is scoped in `AWS_READINESS.md`
> (the 2026-09-02 section) and in [`docs/AWS_POC_WebApps.md`](docs/AWS_POC_WebApps.md): a **web/desktop-app
> QA POC with NO robot testing** (physical-robot QA is its own later POC). It keeps the current Auto-Repair
> (Claude + Chroma + HuggingFace, no Neptune GraphRAG) and lands on **Docker → ECS Fargate → Bedrock → S3**
> (no EKS, no Neptune, no OpenSearch) — inside/near Free Tier. Use that doc for demo planning; use this
> pointer's assessment for the eventual full production topology.

---

## Conventions

- Add config via `vision_agent/config.py` `Settings` (never read `os.environ` directly).
- New robot capability → add the function to **all three** backends (`stubs.py`,
  `playwright_stubs.py`, `real_robot.py`) with an **identical signature**; agent code stays backend-agnostic.
- New API route → add to `api/main.py`; new persisted field → add to `api/models.py` and an
  idempotent migration in `api/database.py`.
- New app screen → no code change needed; re-run the App Explorer (Claude vision discovers elements).
- Windows-centric repo; UTF-8 stdout reconfiguration is intentional.
