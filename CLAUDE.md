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
- **Agent framework: LangGraph** (`langgraph>=0.2`) — four independent compiled `StateGraph`s.
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

- **Four LangGraph agents, one thread orchestrator.** Nodes are pure `state -> dict` partial
  updates; routing via `add_conditional_edges` + small `_route_*` predicates. TestRunner nests
  VisionAgent as its Tier-3 fallback. `api/main.py` runs TestRunner + DefectAgent in daemon threads
  and streams `test_started` / `step_result` / defect events over WebSocket
  (`test_runner/broadcaster.py` maps run_id → callback, keeping callables out of graph state).

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
exploration + config).

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

### Recent progress (2026-08-21) — VPS card-station reader tests + run ordering

- **VPS card station has THREE independent reader sessions sharing one `reader`/`readerCard` state**
  (`Kiosk_App/robotics-kiosk-pos/src/App.tsx`): `tap-load` = *Buy & Load* → **issue a NEW** card
  (`station-arm-real-card`), `tap-check` = *Check Balance*, `tap-topup` = **add money to an EXISTING**
  card (`station-topup-tap` → `station-topup` "Add Money to Card"). The USB HID reader types into an
  autofocused wedge input; `handleReaderInput` commits on a delimited track payload / Enter / 220ms
  debounce, routing to `issueCard` / `runCheck` / `runTopUpCardRead` by which session is armed.
  `issueCard` **requires the Load Amount FIRST** (presenting a card with no amount is rejected with
  *"Enter a non-zero load amount…"*); `topUpCard`/`runTopUpCardRead` require the card to be **already
  issued**.
- **TC-VPS-011 failure root cause (this run, `results/run-1-182645-2108`):** the cached plan (a) tapped
  the WRONG reader — `tap_real_card_button` (issue-new) instead of the top-up reader — and (b) armed it
  BEFORE typing the amount, so the presented card was rejected and the session stayed armed; the later
  vision step then typed the number into the open wedge (not the card-number field), so "Add Money"
  saw an empty number. Final screen stuck on *"Waiting for a card… Detected 0005322931."* (009 passed
  precisely because it types the amount FIRST.) **The card was read fine — the plan drove the wrong
  control in the wrong order.**
- **Two more app_map defects surfaced & fixed:** the top-up reader button (`station-topup-tap`) was
  MISSING from the map entirely (duplicate "Tap Real Card" label → vision emitted one element — now
  fixed durably by `_backfill_unmatched_dom`), and the top-up coords were STALE (`existing_card_number_input`
  1150,**750**→**496**; `add_money_to_card_button` 1050,**804**→1163,**550**). Ground-truth pulled live from
  the running app's DOM at the 1920×1080 exploration viewport. The button-row Y (550) is stable — the
  reader box opens *below* it — so the reader-based plan taps are position-safe.
- **Fixes applied (no regression to 009/010):** `app_map.json` patched (add `topup_tap_real_card_button`
  @1032,550 + fix two coords) — `version_hash` is `md5(explored_at | screen_ids)` so element edits DON'T
  change it → cached plan keys stay valid. TC-VPS-011's cached plan rewritten to the correct **reader-based**
  flow: `verify → type 4000 (load_amount) → tap TOP-UP 'Tap Real Card' → wait 5s (present card) → tap 'Add
  Money to Card' → verify 'Smart Card Loaded'` (Tier-1 HIT, `is_valid` ✓). 009/010 were ALREADY cache-misses
  before this work (a post-run re-exploration staled them) and will Tier-2 re-plan unchanged. A pre-patch
  `app_map.json.bak` is kept as a safety copy. **RESTART the backend to load the api/main.py + explorer edits.**
- **Test-run ordering (new feature).** Backend (`api/main.py` `start_run`): the `filter_tc` id list now
  executes in the **caller's order** (not `test_id` order), walking `filter_ids` in sequence with dedup +
  prefix-match preserved. Frontend (`../kiosk-test-studio` `src/pages/Execution.tsx`): the "Selected Test
  Cases" panel is now a **reorderable RUN ORDER list** (HTML5 drag-and-drop on the ≡ handle **and** ↑/↓
  buttons, no new deps); order persists to `localStorage.selected_tcs` (already an ordered array) and is
  sent verbatim as `filter_tc`, so the suite runs exactly as arranged.

### Recent progress (2026-08-21, run-2) — false-PASS fixes on VPS top-up / balance-check

Symptom (`results/run-2-200422-2108`): TC-VPS-002 typed the card number into the **wrong field** and
TC-VPS-003 hit a **"No card found"** app error, yet BOTH still PASSED. Three distinct defects, all fixed
with no regression (full suite: 98 passed, same 8 pre-existing env-only failures):

- **Root A — typing/tapping by stale coords hit the wrong target.** The (re-explored) app_map charted the
  top-up panel ~112px too LOW (`station-topup-number` live 419 vs map 531, `add_money` live 554 vs map 666;
  the panel is taller mid-exploration when a reader box is open). So the card number focused the amount
  field, and the Add-Money coord tap drifted past the 80px snap → `[raw]` click on empty space (button
  MISSED). **Fix:** tap/type now focus by **testid first** (see the tap-pipeline rule above) — coord-drift
  immune. Verified live: fields focus correctly, buttons hit, happy path clean.
- **Root B — `verify` passed on screen-identity alone, ignoring error banners.** A verify with only
  `expected_screen` (no `expected_text`) returned success once the DOM screen id matched, so a "No card
  found" / "…is not issued" / declined state on the RIGHT screen still passed. **Fix:** new **error-banner
  guard** in `validate_pipeline.run_validate_pipeline` (playwright, no-expected_text case only, so it can't
  override an explicit assertion): `robot.get_page_error_text()` reads visible error surfaces
  (`[data-testid*=error]`, `[role=alert]`, `aria-invalid`, error/danger classes) and FAILs the step when
  any non-empty banner shows. Errors the app clears on success render empty → no false fail (verified).
- **Root C — the between-test reset was WIPING the card store.** A card issued in TC-VPS-001 was "not
  issued" in TC-VPS-002 even though both run in ONE playwright session on the SAME page (NOT a new browser
  per test). Cause: `reset_to_entry()` (called between every test) ran `localStorage.clear()`, and the
  kiosk stores smart cards in `localStorage` key `robotics-pos-smart-cards` (it's the store whenever the
  shared card service is offline — `localhost:4000` was down, `VITE_CARD_SERVICE_ENABLED=false`). So the
  reset destroyed the card before the next test. (A manual cross-browser check "worked" only because it
  never cleared storage.) **Fix:** `reset_to_entry` now PRESERVES localStorage keys matching
  `settings.reset_preserve_storage_keys` (default `"smart-cards,cardbalance"`) across the clear, so issued
  cards survive for later top-up / balance / history tests; auth/session/orders/config keys don't match the
  list → still reset per test (no regression). Verified live: old full-clear loses the card ("No card
  found"), the preserve-reset keeps it. Alternative: bring up the shared card service and set
  `card_service_url` (appends `?cardServiceUrl=…`) so cards persist server-side regardless of the clear.
- New robot capabilities `focus_by_testid` / `tap_by_testid` / `get_page_error_text` live in
  `playwright_stubs.py` (real) and `stubs.py` (shared fallback → False/"" so real-arm/demo are unchanged).

### Never

- **Never hardcode credentials anywhere** (a literal `user@example.com` in a prompt once caused a login
  loop). Thread intake credentials through `cred_hint`; `.env` holds a real `ANTHROPIC_API_KEY` and is
  gitignored — never expose or commit it.

---

## AWS production-readiness (deferred)

Cloud migration is deferred until the local-lab real-robot E2E run works. The full assessment is
archived in [`docs/AWS_READINESS.md`](docs/AWS_READINESS.md): lift-and-shift is ~80% / days via the
existing `VISION_BACKEND=bedrock` / `STORAGE_BACKEND=s3` / postgres toggles; the full event-mesh
topology (MSK/IoT Core/Lambda/SageMaker/Fargate/Redis) is ~30–40% / weeks; the diagram's "SageMaker
endpoint" maps to a **Bedrock Claude call**, not a self-hosted trained model (our training-free
advantage). Every cloud hook must stay behind a `*_backend` toggle defaulting to local. See
`[[aws-readiness-2026-07-20]]`.

---

## Conventions

- Add config via `vision_agent/config.py` `Settings` (never read `os.environ` directly).
- New robot capability → add the function to **all three** backends (`stubs.py`,
  `playwright_stubs.py`, `real_robot.py`) with an **identical signature**; agent code stays backend-agnostic.
- New API route → add to `api/main.py`; new persisted field → add to `api/models.py` and an
  idempotent migration in `api/database.py`.
- New app screen → no code change needed; re-run the App Explorer (Claude vision discovers elements).
- Windows-centric repo; UTF-8 stdout reconfiguration is intentional.
