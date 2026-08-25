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

### Recent progress (2026-08-24) — Auto-Repair agent (RAG + Claude self-healing) + demo

New **`repair_agent/`** module: the self-healing arm of defect intelligence. It takes a failed-test
message and runs one pipeline — **retrieve → diagnose → apply → unit-test → build → PR-prep** — surfaced
live in a new Kiosk Test Studio page.

- **`repair_agent/parse_code_and_store.py`** — the POC's `ParseCodeAndStore.py` brought in almost
  verbatim. The retrieval brain (Tree-sitter chunking, HuggingFace `all-MiniLM-L6-v2` embeddings, Chroma
  vector store) is **LEFT AS-IS**. Refinements only: paths are config-driven (`settings.repair_*`), it
  indexes the **live** kiosk app (`../Kiosk_App/robotics-kiosk-pos`) + the design-doc/test-workbook
  artifacts (moved to `./docs`), tree-sitter is imported lazily (falls back to text chunking), and the
  walk now skips dot-dirs + stray folders. **Gotcha that bit us:** a leftover `.rag/kiosk_rag_index.json`
  (1.1 MB) + `repair_brief.md` inside the live app dominated similarity search and fed Claude stale,
  hallucinated markup → wrong patch. `SKIP_DIRS` + dot-dir skipping fixed it (5208 → 371 real chunks).
- **`repair_agent/repair_failed_test.py`** — **Claude does the reasoning** (`get_llm()` Opus 4.8 via
  `invoke_json`): retrieve top-k from Chroma → ask for ONE minimal find/replace patch → apply
  (single-occurrence + in-codebase guards, LLM path tolerated) → **unit test** (a TS type-check via
  `node_modules/.bin/tsc -b`; the app ships no test runner, and its **eslint is broken with 5357
  pre-existing parser errors** so lint is intentionally NOT the gate) → **build** (`npm run build` =
  `tsc -b && vite build`, the real validation) → **PR-prep** (local branch + commit + diff). A demo
  fallback rule handles the planted bug if Claude is unavailable.
- **PR is gated.** `run_repair()` only *prepares* a local branch/commit/diff. Pushing + `gh pr create`
  is a SEPARATE `open_pull_request()` behind `POST /api/repair/{id}/open-pr` (confirm=true) — outward-
  facing, never automatic. Base branch = `settings.repair_pr_base` (`REPAIR_PR_BASE`).
- **API** (`api/main.py`): `POST /api/repair` (bg thread, streams stages into an in-memory job) ·
  `GET /api/repair/{id}` (poll) · `POST /api/repair/{id}/open-pr` (gated push+PR) ·
  `POST /api/repair/index` + `GET /api/repair/index` (build/status of the Chroma index).
- **Frontend** (`../kiosk-test-studio`): new **Auto-Repair** page (`src/pages/AutoRepair.tsx`, nav group
  "Self-Healing") — a live vertical stepper of the 6 stages with per-stage detail (RAG hits, the Claude
  red/green diff, tsc/vite output, the git diff) and an "Open PR" button (two-click confirm). Polls
  `GET /api/repair/{id}`.
- **The demo (RPS login).** The arm-reachable login flow sets `allowAnyCredentials=true`, which bypasses
  the password comparison — so the POC's `${password}-bug` would NOT fail the live test. Instead the
  planted bug is in the Sign-In submit **gate**: `credentialsReady = … && password.trim().length < 0`
  (Sign In never enables). It lives on an **isolated `demo/rps-login-bug` branch** of the live app (bug
  committed there; `arm-reachable-area` is untouched — no regression). The agent's fix reverts `< 0` → `> 0`;
  because a pure revert nets to zero against a clean base, the PR base IS `demo/rps-login-bug` so the fix
  shows as a real, reviewable diff. Verified end-to-end: Claude finds the exact line, type-check + build
  pass, branch `repair/tc-rps-001-fix` + commit + diff prepared. To replay from the UI, the live app must
  be on `demo/rps-login-bug` (buggy) with no stale `repair/tc-rps-001-fix` branch.
- **Config** (`vision_agent/config.py`): `repair_codebase_dir`, `repair_docs_dir`, `repair_persist_dir`
  (gitignored), `repair_embedding_model`, `repair_pr_remote`, `repair_pr_base`.
- **RESTART the backend** to load the new `repair_agent` module + `api/main.py` routes (uvicorn has no
  `--reload`). Build the index once (`POST /api/repair/index` or `python -m repair_agent.parse_code_and_store`).

#### Update (2026-08-24, later) — auto-trigger, new window, auto-PR, standard-layout bug

- **Kiosk layout for RPS testing is now `standard`, not arm-reachable.** The RPS login test uses
  `http://localhost:5173/?screenLayout=standard&flowMode=full`. `standard` → `allowAnyCredentials=false`,
  so the REAL password comparison runs (arm-reachable bypasses it). `playwright_stubs._kiosk_url()` now
  **respects an explicit `screenLayout` already on the configured URL** (won't force-append the default),
  so the configured kiosk-2 URL wins.
- **The demo bug changed to "valid user REJECTED"** (more visible than a disabled button). On
  `demo/rps-login-bug`, `App.tsx:1900` compares `user.password !== ` `` `${normalizedPassword}-bug` `` — so a
  valid `tester@kiosk.local / Password123` login is rejected with *"Username or password is incorrect."*
  The agent's minimal fix drops the `-bug` suffix (guard clauses preserved — the prompt now forbids
  simplifying unrelated logic). `arm-reachable-area` stays untouched.
- **Auto-repair now runs AUTOMATICALLY on a failed run** (`settings.auto_repair_on_failure`, default on):
  the run-completion path spawns `_run_auto_repair` for the FIRST failed test, which streams stages into an
  in-memory job and broadcasts `repair_started` / `repair_done` on the run WebSocket.
- **New window.** Live Monitor listens for `repair_started` and pops a standalone Auto-Repair window
  (`window.open('?repair=<id>')`; `App.tsx` renders `<AutoRepair standaloneRepairId>` chrome-free) plus a
  prominent in-page banner (popup-blocker fallback). The banner shows the live status and the PR link.
- **PR is now RAISED AUTOMATICALLY** (`settings.repair_auto_pr`, default on) after a green build. `gh` is
  NOT installed, so `open_pull_request` pushes BOTH branches (base `demo/rps-login-bug` + head
  `repair/tc-rps-001-fix`) to `origin` and returns GitHub's prefilled **compare URL**
  (`/compare/<base>...<head>?expand=1`). ⚠️ This **pushes the demo bug branch to the GitHub remote**
  (`srik-g/robotics-kiosk-pos`) — required so the PR has a non-empty diff (the fix is a pure revert). Set
  `REPAIR_AUTO_PR=false` to keep it local. The `/api/repair/{id}/open-pr` endpoint remains as a manual path.
- **RAG retrieval hardened** (this was the bug that made Claude refuse): a failure like "TC-RPS-001 login
  rejected" matched the **test workbook + `package-lock.json`** far more than the code, so no source reached
  the LLM. Fixes in `parse_code_and_store` / `repair_failed_test`: skip lock files (`SKIP_FILES`); a metadata
  filter pulls **code chunks first** (`search(where={"type":{"$in":["code_block","code_file"]}})`); and
  `_retrieval_query()` strips test-id / test-jargon from the vector query (the buggy chunk went rank ~11 → 0).
  The full failure text still goes to the LLM prompt. Rebuild the index after any of these.

### Recent progress (2026-08-24, run-18) — false-PASS fix: uncharted expected_screen

Symptom (`results/run-18-191056-2408`): TC-RPS-001 on the buggy login **PASSED** even though the
products screen never appeared. Two linked causes:

- **The App Explorer can't log in on `demo/rps-login-bug`** — the intentional bug rejects valid
  credentials, so exploration never reaches (never charts) the `products` screen. This is *expected*
  given the bug; the sign-in screen itself charts fine (the test's type/tap steps worked).
- **The false PASS (the real bug):** a `verify` whose `expected_screen` isn't in the app_map returned
  `success=None` (a "verification gap"), and `run_vision_step` recorded that as `success=True`
  ("don't fail over an uncharted screen"). So a login that stayed on sign-in still passed.

Fix (`vision_agent/nodes/validate_pipeline.py`, playwright only — real/demo keep the gap since they
have no live DOM): when `expected_screen` isn't charted, **confirm against the LIVE DOM instead of
free-passing**. `get_dom_screen_id()` (normalizes `products-screen`→`products`, `signin-screen`→
`sign_in`) is compared to the expected screen via `_screens_equivalent` (ignores case/separators,
substring-tolerant), with the expected on-screen text (`expected_text` or a quoted phrase parsed from
the step) as a secondary confirm. Match → PASS (`dom_screen`/`dom_text`); neither → FAIL as a
`screen_id` mismatch (so the strict Claude intent-judge in `run_vision_step` can still rescue a
genuinely-correct-but-differently-named screen). Verified live on the running kiosk: buggy login →
DOM stays `signin` + no products text → **FAIL**; fixed login → DOM `products-screen` → **PASS**.
Note the error-banner selector is unreliable here (missed the login-failed popup, false-matched a
"Sign Out" button), so screen-identity is the decisive signal, not the banner.

Consequence for the demo: the incomplete app_map (no `products`) no longer matters — the verify checks
the live DOM, so TC-RPS-001 now correctly FAILS on the bug (→ auto-repair triggers) and PASSES once
fixed. Re-exploring against a *working* build only matters if you want `products` charted for other
purposes.

### Recent progress (2026-08-24, run-20) — no-retry on verify fail + auto-repair crash fix

`results/run-20-193041-2408`: the verify correctly FAILED now, but two follow-on issues:

- **Unwanted login RETRIES.** A failed `verify` handed off to Tier-3 vision, which re-planned and
  re-attempted the whole login 3× (RETRY 1/2/3) — the test never asked to retry. **Fix**
  (`run_vision_step.py`, gated by `settings.verify_failure_stops_run`, default on, **all backends**): a
  failed `verify` is a test ASSERTION failure → **fail terminally, no Tier-3 handoff**. Action steps
  (tap/type) that fail still hand off so Tier-3 can locate the element via vision and COMPLETE the
  step (not an outcome retry) — so no regression to element-recovery. Cross-kiosk is unaffected: the
  verify's kiosk-switch (`_kid_of_screen`) and the Claude intent-rescue both run BEFORE the failure
  finalizes, so only a genuine assertion failure reaches the terminal guard. This is the playwright
  analogue of the real-backend `robot_error_stops_run` "no Tier-3 wandering" rule.
- **Auto-repair never fired** — `_run_auto_repair` crashed with `TypeError: _repair_set() got multiple
  values for argument 'repair_id'` (the helper's positional param collided with a `repair_id=` kwarg
  callers pass for the job dict). **Fix:** renamed the positional to `rid` so `repair_id=` lands in
  `**fields`. Both `start_repair` and `_run_auto_repair` were affected.

### Recent progress (2026-08-24, run-23) — repair git-safety + single base branch

`repair/tc-rps-001-fix` came out full of garbage (a whole main↔arm-reachable merge, no clean fix
commit, PR failed). Root cause was **git STATE, not the fix logic**: the demo base had been rebuilt off
`main`, an earlier run's `origin/repair/tc-rps-001-fix` (arm-reachable lineage) got `git pull`ed into
the new main-lineage branch → a stuck merge with conflicts; the auto-repair then branched/committed on
top of that mess. (The test-side fixes from run-20 worked: verify failed cleanly, no retry, auto-repair
fired.) Fixes:

- **Single base branch: `demo/rps-login-bug`, based off `main`** (bug `App.tsx:1292`
  `user.password !== ` `` `${password}-bug` ``). All intentional bugs + all repair branches live off this.
  `arm-reachable-area` is no longer used for the repair demo. `REPAIR_PR_BASE=demo/rps-login-bug`.
- **Repo-state guard** (`repair_failed_test._repo_blocked_reason`): the agent now REFUSES to apply/commit
  when the codebase is mid-merge / mid-rebase / has unresolved conflicts — it stops with a clear message
  instead of sweeping a merge into the fix branch. Checked before APPLY (in `run_repair`) and again in
  `prepare_pr`.
- **Unique per-run branch names** (`repair/<test_id>-fix-<repair_id8>`) so a fresh push never collides
  with (and later gets merged into) a previous run's origin branch on a different lineage.
- **Pathspec commit** — `git commit -m … -- <file>` commits ONLY the fixed file, so nothing else can leak
  into the branch. **Generalized demo fallback** matches BOTH bug forms (`normalizedPassword` and `password`).
- **RAG index lock** — on Windows the running backend holds the Chroma DB open, so a CLI rebuild hits
  `WinError 32`. `build_codebase_index` now raises a clear message; **rebuild via the studio's "Rebuild
  index" button right after a backend RESTART, before running any repair** (fresh process = no open handle).
- The stuck merge was aborted, the garbage `repair/tc-rps-001-fix` deleted, working tree clean on
  `demo/rps-login-bug`. Origin still has stale `origin/repair/tc-rps-001-fix` / `origin/demo/rps-login-bug`
  from earlier runs — harmless (unique names avoid collisions now); delete them on GitHub if desired.

### Recent progress (2026-08-25) — Auto-Repair page = repairs dashboard + lock-proof index rebuild

- **The Auto-Repair page is now a repairs DASHBOARD, not a manual trigger.** Repairs already run
  automatically on a failed test (`auto_repair_on_failure`), so the old manual "Failed test description +
  Run Auto-Repair" form (with a hardcoded `DEFAULT_FAILURE` example) was both redundant and confusing —
  it looked like "one issue is always shown" when it was just prefill. Removed. The page
  (`../kiosk-test-studio/src/pages/AutoRepair.tsx`) now polls `GET /api/repair` every 2.5s and lists every
  repair this backend session has run, newest first — each row shows **test id · auto/manual · source
  run_id · relative time · failure summary** and expands to the live 6-stage pipeline + result banner +
  gated Open-PR button. Newest repair auto-expands; empty state explains repairs appear when a test fails.
  The standalone window Live Monitor pops (`?repair=<id>`) reuses the SAME card (locked open) — so the
  auto-triggered flow is unchanged.
- **Route-ordering gotcha (fixed):** the literal-path `GET /api/repair/index` MUST be declared before
  the parameterized `GET /api/repair/{repair_id}`, or FastAPI matches `/api/repair/index` as
  `get_repair(repair_id="index")` → 404. That 404 made the index-status poll never see `building:false`,
  so the UI spun on "Indexing…" even after the build finished. The index routes now sit above
  `{repair_id}`; a `TestClient` check confirms `/api/repair/index`→200, `/api/repair`→200, unknown id→404.
- **New backend endpoint `GET /api/repair`** (`api/main.py` `list_repairs`) returns all `_repair_jobs`
  (full stages + result) sorted by `created_at` desc. In-memory only → **repair history resets on backend
  restart** (documented in the empty state). `POST /api/repair` (manual start) still exists but the UI no
  longer calls it; `client.ts` dropped `startRepair`, added `listRepairs`.
- **Index rebuild is now lock-proof — no restart needed.** The old `build_codebase_index` did
  `shutil.rmtree(PERSIST_DIR)` then `Chroma.from_documents`, which on Windows hit `WinError 32` (the running
  backend holds the Chroma sqlite open) and could leave a half-deleted, corrupt index — the "Rebuild index
  seems stuck" symptom. It now resets the collection **in-place** via the same persistent client
  (`Chroma(...).delete_collection()` → new `Chroma(...).add_documents(...)`), sidestepping the file lock, and
  returns the chunk count (surfaced in the index status message + the UI chip). "Rebuild index" works while
  the backend runs. (Supersedes the run-23 "rebuild only after restart" note.)

### Recent progress (2026-08-25) — repair is now a 5th LangGraph agent · 2 more demo bugs · delete-PR · Agentic View

- **Auto-Repair is now a real LangGraph StateGraph** (`repair_agent/agent.py` + `state.py` + `nodes/` +
  `broadcaster.py`), matching the other four agents. Nodes are pure `state -> dict` updates calling the
  same tested helpers in `repair_failed_test.py`; `run_repair()` is now a thin wrapper that registers the
  progress callback on `repair_agent.broadcaster` (callables stay OUT of graph state, keyed by a
  per-invocation id), invokes the compiled graph, and reshapes the final state into the **identical**
  result dict the API/UI already consumes. Conditional edges: dry-run stops after `diagnose`; a dirty-repo
  guard stops before `apply`. Verified: dry-run streams `retrieve/diagnose` then ends, result shape +
  Claude's minimal patch unchanged. No API changes; no regression.
- **Two more intentional demo bugs, each on its OWN branch off `main`** (join the existing
  `demo/rps-login-bug`): `demo/rps-products-bug` — Add-to-Cart passes a hardcoded `0` (`onAddToCart(product,
  0)`) so the cart never fills (test: a purchase/E2E); `demo/rps-design-bug` — **cross-kiosk card sharing
  broken**: `src/lib/storage.ts::refreshCard` hits the singular `/api/card/${num}` instead of the shared card
  service's `/api/cards/${num}`, so a card issued at the SmartCardStation returns 404 at the Kiosk POS and is
  dropped from cache ("card not found") — violating the design doc's "a card issued at kiosk-1 can be
  validated and charged at kiosk-2" (test: **TC-E2E-001**). Both are single-line diffs off main with
  **unchanged testids** (app_map/plans stay valid).
- **The design/card bug has NO fallback ON PURPOSE** — it's the showcase for Claude reading the design doc +
  the sibling `/api/cards` calls and reasoning out the endpoint fix itself. `_demo_fallback_patch` covers only
  login + products (so those still work if Claude is down). To make the no-fallback path reliable, two general
  fixes: (a) **file-neighborhood expansion** in `retrieve_context` — when a SMALL support module is implicated
  (≤25 chunks, e.g. `storage.ts`), pull its WHOLE module so Claude sees the buggy line next to its correct
  siblings (App.tsx is skipped to stay focused); the code `top_k` is now 6 and `_retrieval_query` also strips
  `TC-E2E-001`-style ids. (b) **`invoke_json` now recovers a prose-wrapped JSON object** (`_extract_json_object`
  scans each `{` and returns the first that actually parses — Claude often prefixes analysis whose backticks
  contain `${num}`). Verified: 3/3 runs produce the exact `/api/card/`→`/api/cards/` fix. Benefits all agents.
- ⚠️ **Rebuild the RAG index after checking out a demo branch** — the index reflects the code on disk AT BUILD
  TIME, so the buggy line must be indexed for retrieval to surface it. **Demo flow:** check out the bug's
  branch → **rebuild index** (Studio button / `POST /api/repair/index`) → run its test → watch auto-repair.
- **PR base is now dynamic** = the branch the app is on when the test fails (`_current_branch()` /
  `_pr_base()`), not a hardcoded `demo/rps-login-bug`. This is REQUIRED now that several demo bug branches
  exist — the fix branch diffs cleanly against whichever demo branch was checked out (falls back to
  `settings.repair_pr_base` if detached or already on a `repair/*` branch).
- **Delete-PR** (`POST /api/repair/{id}/delete-pr` → `delete_pull_request`): deletes the pushed `repair/*`
  head branch on origin (which closes the PR) + the local branch; refuses to touch `demo/*` or non-repair
  branches. Surfaced as a two-click **🗑 Delete PR** button next to **↗ View PR** on the Auto-Repair
  dashboard, so repeated demo runs don't pile up PRs.
- **New "Agentic View" page** (`../kiosk-test-studio/src/pages/AgenticView.tsx`, nav under Overview) — a
  read-only, big-monitor command center for customer demos: a flowing agent pipeline (App Explorer → Test
  Runner → Defect Intelligence → Auto-Repair, plus Vision/Supervisor support chips) with live per-agent
  status (idle/working/complete/findings), headline KPIs (screens mapped, tests executed, defects,
  auto-repaired), a pass-rate donut, the 0-LLM steady-state story, and a live activity feed. It polls
  `GET /runs` + `/repair` + `/app-map` + `/runs/{id}/defects` every 3s and **starts/changes nothing** — pure
  visualization, so it can't regress anything. Self-contained inline styles + keyframes; dark-palette CSS vars.

### Recent progress (2026-08-25) — repair RETRIEVE latency fix (embedding-model caching)

Symptom (observed on the TC-RPS-001 auto-repair): the **retrieve** stage was slow even though the RAG
index was already built — a built index should make retrieval near-instant. Root cause was NOT the
Chroma lookup (milliseconds over ~200 chunks) but **the embedding model being reloaded on every search
call**. `parse_code_and_store._vector_db()` did `HuggingFaceEmbeddings(model_name=…)` (loads
`all-MiniLM-L6-v2` — ~90 MB of weights + torch init, SECONDS) on EVERY `search()`, and one
`retrieve_context` fires several searches (code-filtered + general + per-file expansion), so a single
retrieve reloaded the model **4–8×**. For the login bug it was worse: the buggy code lives in the LARGE
`App.tsx`, which exceeds the small-module expansion cap, so the file-neighborhood loop re-probed
`App.tsx` (a fresh k=60 search, each reloading the model) once per top hit — none of which expanded.

Fixes (both in `repair_agent/`, no behaviour change to WHICH chunks are retrieved):
- **Embedding-model singleton** (`parse_code_and_store._get_embedding_model` + `_EMBEDDING_MODEL`): the
  model loads **once per backend process** and is reused. `_vector_db()` still opens a **fresh Chroma
  handle per search** so every search reflects the CURRENT on-disk collection — immune to an in-place
  "Rebuild index" (this process or a separate one) with no stale-handle risk (opening the handle just
  reads the small sqlite; the model load was the cost). `build_codebase_index` uses the same cached model.
- **Expansion-loop dedup** (`repair_failed_test.retrieve_context`): track `probed` sources so each file
  is searched **at most once**. A big file like `App.tsx` (never added to `seen_files` because it exceeds
  `_SMALL_FILE_MAX_CHUNKS`) is no longer re-probed for every one of its top chunks.
- Measured: warm `retrieve_context` dropped from seconds to **~0.1s** (first retrieve of a process still
  pays the one-time ~10s cold torch/model load). Small-module expansion (e.g. `storage.ts` for the
  card-sharing design bug) still pulls the whole module — no regression to retrieval content, verified
  end-to-end for both the App.tsx (login) and storage.ts (design) bug shapes. This helps every agent that
  calls `search()`. **RESTART the backend** to load the change (uvicorn has no `--reload`).

### Recent progress (2026-08-25) — Auto-Repair local-LLM backup (Claude primary, Ollama fallback)

The Auto-Repair DIAGNOSE step (the pipeline's ONE LLM call) can now fall back to a **local, self-hosted
model** when Claude can't be reached — Claude stays primary and default. A Configuration-page toggle drives
which model is tried first.

- **Fallback chain** (`repair_agent/repair_failed_test.py`): `propose_patch` iterates an ordered provider
  list from `_diagnose_providers()`, driven by `settings.repair_llm_backend` (`claude` | `local`):
  `claude` → **[Claude, local]** (Claude primary, local backup — the "Claude unreachable" case);
  `local` → **[local, Claude]** (used to TEST the local path). Each provider is wrapped in try/except; a
  missing/unreachable one (Ollama server down, `langchain-ollama` not installed → clean `ImportError`) is
  skipped and the next is tried. The deterministic `_demo_fallback_patch` remains the final last resort.
  Provider factories are LAZY, so selecting `claude` never touches Ollama and vice-versa — no behavior
  change / no new hard dependency until the local path is actually used.
- **Local model = Ollama + Qwen2.5-Coder-14B** (`vision_agent/llm.py::get_local_llm`, lazy-imports
  `langchain_ollama.ChatOllama`). Config in `vision_agent/config.py`: `repair_llm_backend` (default
  `claude`), `repair_local_model` (`qwen2.5-coder:14b`), `repair_local_base_url`
  (`http://localhost:11434`), `repair_local_num_ctx` (8192), `repair_local_timeout_s` (120). `invoke_json`'s
  existing fence-strip + prose-wrapped-JSON recovery is what makes a weaker local model's messier output
  usable.
- **`RepairPatch` now carries `source` (`claude`|`local`|`demo-fallback`) + `model`**, flowing through
  `asdict` → diagnose stage → the job the UI polls. The Auto-Repair dashboard shows a **"produced by" badge**
  on the diagnose step (☁ Claude / 🖥 Local LLM · model / ⚙ Demo fallback) — a visual confirmation of which
  model actually made the fix.
- **UI toggle** — Configuration page → **Auto-Repair Model** card (two radio cards, Claude default). Backend:
  `GET /api/config` returns `repair_llm{backend,local_model,local_base_url}`; `PATCH /api/config/repair-llm`
  `{backend}` sets it **LIVE** (`propose_patch` reads `settings.repair_llm_backend` at call time → next
  repair uses the new choice, **no restart**) and persists `REPAIR_LLM_BACKEND` to `.env` via `_persist_env`
  (API key + other lines untouched).
- **To enable the local path:** `pip install langchain-ollama`, install Ollama, `ollama pull
  qwen2.5-coder:14b` (~9 GB VRAM at Q4; runs on a 12 GB consumer GPU / Apple Silicon, CPU-only works but is
  slow — fine for a rare fallback). Not installing it changes nothing (Claude-only, as before).
- **To TEST it:** Configuration → Auto-Repair Model → **Local LLM** → Save (chip shows the model). Check out a
  demo bug branch, rebuild the RAG index, run its test → it fails → the diagnose badge reads **🖥 Local LLM ·
  qwen2.5-coder:14b**. To verify the *automatic backup*, leave it on **Claude** but stop the network / unset
  the key → the run falls through to Local automatically (badge shows Local). Simpler bugs (login `-bug`,
  products `0`) fix reliably on 14B; the design-doc card-sharing bug is weaker locally — that's expected for
  a backup. `python -m repair_agent` internals verified end-to-end with fakes (order + fallback + demo
  last-resort). **No regression:** default is Claude, lazy imports, frontend `tsc + vite build` clean.

### Recent progress (2026-08-25) — 4th demo bug (cross-kiosk txn) + TC-VPS-009 false-PASS fix

- **New demo bug branch `demo/rps-vps-txn-bug`** (off `main`, single-line, testids unchanged): in
  `src/App.tsx` `createApprovedStatusFromCardNumber`, the purchase-persistence guard is
  `if (balanceAfter !== undefined && !issuedSmartCard)`, so an RPS (Kiosk POS) purchase against an
  **issued** ValuePass smart card skips `recordCardTransaction` (deduct + log PURCHASE). The purchase
  still shows approved on RPS, but the shared card store never sees it → when the card is later checked
  on the VPS SmartCardStation the **balance is unchanged from its initial load and the PURCHASE
  transaction is missing** — violating the design (a purchase at kiosk-2 must reflect in balance +
  history at kiosk-1). The fix reverts the guard to `if (balanceAfter !== undefined)`. Contradicts the
  comment right below it → diagnosable; **no `_demo_fallback_patch`** for it (genuine LLM test, like the
  card-sharing design bug). Chosen write-side (not a VPS read-side "don't refresh") because VPS/RPS are
  **same-origin** (`localhost:5173`) and share `localStorage`, so a read-side bug wouldn't reproduce.
- **False-PASS fix — TC-VPS-009 was asserting the wrong thing** (`results/run-30-145029-2508`): the
  test PASSED on the buggy build even though the screenshot showed no purchase in the history. Root
  cause: the plan's final `verify` had `expected_value:"0005322931"` — the card number **typed two
  steps earlier** — so the Tier-1 DOM check found it and passed; the transaction ledger was never
  asserted (a tautology — asserting a value you just typed proves nothing about the outcome). Fix
  (targeted, deterministic, 0-LLM; user chose this over a general engine guard, and to keep the test
  **check-only**): the verify now asserts **`expected_value:"PURCHASE"`** — the VPS ledger renders each
  row as `<small>{txn.type} · {txn.kioskId} · …</small>`, so a real purchase shows the text `PURCHASE`,
  absent under the bug (playwright `_check_text` → deterministic `False` when absent → verify FAILS →
  auto-repair fires). `PURCHASE` appears **only** in the ledger on the station screen (other "purchase"
  strings live on unmounted screens), so no false match. Applied to: the **DB** `test_cases`
  `expected_results_raw` (rewritten to explicitly name `'PURCHASE'` so a Tier-2 re-plan also asserts it)
  AND a **pre-written cached plan** at the new cache key (`test_plans/TC-VPS-009_f019e1af98.json`;
  cache key = `md5(planner_ver|test_id|steps_raw|expected_results_raw|map_version)`, so changing the
  expected text re-keys it — the old `_c8123c2039.json` was orphaned and removed). Verified end-to-end:
  Tier-1 HIT + `is_valid` on the new plan, final verify `expected_value=='PURCHASE'`.
  - ⚠️ **Dependency of the check-only design:** TC-VPS-009 does NOT itself purchase — it just checks
    card 0005322931. Run it **after** a test that makes an RPS purchase with that card, or it fails
    regardless of the bug (no purchase to show). Precondition documents this.
  - ⚠️ The **Excel workbook** (`docs/kiosk_e2e_tests.xlsx`, row 18 "Expected Results"/"Preconditions")
    was **NOT** updated — the file was locked (open). Re-importing test cases from it would revert the
    DB `expected_results_raw` and orphan the new plan. Sync it (paste the DB's new expected text, which
    must byte-match) before any re-import, or the fix silently reverts.

### Recent progress (2026-08-25) — Auto-Repair DIAGNOSE timeout + cancel button

Two robustness fixes after a repair hung (Anthropic credits were exhausted → Claude 400'd fast → the
chain fell through to the local Ollama/Qwen-14B backup, which was slow/cold-loading and appeared stuck):

- **Per-provider DIAGNOSE timeout** (`settings.repair_diagnose_timeout_s`, default 90s): `propose_patch`
  runs each provider's `invoke_json` on a daemon thread via `_invoke_with_deadline` and abandons it on
  timeout → moves to the next provider, then the demo fallback. A stuck/slow model can NEVER freeze the
  repair now. The abandoned daemon thread finishes harmlessly (daemon → never blocks shutdown). Verified:
  a hanging primary times out and the backup completes; provider order + fallback unchanged.
- **Cancel** — cooperative, since Python can't force-kill a thread: `repair_agent/canceller.py` maps the
  graph's internal id → a `threading.Event`; every node calls `bail_if_cancelled()` at its boundary and
  the DIAGNOSE wait polls it, so a cancel stops at the next stage / interrupts a slow diagnose (raising
  `RepairCancelled`, caught by `run_repair` → result `{cancelled:True}`). API owns the Event:
  `POST /api/repair/{id}/cancel` sets it + marks the job `cancelling` (→ `cancelled` when it unwinds);
  `_repair_cancel` dict, cleaned up in `finally`. `run_repair(..., cancel_event=…)` registers it under the
  internal rid. Frontend: a **⨯ Cancel** button on each running Auto-Repair card (`AutoRepair.tsx`,
  `api.cancelRepair`), new `cancelling`/`cancelled` badges + a neutral "Cancelled — no changes committed"
  banner. **No regression:** normal dry-run/full runs stream identically; only a set Event changes behaviour.
- ⚠️ **The real trigger here was billing:** `run-37` shows `Your credit balance is too low to access the
  Anthropic API` — Claude calls (verdict, validation, diagnose-primary) 400 instantly. Top up credits (or
  switch Auto-Repair Model → Local LLM) or the diagnose will always fall to the local/back-up path.

### Recent progress (2026-08-25) — sharper failure text steers repair to ROOT CAUSE (not the label)

The TC-VPS-009 repair kept proposing a WRONG fix — relabeling a VPS transaction `type: 'LOAD'` → `'PURCHASE'`
(run-37/38) — because the failure text handed to the agent led with the surface symptom ("expected 'PURCHASE'
but the screen shows 'LOAD'"), which both misled Claude AND made RAG retrieve the ledger/label code instead of
the real bug (the `&& !issuedSmartCard` persistence guard in the RPS purchase path). Two fixes:

- **`_failure_text_for` (api/main.py) now leads with DESIGN INTENT + a root-cause steer.** It pulls the test
  case's `description` + `preconditions` + `expected_results_raw` from the DB and frames the failure as
  `EXPECTED BEHAVIOUR (design intent): … OBSERVED: … Failing assertions: … Fix the ROOT CAUSE … correct the code
  that PRODUCES or PERSISTS that state, NOT code that merely displays or labels it.` This sharpens the RAG query
  too — verified: retrieval now surfaces `createApprovedStatusFromCardNumber` (App.tsx L312-389, the buggy guard),
  which it previously missed. General win for every auto-repair (design intent + anti-symptom framing).
- **Anti-relabel rule in `_DIAGNOSE_PROMPT`:** "If the failure is a wrong/missing VALUE, LABEL or on-screen TEXT,
  do NOT make it pass by hardcoding or relabeling a string … Relabeling the displayed text (e.g. changing a
  transaction 'type' from one label to another) is almost never the correct fix." Safe for the other demo bugs
  (all root-cause single-token fixes).
- ⚠️ **"Patch find-text was not found in the target file" = a STALE RAG index** (built on a different branch than
  what's on disk — here the messy `repair/tc-vps-009-fix-*` branch that carried BOTH the guard bug and the earlier
  wrong relabel). The LLM proposes a `find` from indexed code that no longer matches disk → apply refuses. **Fix:
  check out the clean `demo/rps-vps-txn-bug`, REBUILD the RAG index, then run.** And this bug only truly PASSES
  after a fresh RPS purchase with the fixed code (check-only test) — and needs Anthropic credits for Claude to
  diagnose. RESTART the backend to load these changes.

### Recent progress (2026-08-25) — repair now retrieves the DESIGN SPEC (root-cause fix for the relabel loop)

The TC-VPS-009 repair kept mis-fixing (relabel `type:'LOAD'`→`'PURCHASE'`) even after the sharper failure text
got the right CODE (`createApprovedStatusFromCardNumber`) into context — because the **design document was never
in the retrieved context**, so Claude couldn't reason from the spec and hallucinated. Diagnosis:
- The design doc **is** indexed (`extract_docx_text` → 3 `design_document` chunks; one holds the "Payment and Card
  Reader Design" section with *"if the purchase succeeds, the card balance is reduced and a PURCHASE transaction is
  recorded"*). It IS retrievable via a `where={"type":"design_document"}` search.
- BUT `retrieve_context` did **code-first** retrieval + only `general_docs = search(rq, k=2)` unfiltered, and the
  design chunk ranked ~#5 → it never made the cut. Claude got code but no spec → guessed wrong.
- **Fix** (`retrieve_context`): a dedicated `design_docs = search(rq, k=_DESIGN_DOCS=2, where={"type":"design_document"})`
  pass, inserted **right after the code hits** (before the `_MAX_CONTEXT_BLOCKS=16` cap) so the spec is ALWAYS
  included; design chunks render with a `DESIGN SPEC (authoritative: the code MUST conform to this)` header so the
  LLM weighs them as ground truth. Verified for the TC-VPS-009 failure: context now contains BOTH the buggy
  `&& !issuedSmartCard` guard function AND the "PURCHASE transaction is recorded" spec sentence. This is what lets
  Claude reason "the spec requires recording a PURCHASE for an issued card → the guard that skips it is the bug"
  instead of relabeling. General win — every repair now sees the relevant spec (helps the card-sharing design bug too).
- This is a RETRIEVAL-logic fix (no re-index needed — the design chunks were already indexed); just **RESTART the
  backend**. Separately, the "find-text not found" error is still a STALE-index/branch-mismatch symptom — rebuild the
  index on the clean `demo/rps-vps-txn-bug` before running. (Optional further improvement: the design doc is only 3
  coarse chunks; finer paragraph/section chunking in `extract_docx_text` would sharpen design retrieval further.)

### Recent progress (2026-08-25) — the REAL root cause: snippet truncation hid the buggy line + coarse design chunks

The repair STILL mis-fixed even after design retrieval was added — three compounding retrieval bugs, now fixed:
- **Snippet truncation cut the buggy line out of the context (the decisive bug).** `retrieve_context` rendered
  each chunk as `page_content[:1800]`. The buggy `createApprovedStatusFromCardNumber` chunk is 2837 chars and its
  guard `&& !issuedSmartCard` sits at offset **1910** — PAST the cut. So Claude retrieved the right function but
  literally never saw the buggy line; it saw the opening + the "LOAD" symptom and guessed (relabel). Fix: caps
  raised to `_SNIPPET_PROMPT=4000` / `_SNIPPET_HIT=2500` so a whole function reaches the LLM. **This is why the
  fix kept being wrong and why apply hit "find-text not found"** (a guess targets code that isn't there).
- **Design doc was 3 giant 2200-char windows** → key sentences diluted below the retrieval cut. `extract_docx_text`
  now splits **by Word Heading style** (11 Heading1 sections) into ~12 focused `design_document` chunks (heading
  prepended + stored in `section` metadata, `chunk_size=1000`). Requires an index REBUILD (chunking change).
- **Symptom-side query ranked the CAUSE-side design section low.** The VPS-check failure query ranks "Payment and
  Card Reader Design" (the section with "if the purchase succeeds … a PURCHASE transaction is recorded") ~#4 among
  design chunks, so `_DESIGN_DOCS` was bumped 2→**5** to reliably include it.
- Verified end-to-end for the TC-VPS-009 failure: context now contains ALL of — the design spec sentence, the buggy
  `balanceAfter !== undefined && !issuedSmartCard` guard LINE, and the `type: 'PURCHASE'` (proving the label is
  already correct, so the bug is the guard, not a relabel). The guard line is unique on disk → a correct fix applies
  cleanly. **RESTART the backend** (truncation + `_DESIGN_DOCS` are runtime) and the index is rebuilt (216 chunks).
- **Branch note:** `demo/rps-vps-txn-bug` has NO auto-repair commits — just the bug (`51466cf`) + one manual
  `e8c1cb1 "fix smartcard mock load card number"` (makes the mock-card button issue the fixed demo card 0005322931).
  The "find-text not found" was the truncation bug above, NOT branch pollution. Strip `e8c1cb1` only if you want
  bug-only (it changes mock-card behaviour the demo may rely on).

### Recent progress (2026-08-25) — re-exploration flakiness: separator-insensitive screen-identity match

Symptom (`results/run-2-182823-2508`): after an **App re-exploration**, the simple TC-RPS-001 login test
suddenly FAILED at the very FIRST `verify` — `Wrong screen: expected 'login', got 'sign_in' [dom]` — even
though nothing about the login screen changed. Root cause was a **separator-sensitivity bug** in playwright
screen-identity, exposed by re-exploration reshuffling the exact string:
- The app_map keys the screen `login` with `dom_id: "signin"` (all its elements are `signin-*`). The live
  DOM normalizes the container to **`sign_in`** (underscore). The reverse-lookup cache
  (`_load_dom_to_screen_cache`) was keyed by the **raw** recorded `dom_id` (`"signin"`), so a live `sign_in`
  MISSED it, and `get_dom_screen_id()` returned `sign_in` unresolved. Then the **charted** verify path
  (`verify_current_screen`) did a **strict `==`** compare (`"sign_in" == "login"` → False → FAIL). (The
  *uncharted* path already tolerated this via `_screens_equivalent`; the charted path did not — an asymmetry.)
- **Fix** (`vision_agent/robot/playwright_stubs.py`): a new `_norm_sid()` collapses a screen id to its
  separator-insensitive lowercase core (`signin`/`sign_in`/`sign-in`/`Sign In` → `signin`). (a) The reverse
  cache is now keyed by the NORMALIZED dom_id **and** the normalized screen key (key→itself), and
  `get_dom_screen_id()` looks up by `_norm_sid(sid)` — so any separator style resolves to the canonical
  `login` for EVERY caller. (b) `verify_current_screen` now matches the live id against the normalized
  **expected key AND that screen's `dom_id`**, using **exact normalized equality (never substring)** so
  distinct screens can't collide — a defensive second layer immune even if the dom_id/cache is stale.
  Verified against the live app_map: `sign_in`/`signin`/`login` all → match `login`; `products` → no match.
- ⚠️ **Operational note (separate from the code bug):** the run failed on a build where the login bug was
  ABSENT — the kiosk app was checked out on a LEFTOVER `repair/tc-rps-001-fix-<8hex>` branch (an old
  auto-repair fix branch), NOT `demo/rps-login-bug`. That's also why re-exploration charted `products`
  (login succeeded). For the login-bug demo, **check out `demo/rps-login-bug`** first, then re-explore /
  rebuild the index. **RESTART the backend** to load this fix (the dom→screen cache is a module global,
  cleared on restart; uvicorn has no `--reload`).

### Recent progress (2026-08-25) — repair retrieval regression: two-lane (intent + action) code search

Symptom (`results/run-3-184426-2508`): after the screen-match fix above, TC-RPS-001 correctly FAILED on
the login bug (step 1 login-screen verify PASSES; the products-screen verify FAILS because the bug rejects
valid credentials and stays on sign-in) — but Auto-Repair's DIAGNOSE said *"Insufficient context: the
credential-authentication code … was not retrieved."* The `SignInScreen` chunk holding the buggy
`user.password !== ` `` `${password}-bug` `` line ranked **#11** in code retrieval — outside the retrieved set.
Root cause: the failure manifests as a **navigation symptom** ("did not reach the products screen"), and the
enriched failure text's **design-intent navigation vocabulary** ("lands on the products screen", "Select
Product and Quantity") — the SAME enrichment that VPS-009 needs to reach its persistence code — dominates the
embedding and pulls retrieval toward screen/config code (`developerSettings.ts`), burying the credential
check. A single blended query can't serve both failure shapes at once.

Fix (all in `repair_agent/repair_failed_test.py` + `api/main.py`, additive / degrades cleanly):
- **`_failure_text_for` (api/main.py) now appends "Steps attempted (in order): …"** — the ordered ACTIONS
  the test performed. This gives retrieval (and Claude) the ACTION vocabulary of the code path UNDER TEST
  ("Sign In", "add to cart", "check balance") instead of only the outcome symptom. Type VALUES are redacted
  so a typed **password never reaches the prompt/embedding** (an email identifier is kept — non-secret, adds
  signal); the failed step is marked `[FAILED HERE]`.
- **Two-lane interleaved code retrieval in `retrieve_context`:** Lane 1 (intent/symptom) = the full failure
  (unchanged — surfaces the code that PRODUCES the wrong outcome, e.g. a cross-kiosk persistence guard —
  VPS-009 path preserved). Lane 2 (action) = `_action_query()` built from summary + the Steps-attempted line
  + failing assertion (surfaces the code for the ACTION that failed — the credential check). The two lanes'
  hits are **interleaved round-robin, deduped, capped at `top_k`** so neither vocabulary buries the other,
  and DESIGN/general docs still fit the `_MAX_CONTEXT_BLOCKS=16` budget (design still = 5). `top_k` 6→8 for a
  small margin. When a failure has no Steps line the action query collapses to the intent query → identical
  single-lane behaviour (no regression).
- Verified end-to-end on the run-3 failure: the `SignInScreen` auth chunk (with `-bug`) is now included at
  block 5, design chunks still 5, the failure text does not leak the password. VPS-009's intent lane is
  unchanged (its index lives on `demo/rps-vps-txn-bug`, not testable on the login-bug index, so the change was
  kept a strict superset of the intent path it relies on). **RESTART the backend** to load these runtime
  changes (retrieval-logic only — no re-index needed).
- Note: retrieved CODE legitimately contains the app's own demo password (it's in the source) — that is the
  code the repair must see and is NOT a failure-text leak; only the failure DESCRIPTION is redacted.

### Recent progress (2026-08-25) — VPS-009 "wrong fix" was a STALE INDEX, not the two-lane retrieval

Symptom (`results/run-6-195052-2508`): after the two-lane retrieval change, TC-VPS-009 on
`demo/rps-vps-txn-bug` still got a WRONG fix even though the design-doc spec ("…a PURCHASE transaction
is recorded") WAS retrieved. It looked like a retrieval regression; it was not.

Root cause — the persisted RAG index was STALE:
- The live `docs/chroma_code_db` `langchain` collection held the **FIXED** `createApprovedStatusFromCardNumber`
  (guard `if (balanceAfter !== undefined)`), while disk had the **buggy** `&& !issuedSmartCard` guard
  (`App.tsx:361`). The last successful rebuild had indexed fixed code (the branch has fix/revert churn,
  e.g. `c9ac86c "Reverted the auto-repair change"`); the buggy disk was never re-indexed. So Claude never
  saw the buggy line and guessed. Proven: the tree-sitter chunker DOES capture `&& !issuedSmartCard` from
  current disk (1 chunk, 2837 chars), and a fresh temp-dir rebuild → `retrieve_context` puts that buggy
  chunk at **block 2** with the design spec present. The two-lane change was exonerated (it retrieved the
  right FUNCTION; only the persisted CONTENT was stale). **User rebuilt the index → TC-VPS-009 fixed correctly.**
- Diagnostic tell: `search(...)` returns code whose lines don't match disk (here the guard line differed);
  the persist dir had accumulated **7 orphan collection-UUID dirs** from repeated in-place rebuilds.
- **Hardening** (`repair_agent/parse_code_and_store.py`): `build_codebase_index` now clears chromadb's
  per-process client cache (`SharedSystemClient.clear_system_cache()`, best-effort across versions) after a
  rebuild, so a SEARCH in the same running-backend process reopens the collection and reads the freshly
  written chunks instead of a cached handle to the pre-rebuild collection UUID (a known way an in-running
  "Rebuild index" could appear to succeed yet keep serving the OLD index). Verified: rebuild → in-process
  search reads fresh; chunk count unchanged (221); no build regression.
- **Rule reinforced:** after checking out / reverting on a demo bug branch, **REBUILD the RAG index against
  the on-disk code before running the repair** — the index reflects code AT BUILD TIME, and a rebuild done
  while the branch was transiently fixed leaves the buggy line unindexed. HNSW ranking varies slightly build
  to build; the buggy chunk lands at block ~2 with `top_k=8`, comfortably included.

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

---

## Conventions

- Add config via `vision_agent/config.py` `Settings` (never read `os.environ` directly).
- New robot capability → add the function to **all three** backends (`stubs.py`,
  `playwright_stubs.py`, `real_robot.py`) with an **identical signature**; agent code stays backend-agnostic.
- New API route → add to `api/main.py`; new persisted field → add to `api/models.py` and an
  idempotent migration in `api/database.py`.
- New app screen → no code change needed; re-run the App Explorer (Claude vision discovers elements).
- Windows-centric repo; UTF-8 stdout reconfiguration is intentional.
