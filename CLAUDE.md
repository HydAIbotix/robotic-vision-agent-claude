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

Health `GET /health` · Runs `GET/POST /runs`, `GET /runs/{id}`, `WS /runs/{id}/ws`,
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

## Work done in the recent session (reconstructed from git, commits `73cc052`…`6079702`, 2026-07-03 → 07-07)

> These are reconstructed from git history and current code, not a saved chat transcript.

**Themes:** "app explorer changes" → "Enhancements" → "Robot API changes" → "Fixes" → "Fixes".

- **Dual-controller Robot API** (`Robot API changes`, `9c200e8`): the AGV mobile base and the arm can
  now live on **separate controllers/IPs**. `config.py` gained `arm_url` / `agv_url` (each falls back
  to `robot_ip:robot_port`; `/api/v1` auto-appended via `arm_api_base()`/`agv_api_base()`).
  `real_robot.py` routes each call with `_base_for(endpoint)` (`/base/*` → AGV, everything else →
  arm), `setup()` posts to both controllers, and `health_check` probes both (`arm_url`/`agv_url`,
  with a `robot_url` back-compat alias).
- **`tap()` reuses the post-tap camera frame** it gets back from `/screen/click` (saves a separate
  `/capture` arm cycle) and waits 0.8 s for the kiosk to settle before the next capture.
- **Multi-app app_map + explorer robustness** (`app_map/store.py`, `app_explorer/nodes/explore_screen.py`,
  `identify_result.py`, `run_explorer.py`): merge/remove per `app_id`; `app_map.json` untracked.
- **Enhancements** (`6d32d0f`): `api/database.py` + `models.py` schema additions, `run_vision_step.py`,
  `playwright_stubs.py`, `config.py`, `robot/__init__.py` dynamic dispatch.

**Files modified across the session:** `api/main.py`, `api/database.py`, `api/models.py`,
`app_explorer/nodes/explore_screen.py`, `app_explorer/nodes/identify_result.py`, `app_map/store.py`,
`run_explorer.py`, `test_runner/nodes/run_vision_step.py`, `vision_agent/config.py`,
`vision_agent/nodes/analyze.py`, `vision_agent/robot/real_robot.py`, `vision_agent/robot/__init__.py`,
`vision_agent/robot/playwright_stubs.py`, `.gitignore` (+ `app_map.json` removed from tracking).

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

## Active & Past Issues

Debugging history from the build session ("Robotics vision sub-agent with LangGraph", 2026-06-25 →
07-01) plus follow-ups. Read this before touching coordinate math, the robot backends, the explorer,
or the Tier-3 path — most of these are subtle and have bitten us already.
Legend: ✅ fixed · ⚠️ fixed but **not verified live / fragile** · 🔲 still open.

### Coordinate & vision accuracy (the fragile foundation — most bugs trace here)

- ✅ **Double-multiplied coordinates (taps millions of px off-screen).** `analyze_screen` already
  converted normalized→pixels before storing in `app_map`, then `execute_action._get_pixel_center`
  multiplied by the viewport *again* (`981 × 1400 = 1,373,400`). Fix: `_get_pixel_center` returns the
  stored pixel center directly; deleted the stale `_VIEWPORT_W/H` constants.
- ✅ **Vision returns MIXED coordinate scales in one response** — some elements normalized `[0,1]`,
  others raw pixels, in the same reply; `_norm_to_px` blew up the pixel ones ~1400×. Fix: made
  `_norm_to_px` **per-value scale-aware** (single source in `analyze.py`, imported by
  `explore_screen.py`), and made `_dom_correct_elements` DOM-authoritative (a text match snaps to the
  DOM's exact coordinate regardless of distance; each DOM element consumed once). ⚠️ Root-cause fix
  was unit-tested on the captured broken data but **never confirmed in a live exploration** — old
  maps have bad coords baked in and must be cleared + re-explored.
- ✅ **DOM snap to the wrong element** (Sign In landed on the password field / a dev-settings link).
  Claude placed the button ~40px too high; the nearest-element snap ignored element type. Fix: `tap()`
  tries `document.elementFromPoint` first, then searches within an 80px radius sorting
  `<button>` → `<input>` → distance.
- ✅ **Tier-3 screenshot captured mid-transition** → false "screen did not transition" → retry on the
  wrong screen → FAIL. Login API is async (~300–700ms) but `tap()` waited only 300ms. Fix: `tap()`
  now `wait_for_load_state("domcontentloaded", timeout=1200)` after click. ⚠️ **Must be mirrored into
  `real_robot.py`** (real backend uses a fixed `time.sleep(0.8)` — see the robot-parity note below).
- 🔲 **Coordinate accuracy from vision is mitigated, not solved.** The DOM snap, button-preference
  sort, and scale-tolerant converter all compensate for Claude returning imprecise/mixed coordinates.
  A mid-session regression (elements shifted, e.g. `email_input` x=462 vs expected x=981) was never
  fully root-caused — attributed to a changed/left-aligned kiosk layout or DPI/viewport difference.

### Robot interaction (Playwright & keyboard)

- ✅ **Login never submitted** — `tap()` used JS `element.click()`, which fires only `click`, not
  `mousedown→focus→mouseup`, so React's `onFocus` never opened the on-screen keyboard and the
  controlled `inputMode="none"` field never updated. Fix: click via `page.mouse.click(cx,cy)` (real
  event chain); type via `page.keyboard.type(text, delay=30)` so `onChange` fires per char.
- ✅ **Virtual-keyboard typing errors.** Three causes: (a) the on-screen keyboard is *one-shot* but
  `_click_key` tapped shift **before and after** an uppercase letter → re-enabled caps
  ("Password123" → "PASSWORD123"); (b) space looked up as `' '` but stored as `"space"`; (c) the
  "Done" key coordinate sometimes hit the adjacent `-` key. Fix: remove the second shift tap; map
  `" " → "space"`; dismiss via `[data-testid="keyboard-done"]` before falling back to coordinates.
- ✅ **Keyboard mapping tapped a display label** — `_map_keyboard`'s type filter included `"text"`,
  which matched `header_title` instead of an input. Fix: drop `"text"` from the filter.
- ✅ **`Mouse.wheel()` signature crash** — called with 4 args; Playwright takes `wheel(dx, dy)`. Fix:
  `mouse.move(x,y)` then `mouse.wheel(0, delta_y)`.
- ✅ **Session persisted across resets** — `reset_to_entry()` navigated to root but the SPA restored
  its session from `localStorage` and auto-redirected to products. Fix: clear `localStorage` +
  `sessionStorage` before navigating.

### App Explorer correctness

- ✅ **Excessive logout/login; `sign_out` explored from every page** — `execute_action` called
  `reset_to_entry()` (full login replay) before *every* action. Fix: skip reset when the current DOM
  screen matches the action's source; use `navigate_to_screen()` (one sidebar click) between
  authenticated screens; full-reset only when a valid login is genuinely required
  (`_requires_valid_login`). Global elements deduped via `app_map["element_transitions"]`. Removed
  kiosk-specific hardcoding (`_DOM_SCREENS`, `_NAV_LABELS`, `_AUTHENTICATED_SCREEN_IDS`).
- ✅ **SPA nav screens collapsed to one perceptual hash** — all views share URL `localhost:5173` and
  the shared sidebar dominates the 16×16 aHash, so distinct screens hashed identically. Fix:
  `get_dom_screen_id()` reads each screen's unique `data-testid="*-screen"` as the primary oracle in
  `identify_result.py`, before hash lookup.
- ✅ **Skeleton/"unknown" screen entries; sign-up misidentified as login** — `identify_result` created
  a skeleton entry, so `explore_screen`'s `screen_id not in screens` check skipped analysis. Fix:
  gate on `if not existing.get("elements")`; when `screen_id == "unknown"` use
  `last_result_screen_id`; prefer `last_result_screen_id` over `_analyze_fresh` (which returns "login"
  for the look-alike sign-up form).
- ✅ **Commerce walkthrough never reached cart/payment/success.** Three compounding bugs in
  `run_explorer.py`: (a) looked up login by `entry_screen="signin"` but the screen was keyed
  `"sign_in"`; (b) tapped "Add to Cart" with quantity 0 (never incremented); (c) searched increment
  by id-keywords `"increment"/"plus"` but the real element was `nexora_increase` (type `stepper`,
  label `+`). Fix: find the login screen **by content** (email + sign-in elements), increment before
  add-to-cart, and let Opus pick elements by goal rather than hardcoded ids; DOM-verify each nav.

### Test Runner / Tier-3

- ✅ **`(0,0)` tap treated as success → false PASS, Tier-3 never fired.** Screens missing from the
  app_map at plan time got `(0,0)` coords; the executor counted that as success so `outcome` never
  became `failed`. Fix: in `run_vision_step.py`, treat a `(0,0)` tap as an explicit failure to trip
  the Tier-3 vision fallback. (Real cure is exploration reaching those screens.)
- ✅ **Hardcoded credentials / login loop.** Tier-3 looped logging in with `user@example.com` — a
  literal example in the `PLAN_STEPS` prompt (`vision_agent/prompts.py`), and intake credentials were
  never threaded in. Fix: removed the example, added a "use only provided credentials" rule, thread
  intake `credentials` in via `cred_hint`, and resume from the current screen on failure instead of
  resetting to login. **Hard rule: never hardcode credentials anywhere.**
- ✅ **Bare `json.loads()` crashed the whole run** on one empty/transient LLM response
  (`Expecting value: line 1 column 1`). Fix: resilient `invoke_json()` in `vision_agent/llm.py`
  (retries, empty/multi-block handling, safe fallback), applied to plan/validate/explorer nodes — one
  bad response now degrades a single step, not the run.
- ⚠️ **Validation silently skipped + hallucinated verdict** — a wrong-total test PASSED and the report
  claimed it "verified $856" when the screen showed $756.67. Field-name mismatch: executor read
  `expected_text`, plan wrote `expected_value`, so the check was skipped and `conclusive_verdict`
  fabricated a match. Fix: read **both** keys; an empty-DOM verify now **auto-FAILS** (was
  auto-passing); failure message reports the actual on-screen value. Backend verified; **full browser
  re-verification was still pending** at the last checkpoint.

### Kiosk URL lifecycle & multi-app map (2026-07-08)

- ✅ **Test opened the WRONG kiosk's URL** (e.g. `TC-VPS-001` launched the kiosk-2/RPS app). Root
  cause was threefold: (a) a single global `settings.kiosk_url` drove every run's browser; (b) three
  un-joined `kiosk_id` namespaces (`KioskConfig`=`K-02`, `DeviceConfig`=`kiosk-1/2`, `TestCase`=`K-01`);
  (c) planning fed the whole (last-explored) app_map to Claude regardless of `app_id`. Fix: `kiosk_id`
  is now the single join key — `_infer_kiosk_id` resolves the test-id alias (`TC-VPS-001`→`VPS`) via
  the Device Map, `_execute_run` looks up `KioskConfig.url` for the run's kiosk and sets
  `settings.kiosk_url` (restored in `finally`), and single-kiosk runs scope the map via new
  `store.scoped_to_app`. ⚠️ Needs a live browser + Claude run to confirm end-to-end. See the
  `[[kiosk-id-join-key]]` invariant.
- ✅ **Exploration URL now remembered per kiosk.** `start_explore` upserts `KioskConfig.url` for its
  kiosk_id; `merge_explored_app` also stamps `apps[app_id].url` (shown in the App Explorer UI). The
  whole lifecycle (planning/execution/results) reuses it — no re-entering the URL per run.
- ✅ **Exploring a 2nd kiosk wiped the 1st app map.** `finalize_map`/`store.save` overwrote
  `app_map.json` mid-run BEFORE `run_explorer.py` read the "existing" map to merge, so the merge saw
  an already-clobbered file. Fix: `run_explorer.py` snapshots the existing map into `_existing_map`
  BEFORE `explorer.invoke()` and merges into that snapshot. Per-app clear (`DELETE /app-map/{app_id}`)
  is unchanged. **User must re-explore each kiosk once** so URLs persist and screens re-tag.
- ✅ **Execution UI now separates the plan by kiosk.** `Execution.tsx` groups both the selected
  test-case list and the execution-plan preview under a per-kiosk header (`groupByKiosk`), so the
  operator sees at a glance which kiosk each test/plan runs on. App Explorer shows each app's
  remembered URL and pre-fills it when a known Kiosk ID is re-entered.

  **Required user re-steps for these fixes to take effect:** (1) re-explore each kiosk using the
  SAME Kiosk ID that appears in Configuration → Device Map (so URL persists + screens re-tag);
  (2) confirm each Device Map alias (e.g. `VPS`, `RPS`) maps to that kiosk_id. Re-uploading the test
  Excel is optional — execution re-resolves each test's kiosk via the Device Map at run time.
- ✅ **Clear-ALL-apps left previous kiosk's screenshots behind.** `_delete_exploration_shots(None)`
  only deleted files matching a prefix allowlist (`explore_/aria_/scroll_/walkthrough_`), so
  `keyboard_map_*.png`, `calibration.png`, `health_capture.png` survived and showed up on the App Map
  after re-exploring another kiosk. Fix: global clear now nukes EVERY top-level image file + the whole
  `annotated/` folder (execution shots live in per-run subfolders and are untouched — verified by test).
  Per-app clear stays surgical (only the cleared app's screens' shots; other apps preserved).
- ✅ **Explorer now captures & reuses app-generated identifiers.** The explorer couldn't reach
  stateful management flows (add money / check balance of a *just-issued* card) because it had no way
  to carry the generated card number across screens. Fix: `SUGGEST_EXPLORABLE_ACTIONS` now (a) mandates
  exploring account/card/entity **management** actions (top-up, balance, view/manage existing, history),
  and (b) captures identifiers the app displays into `captured_values`, reusable on later screens via
  `{{captured.NAME}}`. New `ExplorerState.captured_values` accumulates them in `explore_screen`;
  `execute_action._resolve` substitutes them (like credential placeholders). Discover during
  exploration, not deferred to test-time. ⚠️ Still not working live — the explorer did not reuse the
  issued card number for check-balance/add-money. **De-prioritised** by the user: test execution is
  expected to navigate those flows via on-screen elements. Left in place; revisit if test-time
  navigation proves insufficient.

### Test-plan generation scoped per kiosk (2026-07-08)

- ✅ **Generated plan referenced the WRONG app's screens** (`TC-VPS-001`'s plan had `login` + email/
  password steps, but VPS has no login — those are RPS's screens). Root cause: BOTH plan-generation
  paths fed Claude the **whole combined app_map** (VPS + RPS) with no per-kiosk filter, and the
  `_TC_PLAN_PROMPT` JSON example was itself a login flow (anchoring Claude to invent login steps).
  Fix (not a patch — one choke point + defense-in-depth prompt):
  - New `_scope_map_for_test(db, app_map, test_id, steps_raw)` resolves the test's kiosk (via the
    alias-aware `_infer_kiosk_id`) and returns `store.scoped_to_app(...)` — the map filtered to that
    ONE kiosk's screens. It warns loudly if a multi-app map can't be scoped (Device Map alias↔kiosk_id
    mismatch), so silent wrong-app plans can't recur.
  - `POST /tc-plan` (Test Intake) now scopes before building the cache key + element inventory, so
    Claude only ever sees the target app. `_execute_run` STAMPS each test's resolved `kiosk_id`;
    `parse_steps` scopes the map to `tc["kiosk_id"]` at the single planning choke point — correct for
    every tier and even mixed multi-kiosk runs. Both paths now compute `version_hash` on the SAME
    scoped map, so a UI-generated plan is correctly reused at execution (also fixed a latent
    cache-key mismatch between UI and runner).
  - `_TC_PLAN_PROMPT` hardened: "the app map is the COMPLETE and ONLY source of truth — never invent a
    screen/element/flow not in it; if there's no login screen, add no login steps; emit
    `vision_required` for genuinely-absent elements." The login-flavored example was replaced with a
    neutral FORMAT-ONLY skeleton; email/password `required_config` is now conditional on the map
    actually having a login screen. Verified with an in-memory DB: a VPS test scopes to VPS screens
    only (no `login`), an RPS test to RPS screens only. Stale `TC-VPS-001` cached plan deleted.
    **User: regenerate the plan** (Test Intake → force) so the frontend localStorage copy refreshes.

### Structured-plan `type` step now focuses the field (2026-07-08)

- ✅ **Type steps didn't enter the value — the whole page got "select-all"ed instead** (`TC-VPS-001`:
  amount `300` never entered; every field highlighted; `Use Mock Card` then loaded nothing; verify
  correctly FAILED with "$300.00 not present"). Root cause: in `run_vision_step._execute_structured_plan`
  the `type` handler called `robot.type_text(value, clear_first=True)` **without first focusing the
  target field**. `type_text` types into whatever is focused and its `clear_first` sends Ctrl+A — with
  nothing focused, Ctrl+A selected the whole PAGE and the characters went nowhere. This was an
  **app-shaped** bug, not app-specific code: login-style plans (RPS) emit a *separate tap step before*
  the type, so the field was already focused; a standalone type step (VPS "enter amount") carries its
  own `px/py` and has no preceding tap, so nothing was focused. Fix: the `type` handler now taps the
  field's `px/py` to focus it **when the step carries coordinates**, then types (mirrors the App
  Explorer's `execute_action._run_steps`). Type steps without coords still rely on the prior tap, so
  login flows are unchanged. Generic for every app. The `tap()` exact-point branch returns the input
  directly, so focusing never snaps to the nearby button. The plan for `TC-VPS-001` was already
  correct — only execution was broken — so the cached plan was kept.
- ✅ **Tier logging is now explicit end-to-end** (developer can see where a run routes/fails):
  planning logs `TIER-1 (plan cache): HIT/MISS/STALE → moving to TIER-2`, `TIER-2: SUCCESS/FAILED →
  TIER-3`, `TIER-3 (legacy vision)`; execution logs `TIER-1/2 EXECUTION … (0 LLM calls)`, and on a
  failure `TIER-1/2 EXECUTION: FAILED at step N (method=…) — reason: <observation> → handing off to
  TIER-3 (vision), resuming from current screen`. The validation pipeline already reports accurate
  reasons ("expected 'X' … but it is not present / shows 'Y'"); the earlier "wrong validation" was a
  downstream symptom of the unfocused-type bug, resolved by the fix. **User: re-run `TC-VPS-001`** to
  confirm the amount is entered and the balance verifies (needs the live browser + Claude API).
  ✅ **Confirmed live: `TC-VPS-001` PASSED** after this fix.

### Studio crashed on a malformed plan `required_config` (2026-07-08)

- ✅ **Test Intake page white-screened during plan generation for `TC-RPS-001`** — `Uncaught
  TypeError: Cannot read properties of undefined (reading 'toLowerCase')` at `TestIntake.tsx:467`
  (`placeholder={`Enter ${f.label.toLowerCase()}`}`). Root cause: Claude's generated plan included a
  `required_config` entry that omitted `label` (the `_TC_PLAN_PROMPT` schema lists key/label/type but
  the model doesn't always emit all three), and the UI dereferenced `f.label` unguarded. Fixed at BOTH
  layers so neither a bad plan nor a bad render can recur (generic, not RPS-specific):
  - **Frontend (`kiosk-test-studio/TestIntake.tsx`)** — `credFields` now drops entries with no `key`
    and falls back to `key` for a missing `label`, so the page can never crash on plan data.
  - **Backend (`api/main.py` `POST /tc-plan`)** — normalises `required_config` before caching/returning:
    drops non-dict/keyless entries, defaults `label` from the key (`existing_card_number` →
    "Existing Card Number") and `type` to `text`. Unit-tested: missing-label defaulted, keyless/garbage
    dropped. Root-cause fix so malformed config never reaches the UI or the plan cache.
  - **User: regenerate the `TC-RPS-001` plan** (Test Intake) so the cleaned config replaces the cached one.

### Cross-kiosk E2E execution (load at VPS → buy at RPS → verify at VPS) (2026-07-08)

`TC-E2E-001` exercised a flow spanning BOTH kiosks. Four distinct defects, all now fixed generically
(no VPS/RPS-specific code); applies to every execution mode (playwright + real robot):

- ✅ **Whole RPS half collapsed into one un-plannable `vision_required` step.** Root cause: the
  per-kiosk plan scoping added earlier resolved the E2E test to a SINGLE kiosk and hid the other
  kiosk's screens, so Claude had nothing to plan the RPS flow from. Fix: scoping is now KIOSK-SET
  aware — new `_infer_kiosk_ids` returns every kiosk a test references (ordered by first mention in
  the steps), and `store.scoped_to_apps` filters the map to the UNION of those apps. A single-kiosk
  test still scopes tight (VPS never sees RPS's login); a cross-kiosk E2E sees BOTH apps. Wired through
  `/tc-plan`, `_execute_run` (stamps `tc["kiosk_ids"]`), and `parse_steps` (scopes to `tc["kiosk_ids"]`),
  so the cache key matches UI↔runner. `_TC_PLAN_PROMPT` + `PLAN_FROM_MAP` gained a CROSS-KIOSK section
  ("plan the whole journey; tag each step's `device`; the runtime switches apps from the tag"), and the
  shared `element_inventory_for_prompt` now tags each SCREEN with its `app/kiosk` in multi-app maps.
- ✅ **`$4000` never entered — the `type` step had no `value`.** Claude put the amount only in the
  description. Fix: both planner prompts now REQUIRE a non-empty `value` on every type step (and px/py
  so the field self-focuses). The executor also fails a type step loudly if the value is empty/unresolved.
- ✅ **"enter the SAME card number issued at VPS" was impossible** — a value generated at runtime can't
  be hard-coded at plan time. Added a generic capture mechanism: a `capture` plan action reads the
  displaying element's live text into a named var; later `type` steps reference it as
  `{{captured.NAME}}`. `run_vision_step` handles `capture` (playwright DOM read via testid/point;
  best-effort elsewhere) and substitutes `{{captured.*}}` in type values; an unresolved placeholder
  fails the step → Tier-3. Prompts document it as the ONE allowed placeholder exception.
- ✅ **Flow aborted silently at the Tier-3/resume hop (per the defect-intelligence report).** The
  cross-kiosk device switch (`move_to_position` + browser `navigate_to_url`) and the tap/type robot
  calls are now wrapped so ANY failure (real-robot API timeout, nav error) is recorded as a failed
  step and follows the normal failure path (Tier-3 → fail) instead of crashing the suite. Added
  explicit `[CROSS-KIOSK] switch device X → Y (kiosk Z)` logging and a loud warning when a device has
  no configured URL (so the second app can't load).

**Robot-mode parity + configurable response timeout (applies to all fixes above):**
- ✅ New `settings.robot_response_timeout_s` (**default 2.0s**) — every real-robot REST call
  (`_post`/`_get`/`capture`) uses it as the HTTP response timeout, so a hung/slow robot fails fast.
  A timeout raises → the runner's try/except fails the step gracefully via the existing path. The
  physical-action wait (`base_move_timeout_s`/`arm_move_timeout_s`) is unchanged and separate.
- ✅ **`real_robot.move_to_position(x,y,θ)` implemented** (was missing → cross-kiosk moves were a
  silent no-op on the real backend). It drives the base via `/base/goto` with the non-blocking
  POST→poll pattern, so real-robot cross-kiosk hops invoke the robot API. Playwright/demo keep it a
  no-op (URL switch handles the app change) — identical I/O across backends.
- Execution plan preview already groups steps by `device`; with device tags now emitted per step, an
  E2E plan renders as separate VPS / RPS groups (plan shown separated by kiosk).
- **User: regenerate the `TC-E2E-001` plan** (stale one deleted) and re-run to confirm end-to-end
  (needs live browser + Claude API; the card-number capture depends on the VPS "card issued" element
  being present in the app map — if absent, that portion degrades to Tier-3 vision).

### AGV / mobile-base test steps + self-explanatory command ids (2026-07-09)

Raw test cases now drive the AGV (mobile base), not just the arm/touchscreen. Example steps:
"Move the AGV to VPS → check the AGV state → wait 30s → move back home".

- ✅ **Device identity is content-driven, not name-driven.** Per-step device targeting comes from the
  STEP TEXT: Claude tags each step's `target`/`device` by reading the step (e.g. "Move AGV to VPS" →
  `target: VPS`). The test-id is only a *fallback* input to `_infer_kiosk_ids` (which decides which app
  maps to load for planning); it never overrides what the steps say. At runtime the device ALIAS is
  resolved to its **kiosk_id from the Device Map** and that kiosk_id is the target passed to the robot
  API (`VPS` → `kiosk-1` → `/base/goto {target: "kiosk-1"}`; the request field is `target`, not
  `kiosk_id` — see the 2026-07-13 hardware-fix section below).
- ✅ **New plan actions for base ops** (both `_TC_PLAN_PROMPT` and `PLAN_FROM_MAP` + the executor):
  `move` (AGV goto a device alias or reserved `home`), `check_state` (assert `/base/state` or
  `/arm/state`, optional `expected_state`), `wait` (bounded sleep). These carry NO screen_id/px/py and
  produce NO screenshot. `_execute_structured_plan` handles them; the cross-kiosk auto-switch is
  skipped for these actions so a `move` never double-drives the base. `is_valid` already ignores
  non-`tap` steps, so an AGV-only plan is cache-valid, and Tier-1 cache load now works even with no
  app_map (pure base tests). `_PLANNER_VERSION` bumped → old plans re-generate.
- ✅ **Self-explanatory command ids.** `real_robot._new_cmd_id(label)` now emits `cmd-<n>-<label>`
  with a per-test counter (`reset_command_seq()` at each test start): `cmd-1-goto-VPS`,
  `cmd-2-arm-click`, `cmd-3-arm-type`, `cmd-4-base-state`, `cmd-5-arm-abort`, `card-tap-<kiosk>`, …
  A user reading the log/monitor can tell exactly what each command did and in what order.
- ✅ **Robot-API telemetry in the live monitor.** Every real-robot REST call is recorded (endpoint,
  cmd_id, HTTP status, latency, request/response time) and `_record` prints a `[ROBOT API] …` line;
  the runner flushes new events after each step to the live monitor as `log` events
  (`[ROBOT API] POST /base/goto (cmd-1-goto-VPS) → 202 in 34ms`). No-op for playwright/demo.
- ✅ **Empty screenshots handled gracefully.** AGV steps set `screenshot_after: ""`; `StepShots.tsx`
  already renders nothing for an empty path, and `_cap` returns `""` on any capture failure — no
  broken images, no crash. All backends: real fires the base/state APIs; playwright/demo simulate an
  idle base (no-op fallbacks added to `stubs.py`) so the same plan runs (validates structure) in dev.

### FIRST real-AGV run: `/base/goto` needs `target`, not `kiosk_id` (`TC-AGV-001`, 2026-07-13)

The first execution of the `real` backend against physical hardware. `TC-AGV-001` ("Move the AGV to
Kiosk-1") FAILED at step 1 with **HTTP 422 Unprocessable Entity** from
`POST http://<agv>:8000/api/v1/base/goto`. Root cause: the real AGV controller drives to **named
positions** from its pre-built map (`kiosk-1`, `kiosk-2`, `home`) and its `/base/goto` schema
requires a **`target`** field holding that name — but our client sent `{"kiosk_id": …}`, so the
required `target` was missing → 422. (The alias→kiosk_id resolution in the runner was already
correct: `VPS`→`kiosk-1` via the Device Map; only the request field name was wrong.) The follow-on
`/capture` connect-timeout in the same log is a *downstream* symptom — after the move step failed the
run tried to screenshot the arm camera at a different IP; fixing the goto removes the trigger.

- ✅ **`/base/goto` now sends `target`** (`vision_agent/robot/real_robot.py`), in BOTH callers:
  - `navigate_to_kiosk(kiosk_id)` → `{"target": kiosk_id, "cmd_id": …}` (the AGV `move` action path;
    `"home"` passes verbatim as the target).
  - `move_to_position(x, y, θ, target=None)` → when `target` is given (the cross-kiosk destination
    kiosk_id), sends `{"target": target, "cmd_id": …}`; with no target it FALLS BACK to the raw pose
    `{"x", "y", "theta", "cmd_id"}` for a controller that navigates by coordinates. Signature gained
    the optional `target` kwarg; `stubs.py` mirrors it (simulated no-op) for backend parity, and the
    runner's cross-kiosk switch now passes `target=target_kid` (x/y/theta ride along only as fallback).
  We send exactly `target` (+ `cmd_id` for poll correlation) — `kiosk_id` is REPLACED, not added, so a
  strict schema can't 422 on a stray field. The Device-Map `pos_x/pos_y/pos_theta` config is retained
  purely as that raw-pose fallback (the robotics team's named map is the primary path).
- **Scope of the fix — only the navigation API changed.** The other real-robot APIs that carry
  `kiosk_id` (`/screen/click` in `tap`/`type_text`/`swipe`, `/card/tap`) identify *which kiosk's
  touchscreen the arm is servicing* — that is genuinely a `kiosk_id`, not a drive-to `target`, and
  none of them returned 422. Left unchanged; revisit only if a hardware run shows an arm/card API also
  rejecting `kiosk_id`. `/base/abort`, `/base/state`, `/base/pose` carry no target (cmd_id / GET only).
- **Backend parity + no regression:** target-driven for real; simulated no-op for playwright/demo (the
  browser URL switch still handles cross-kiosk app changes there). Unit-tested (11/11) that
  `navigate_to_kiosk`/`move_to_position` emit `target` (and the pose fallback when no target); the
  prior suites still pass (`plan_normalize` 34, `inline_fast` 16, `exec_integration` 11, `intent_rescue`
  12). **Unverified beyond the 422 fix** — the user must re-run `TC-AGV-001` on the real AGV; the next
  thing to watch is the poll loop (`/base/state` must echo `state:"idle"` and the same `cmd_id`) and
  then `/capture` reachability from the arm controller IP. **→ this prediction was borne out: see the
  next section — the base reports `ready` (not `idle`) and doesn't echo our `cmd_id`.**

### Real-AGV run #2: base reports `ready` (not `idle`), + live status tracking (`TC-AGV-001`, 2026-07-13)

With the `target` fix in, the AGV **physically moved home → kiosk-1**, but the run still FAILED:
`TimeoutError: Robot /base/state timed out after 60.0s (last state: 'ready')`, and there was NO log
for ~2 minutes while it drove (silent wait), then a downstream `/capture` connect-timeout. Three
distinct issues — all fixed in `real_robot.py` + `run_vision_step.py`, unit-tested (17/17), applied
consistently across backends. NB: the user's first hypothesis was a wrong URL (arm vs AGV), but the
log is decisive that it was NOT — `/base/state` returned `'ready'` for 60s, so it *was* reaching the
AGV (had it hit the unreachable arm at `.105` it would have raised a ConnectTimeout on the first poll,
exactly like `/capture` did). Root cause was the arrival contract, not routing.

- ✅ **The base signals arrival with `state:"ready"`, not the arm's `"idle"` — and doesn't echo our
  `cmd_id`.** The old `_poll` waited for `state=="idle" AND cmd_id==ours`, so a `ready` base never
  satisfied it → 60s timeout even though it had arrived. New dedicated **`_poll_base(cmd_id,
  timeout_s, initial_state)`**: arrival = any of `_BASE_READY_STATES` = {`ready`, `idle`, `arrived`,
  `done`, `reached`}; `state:"error"` fails fast; the **`state` field is authoritative** (no `cmd_id`
  gate, since the AGV's `/base/state` returns a placeholder cmd_id). The fast arm `_poll` (sub-second
  taps, real `idle` + real `cmd_id` echo) is unchanged. `check_state` also treats base `ready`≡`idle`
  (a test asserting the AGV is "idle" is satisfied by "ready"/"arrived"), so a "wait till the AGV is
  idle at the kiosk" step passes on `ready`. Once `ready`, the next step runs IMMEDIATELY (no implicit
  wait) unless the test explicitly asks to wait — a `wait` step handles the explicit case.
- ✅ **Real-time AGV status tracker (no more silent 2-minute wait).** `/base/goto` returns an
  immediate `{"state":"moving"}` — used for the first status tick — then `_poll_base` polls
  `/base/state` every **`settings.base_poll_interval_s` (2.0s, configurable)** and pushes a live tick
  each interval showing `state` + `nav_feedback.distance_remaining` + `nav2_state`
  (`[ROBOT AGV] 13:15:57.xxx /base/state … — state='moving', 0.68m remaining, nav2=ACTIVE`). Delivery
  is a **push sink**: the runner registers `robot.set_event_sink(cb)` and the sink emits `log` events
  to the live monitor DURING the blocking move (the old pull-based `_flush_robot_events` only ran
  *between* steps, so nothing showed while the base drove). Status ticks are **live-only** (pushed,
  not stored in the `_events` ring-buffer) so the post-step pull-flush never double-emits them; HTTP
  call records still flow through `_events`/`get_events` as before. `stubs.py` gets a no-op
  `set_event_sink` for parity.
- ✅ **User-friendly timestamps + visible controller routing in telemetry** (issue #2). Every
  `[ROBOT API]`/`[ROBOT AGV]` line and event now carries `request_time`/`response_time` as local
  **`hh:mm:ss.mmm`** (e.g. `13:15:56.925 → 13:15:57.032`) alongside the raw epochs, plus a
  **`controller`** field = the host that served the call (`192.168.0.101:8000` for AGV vs
  `192.168.0.105:8000` for arm). This makes the AGV-vs-arm URL routing directly verifiable from the
  monitor — addressing the user's URL concern by *showing* which controller each call hit. Routing
  itself is unchanged and already strict (`_base_for`: `/base/*` → `agv_api_base()`, everything else →
  `arm_api_base()`); a new `_get_quiet` is used inside the poll so 30+ raw `GET /base/state` lines
  don't spam the monitor (one consolidated status tick per interval instead).
- **Config:** new `settings.base_poll_interval_s` (2.0s) — how often to poll the base while it drives;
  separate from the fast `robot_poll_interval_s` (0.5s) used for arm taps and from
  `robot_response_timeout_s` (per-HTTP-call timeout) and `base_move_timeout_s` (overall move deadline,
  60s).
- **Backend parity + no regression:** real drives/polls the physical base; playwright/demo simulate an
  idle base and no-op the sink. Unit-tested (17/17): goto+state route strictly to the AGV URL, poll
  returns on `ready` (no timeout), live ticks carry state/distance/hh:mm:ss.mmm/controller, error
  fails fast, and STATUS ticks stay out of the ring-buffer (no double-emit). Prior suites unchanged
  (`base_goto_target` 11, `plan_normalize` 34, `inline_fast` 16, `exec_integration` 11, `intent_rescue`
  12). **User: re-run `TC-AGV-001`** (home → kiosk-1 → back home) — the return-home leg should now
  complete (`ready` detected) with live distance-remaining status throughout, and the `/capture`
  timeout should be gone since the move no longer fails.

### Camera-agnostic calibration + editable camera/viewport on Robot Setup (Intel RealSense D405, 2026-07-15)

Made the coordinate pipeline explicitly camera-model-agnostic and exposed the two knobs on the Robot
Setup page. Camera in use: **Intel RealSense D405** — native **1280×720 (16:9)**, RGB synthesized from
the left depth imager (co-registered with depth), range 7–50 cm, depth FOV 87°×58°. New
`test_camera_config` 13/13; all prior suites green (9 suites, 153 checks); frontend `tsc -b` clean.

- **The camera resolution is DISCOVERED, not configured.** `real_robot.capture_screen` reads
  `width/height` from every `/capture` response and sets `_calibration["scale_x/y"] = measured/viewport`,
  overriding the `robot_camera_*` seed. A `/capture` always runs before the first tap (leading verify
  or `_ensure_localized`), so a real tap always uses MEASURED dims. Nothing about the D405 (or any
  camera) is hard-coded into the tap math — full detail is in the big comment block in
  `vision_agent/config.py`. This directly answers "what to change for a new camera model": **nothing in
  code** — the rectified resolution auto-measures and calibration adapts; optionally update the
  `robot_camera_*` seed on Robot Setup (cosmetic) and re-run Capture+Calibrate.
- **The ONE accuracy knob is the exploration viewport aspect ratio.** app_map coords live in the
  Playwright exploration viewport; `_scale` maps them to the rectified camera frame PER-AXIS, which is
  exact only when both share an aspect ratio (else a responsive app reflows and taps drift). The D405
  is 16:9, but the RECTIFIED frame's aspect = the kiosk SCREEN aspect (homography), so the real rule
  is: match the exploration viewport to the MEASURED rectified aspect.
- ✅ **New `PATCH /api/config/camera`** (`api/main.py`) saves `viewport_width/height` +
  `camera_width/height` to live settings + `.env` (validates positive ints; returns both aspect ratios
  + `aspect_matches`). `robot_camera_*` default changed 1920×1080 → **1280×720** (D405 seed; overridden
  by calibration anyway).
- ✅ **Robot Setup page (`kiosk-test-studio/RobotSetup.tsx`) gained a "Camera & Coordinate Calibration"
  card**: edit/save the exploration viewport + camera seed, live aspect-ratio labels, a mismatch
  warning, the measured (last-capture) resolution, and a **"Match viewport to measured camera"** button
  that copies the measured rectified resolution into the viewport (→ 1:1, no aspect risk). `client.ts`
  gained `setCameraConfig`. So the operator NEVER edits code or `.env` by hand — they enter/save on the
  page. Existing "Capture + Calibrate" already shows measured width/height/scale.
- **Setup order for a real-robot target:** (1) Robot Setup → Capture + Calibrate (measures the rectified
  resolution); (2) "Match viewport to measured camera" (or set the viewport aspect manually); (3)
  explore each kiosk in Playwright at that viewport; (4) run tests. `screen_width_m/height_m` stay
  unused by our code (the robot does pixel→3D itself). See [[agv-hardware-test-2026-07-13]].

### Coordinate lifecycle: explore-viewport now configurable + real-backend leading-verify capture (2026-07-15)

Two coordinate/verification fixes so the playwright-explore → real-robot-test path is accurate and
the first screen check works on hardware. Both default-preserving (playwright suites unchanged);
new `test_coord_config` 10/10, all prior suites green (`base_goto_target` 11, `agv_poll_status` 17,
`api_alignment` 29, `plan_normalize` 34, `inline_fast` 16, `exec_integration` 11, `intent_rescue` 12).

- ✅ **Playwright exploration viewport is now driven by `settings.viewport_width/height`** (was
  hard-coded `1400×900` in `playwright_stubs.py` for the browser viewport AND the on-screen-keyboard
  tap math, so `VIEWPORT_WIDTH/HEIGHT` had NO effect on exploration — a latent inconsistency: setting
  them would have skewed `real_robot._scale`, which divides by `viewport_width`). New `_vp()` is the
  single source; the browser viewport, `_click_key`, and the keyboard "done" tap all read it;
  `explore_screen._VIEWPORT_W/H` scroll fallbacks too. **Default stays 1400×900** → byte-identical
  playwright behaviour. **Why it matters:** `app_map` coords are pixels in the *exploration image*
  space, and `real_robot._scale` maps them viewport→camera as a per-axis proportional scale. That's
  exact only when the two images frame the SAME layout. 1400×900 is 14:9 but a kiosk camera frame is
  typically 1920×1080 (16:9); if the app is responsive it reflows between them and a per-axis scale
  mis-places elements. So for a REAL-ROBOT target, set `VIEWPORT_WIDTH/HEIGHT` to the kiosk's
  rectified-camera aspect ratio (e.g. 1920×1080) BEFORE exploring — now that actually takes effect,
  the app renders the layout the arm will photograph, and `_scale` becomes a clean map. (Pure
  playwright targets keep 1400×900 — explore and test share it.)
- ✅ **A LEADING `verify` now captures a camera frame on the real backend** (`run_vision_step.py`).
  Was playwright-only: `if settings.robot_backend == "playwright" and not last_screenshot:`. So on
  the real robot, a first-step `verify` (e.g. `TC-RPS-001` step 1 "the login screen is visible", which
  has no preceding tap) passed `image_path=""` into the pipeline → `_match_by_phash` got no image →
  inconclusive → the Claude fallback also got no image → **spurious FAIL on step 1 → needless Tier-3**.
  Fixed to `if not last_screenshot and settings.robot_backend != "demo":` — captures for playwright
  AND real (demo excluded: its pipeline is always-true and a capture would consume a scripted screen).
  On the real backend this capture ALSO localizes the screen (AprilTag) and calibrates the camera
  scale, so it doubles as the run's first calibration. **This is how the robot knows the login screen
  is up:** the plan's leading `verify` step captures via the camera and matches it against the app_map
  — `_match_by_phash` (0 LLM) when the stored hash is close, else the Claude-vision fallback (robust
  when the stored reference came from a *browser* screenshot and the live frame is a *camera* photo).
  It is NOT a hidden pre-step: it's an explicit plan step, so a plan that omits a leading verify just
  starts tapping stored coordinates without confirming the start screen (well-formed plans include it).

**Calibration lifecycle (how the arm gets accurate without `screen_*_m` / `robot_camera_*` from us):**
`real_robot.capture_screen` measures the ACTUAL camera resolution from every `/capture` response and
sets `_calibration["scale_x/y"] = camera/viewport`, which overrides the `robot_camera_width/height`
config defaults (those are only the pre-calibration fallback). So calibration is automatic on the
first capture — and `GET /robot/health` already does a capture+calibrate and reports the measured
resolution + scale. The physical `screen_width_m/height_m` are NOT consumed by our code: the ROBOT
converts the `(u,v)` pixel we send into a 3D stylus point using its OWN per-kiosk screen pose (from
AprilTag localization) and physical dimensions from ITS `/setup` config — we only send pixels. Those
config fields exist on the Configuration page as forward-looking values for when our `setup()` is
wired to upload kiosk definitions; today they're unused placeholders. No separate calibration UI page
is required (calibration is runtime-automatic and surfaced by `/robot/health`); a guided per-kiosk
"capture & confirm the rectified frame" gate is a nice-to-have that can live in the existing Robot
Health panel. See [[agv-hardware-test-2026-07-13]].

### Camera Vision Test page — are real-camera frames good enough, and at which tier? (2026-07-16)

Added a dedicated **Camera Vision Test** page (Settings group) + backend so an operator can capture a
frame straight from the arm and see, on the SAME code the live system uses, whether it supports Tier-1
(0 LLM) or needs Tier-3 (Claude). New `test_vision_test_page` 7/7; all prior suites green (9 suites, 153
checks); frontend `tsc -b` clean. **No existing behaviour changed** — the only core edit is a pure
extract-method in `analyze.py`.

- **The refactor (non-breaking):** `vision_agent/nodes/analyze.py` now exposes
  `analyze_image_elements(image_bytes) -> ScreenAnalysis` — the exact SLOW PATH of `analyze_screen`
  (Pass-1 extract + Pass-2 low-confidence correction + `_norm_to_px` → pixels), extracted so the
  diagnostic runs the SAME vision code the App Explorer uses (no reimplementation/drift).
  `analyze_screen` now calls it; its cache fast-path + history/state handling are byte-identical.
- **Backend endpoints (`api/main.py`, all under `/api`):**
  - `POST /vision-test/capture {capture_type: screen|raw}` — calls the ARM controller's `/capture`
    directly (`settings.arm_api_base`, spec `type` field) so it works whenever the arm is reachable
    regardless of `robot_backend`. Pure diagnostic: does NOT touch `real_robot`'s live calibration or
    the `_screen_localized` latch. Saves the frame under `screenshots/vision_test/`.
  - `POST /vision-test/upload` — save a frame captured elsewhere (e.g. the attached `screen_image.png`)
    so the pipeline can be assessed offline, no live robot.
  - `GET /vision-test/image/{file}` — serve the saved frame (`.name` guard, no traversal).
  - `POST /vision-test/analyze {filename, expected_screen?, use_claude}` — runs the REAL pipeline and
    returns a tier verdict: (1) **Tier-1 aHash** via `screen_cache.compute_hash` ranked against every
    `app_map` `screen_hash` (thresholds mirror `validate_pipeline._match_by_phash`: ≤8 match, >20
    mismatch); (2) **OpenCV boundaries** via `vision/detector.detect_interactive_rects` (0 LLM);
    (3) **OCR** via pytesseract full-image (0 LLM, if installed); (4) optional **Claude vision** via
    `analyze_image_elements` (Tier-3). Emits a plain-English recommendation.
- **Frontend (`kiosk-test-studio/pages/CameraVisionTest.tsx`, wired in `App.tsx`/`Layout.tsx`;
  `client.ts` gained `visionTestCapture/Upload/Analyze` + types):** capture (screen/raw) or upload →
  overlays OpenCV boxes (cyan) + Claude element centers (pink) on the frame with percentage-scaled
  divs → shows the Tier-1 ranking table (best highlighted), OCR text, Claude element table, and a
  verdict banner.
- **KEY EMPIRICAL FINDING (ran the pipeline on the user's real `screen_image.png`, the RPS/POS
  login frame, 600×360):** on that frame **all three zero-LLM methods FAIL** — Tier-1 aHash nearest
  screen is `smart_card_kiosk_station` at distance **24** (correct `login` is **42** away, both ≫ 8);
  OpenCV finds **0** interactive rectangles; tesseract OCR extracts an **empty** string. The frame is
  legible to Claude vision (Tier-3) but not to the cheap tiers, because a camera PHOTO (glare top-right,
  blur, slight keystone/perspective, blue color cast, low field/background contrast) is far from the
  crisp BROWSER screenshots the `app_map` references were captured from. Implications, in order:
  1. **Tapping accuracy is UNAFFECTED** — element COORDINATES come from the `app_map` (learned in
     Playwright), not from the camera frame. Only screen/text VALIDATION reads the camera.
  2. **Validation will fall through to Claude (Tier-3)** — ~1 LLM call per `verify`. Correct and
     robust, just not free. (This is exactly why the real-backend leading-verify capture + the
     `_claude_vision_validate` fallback exist.)
  3. **To make Tier-1 viable on the real robot**, the `screen_hash`/`reference_screenshot` references
     must be CAMERA-domain, not browser-domain — i.e. after Playwright exploration, do a one-time
     per-screen camera-reference pass on the real robot so aHash compares camera↔camera. Even then,
     glare/blur variance may keep it fragile; this page is the tool to evaluate it per kiosk.
  4. **Frame-quality levers** worth trying before a hardware test: reduce glare (diffuse lighting / a
     polarizer), improve focus, and confirm `type:"screen"` rectification actually deskews+crops to a
     fronto-parallel screen (the 600×360 attachment is also low-res — check the real `/capture` dims
     this page reports; higher-res sharper frames help OCR/OpenCV, though not the browser-vs-camera
     aHash gap). See [[agv-hardware-test-2026-07-13]].

### Camera Vision Test — real-camera findings + OCR/enhancement/capture-folder fixes (RPS login, 2026-07-20)

The operator ran the Camera Vision Test on a REAL arm-camera `type:screen` capture of the **RPS
(kiosk-2) login** screen (`Robot_Camera_images/Capture_API/…png`, 600×337, very blurry, blue cast,
glare). Four issues raised; all fixed and **actually tested** this time (new `tests/test_camera_vision.py`,
8/8 asserting; prior probe still 7/7; frontend `tsc -b` clean; `api.main` imports clean; no regression).

- **1 · Why did the RPS frame's aHash rank VPS's `smart_card_kiosk_station` (kiosk-1) #1, not RPS
  `login`? Is the hash logic buggy? — NO bug; NO fix to the hash, by design.** Verified empirically:
  aHash (`screen_cache.compute_hash`) is a **16×16 grayscale global-brightness fingerprint** — it
  encodes coarse light/dark LAYOUT, not fields or their order, and cannot bridge the **camera↔browser
  domain gap** (references were captured as crisp *browser* screenshots; this is a *camera photo*). A
  16×16 downscale collapses "centered bright panel on dark bg" for BOTH the RPS login form and VPS's
  smart-card panel, so VPS ranks marginally nearer purely by blob shape. The best distance is 30 (RAW)
  / 38 (enhanced), both **> 20 → verdict `no_match`, which is CORRECT**; within the mismatch band the
  ranking ORDER is meaningless (tested: preprocessing did NOT move `login` up — it stayed rank #10, and
  `smart_card_kiosk_station` stayed #1). **Changing `compute_hash` is off the table** — it would
  invalidate every stored `screen_hash` in existing app_maps (hard regression). The real Tier-1-on-real-
  robot fix is **camera-domain references** (one-time per-screen camera-reference pass after Playwright
  exploration so aHash compares camera↔camera). Code change: the `no_match` **detail + recommendation
  now say this explicitly** so the ranking isn't misread as a real content match (message-only; no logic
  change).
- **2 · "tesseract is not installed" — root cause + real, tested fix.** The Tesseract ENGINE binary
  WAS installed (`C:\Program Files\Tesseract-OCR\tesseract.exe`, v5.4) but **not on PATH**, and
  `pytesseract` (the pip wrapper, which only shells out to that binary) couldn't find it. My earlier
  probe *silently skipped* OCR instead of asserting it → the gap shipped. Fix: new
  `Settings.tesseract_cmd` (blank → auto-discover) + `api.main._resolve_tesseract()` (checks
  `settings.tesseract_cmd` → PATH → common install locations, then points `pytesseract` at it) +
  `_run_ocr()` which **distinguishes package-missing / engine-missing / working** with an actionable
  message each (engine-missing now says "install via winget / set tesseract_cmd", not a raw stack
  trace). **Now genuinely tested:** `test_ocr_reads_crisp_text` renders known text and asserts OCR reads
  "SIGN…PASSWORD" back — a broken OCR path can no longer pass.
- **3 · Local tools to improve frame quality for the cheap tiers — added + demonstrated.** New
  `_enhance_for_ocr()` (OpenCV, already a dep): **grayscale → 3× cubic upscale → CLAHE → light denoise
  → unsharp** (no hard threshold — binarizing destroyed more text than it recovered on this frame). The
  analyze endpoint now runs OCR + OpenCV + aHash on BOTH the raw and the enhanced copy and returns an
  `enhanced` block (image saved + served, OCR text, OpenCV count, nearest-hash). **Honest empirical
  result on this capture:** enhancement takes raw OCR from `''` → partial header text ("Generw toe POT"
  ≈ "Generic Kiosk POS"); the small field labels stay unreadable and aHash is unchanged — i.e. the
  tools help but **this frame is too blurry/low-res; the real lever is capture quality** (focus,
  lighting/glare, and higher rectified resolution — 600×337 is ~2× downscaled from the D405's 1280×720).
  The page states this so it isn't oversold.
- **4 · Auto-save captured frames to a dedicated project folder.** `_vision_test_dir()` now resolves to
  a **project-root `camera_captures/`** folder (was `screenshots/vision_test/`), a sibling of
  `screenshots/` so an App Explorer "clear all" can't wipe captured camera frames. Every `/capture` and
  `/upload` frame — plus each `_enhanced.png` — is saved there automatically and served via
  `/vision-test/image/{file}`. Added to `.gitignore` (generated data).
- **Frontend (`kiosk-test-studio` `CameraVisionTest.tsx` + `client.ts`):** OCR card shows the resolved
  engine + a clear amber warning (not a red crash) when the engine is missing; new **"Image enhancement
  · OpenCV preprocessing"** card shows the enhanced frame side-by-side with its recovered OCR / OpenCV
  count / nearest hash; `VisionAnalysis` gained `ocr.engine` + an `enhanced` block. Additive, backward-
  compatible (old fields intact).
- **No-regression scope:** none of the live automation paths changed — `compute_hash`,
  `validate_pipeline`, exploration, and the robot backends are untouched; OCR/enhancement live ONLY in
  the diagnostic endpoint; the `ocr` response keeps `available/text/error` (frontend contract) and only
  ADDS `engine` + `enhanced`. See [[camera-vision-test-2026-07-16]], [[agv-hardware-test-2026-07-13]].

### Claude vision 400 on real-camera JPEG — media-type now sniffed, not hardcoded (2026-07-23)

Testing the Camera Vision Test against the REAL arm `/capture type=screen`, Tier-3 element extraction
failed: `BadRequestError 400 … image/png media type, but the image appears to be a image/jpeg image`.
Root cause: the real arm's `/capture` returns **JPEG** bytes (`type:screen` is AprilTag-rectified +
re-encoded — magic bytes `ffd8ffe0`), even when the frame is saved with a `.png` filename, but **every
Claude vision block hardcoded `media_type: "image/png"`**. Browser screenshots are PNG so this never
bit until a real camera frame reached Claude. Generic fix (no behaviour change for PNG):

- ✅ **New `vision_agent/llm.detect_image_media_type(bytes)`** sniffs the format from magic bytes
  (JPEG `ff d8 ff`, PNG `89 50 4e 47…`, GIF, WEBP; defaults to `image/png` on anything unrecognised —
  the historical assumption, so browser PNGs are byte-identical).
- ✅ **Applied at EVERY Claude image block** (all consume the SAME bytes they base64-encode, so the
  media_type always matches the data): `analyze.py` (`analyze_image_elements` — the Tier-3 / Camera
  Vision Test path that hit the 400), `validate_pipeline.py` (all 4 vision fallbacks:
  `_claude_vision_text_check`, `_claude_extract_value`, `verify_intent_satisfied`,
  `_claude_vision_validate`), `validate.py` (legacy VisionAgent validate node), `run_vision_step.py`
  (`_capture_value_via_vision` + the inline-vision fast path — the two that hit real-robot camera JPEGs
  during a test run), and `explore_screen.py` (scroll-analysis + keyboard-map — browser today, future
  camera-reference-proof).
- **No regression by construction:** PNG frames still resolve to `image/png` (verified), so playwright/
  browser paths are unchanged; only the media_type STRING varies, never the bytes. Tested:
  `tests/test_camera_vision.py` (13 passed) asserts PNG/JPEG/GIF/WEBP detection, that the REAL camera
  frame `camera_captures/vision_test_screen_1784803921993.png` sniffs as `image/jpeg` (the exact 400
  trigger), and garbage→png default. All 5 edited modules import clean. (Pre-existing
  `test_vision_agent.py` failures here are unrelated — `ReadTimeout` to the arm at `192.168.0.105:8000`,
  which isn't reachable from the dev box.)
- **NEXT: real-robot RPS login test** (operator will run it and report). Watch: the leading `verify`
  captures a camera JPEG → now reaches Claude for screen ID without the 400; aHash will likely
  `no_match` (browser↔camera domain gap — expected, falls to Claude vision per [[camera-vision-test-2026-07-16]]);
  tapping uses app_map coords (camera quality irrelevant to taps). See the Tier clarifications below.

### Tier architecture clarified + real-robot 0-LLM plan + Tier-3 cost (analysis only, 2026-07-23)

Operator questions after the phone-photo test. **No code changed** — this records the architecture
answers + the recommended path so they aren't re-derived. Key clarifications (verified against
`run_vision_step.py` / `validate_pipeline.py` / `detector.py`):

- **OCR ≠ element detection.** Tesseract returns text strings only (+ boxes for text it read); it has
  NO concept of buttons/inputs, so it can NEVER emit UI-element boxes regardless of camera quality —
  that's OpenCV `detect_interactive_rects` / Claude's job. On the phone frame OCR read ONLY the
  white-on-near-black header ("ROBOTICS KIOSK AUTOMATION / Generic Kiosk POS") because Tesseract's
  Otsu binarization needs high luminance contrast; the login card (light text on medium-blue) + LCD
  moiré + soft/keystoned edges falls below that threshold and is dropped. A better camera spreads the
  header-quality read to the card, but still won't produce element boxes.
- **Tier-1 identifies the SCREEN, not elements.** Element coordinates come from the **app_map**
  (Playwright-learned), scaled to the camera — in EVERY tier. A Tier-1/2 tap is `robot.tap(px,py)` with
  px/py from the plan (app_map); the camera image is never consulted to LOCATE an element. ⇒ **camera
  image quality does NOT affect tapping accuracy at all** — only screen/text VALIDATION (`verify`
  steps) reads the camera. So the real-robot goal is two independent things: (1) screen identity works
  from a camera frame (keeps `verify` 0-LLM), (2) tapping stays accurate (calibration + AprilTag, not
  sharpness). See [[camera-vision-test-2026-07-16]].
- **Tier-3 confirmed = the FALLBACK, not the default.** Per step it makes a Claude VISION call that
  reads element coordinates OFF THE IMAGE and taps them (does NOT use app_map for coords) — used only
  when a screen/step isn't charted or a structured step fails. The operator's "identify screen, all
  detection from app_map" describes **Tier-1/2** (0 LLM per click; Claude paid only at Tier-2 planning,
  one text-only call). Two distinct runtime Claude uses: (a) screen-identity verify fallback when aHash
  can't confirm (~1 cheap call/verify), (b) full Tier-3 element extraction when a step falls through
  (~1 call/step). **Cost (Opus 4.8, $5/$25 per 1M):** ~3k input+prompt ≈ $0.015; screen-identity verify
  ≈ **$0.02–0.03/call**, full Tier-3 step ≈ **$0.04–0.06/step**. A 4-verify run all falling to Claude
  ≈ $0.10–0.12/run — modest but not zero at fleet scale, which is why closing the aHash gap matters.
- **Camera-domain references are NOT the only 0-LLM screen-ID option.** Ranked: **QR/fiducial screen
  tag** (app renders a tiny code encoding `screen_id` — deterministic, immune to the domain gap,
  cheapest reliable win IF the app can be modified) > **ORB/AKAZE feature match** vs camera-domain refs
  (robust to glare/blur/angle AND yields a homography → better than the current per-axis `_scale`) >
  **warp-then-reuse** (warp the camera frame to the browser-viewport grid via the AprilTag homography +
  photometric normalize, then aHash against EXISTING browser refs — no recapture) > OCR-token
  fingerprint > hash a stable high-contrast band (header) only. Recommended: QR tag + ORB fallback.
- **Physical setup — the real lever (D405 is a SHORT-range 7–50 cm camera; use it CLOSE, not at
  distance).** 21" 16:9 screen ≈ 465 mm wide; the operator's plan to shrink the RPS app to ~50% width
  (≈232 mm) for the myCobot 280's 280 mm reach ALSO fixes image quality: at ~15 cm the D405's 87° HFOV
  spans ≈285 mm → the app fills ~80% of the 1280×720 frame ≈ 4.5 px/mm (vs the current 600×341 crop =
  screen only ~21% of the sensor). Recommendation: half-width app + D405 at ~12–20 cm, centered,
  fronto-parallel (reduce keystone — 2nd-biggest lever after distance); confirm `/capture type:screen`
  returns full-res not downscaled; then a per-screen camera-domain reference pass (or ORB). **Joint
  angles are NOT the quality lever** — they change pose (distance/angle/framing), not sensor quality;
  optimize FOR "close + centered + head-on," not a specific joint config. Arm-reach and image-quality
  have the SAME solution. See [[agv-hardware-test-2026-07-13]].
- **Offered next build (not yet done, awaiting go-ahead):** a camera-domain reference-capture tool +
  an ORB matcher alongside aHash — local, 0-LLM, additive/non-regressive (behind existing toggles;
  playwright/demo untouched). Start with reference-capture to measure how much ORB buys.

### Camera Vision Test timed out on a large (phone-photo) upload — resolution-bounding fix (2026-07-23)

The operator uploaded a **mobile-phone photo** of the RPS login screen (`IMG_5716.jpeg`, **4032×3024**,
~12 MP — taken to compare a phone camera vs the RealSense frames) into the Camera Vision Test page. It
**timed out** (client allows analyze 90 s) instead of returning results. Root cause: `analyze` ran the
expensive 0-LLM steps at full resolution — `_enhance_for_ocr` upscales the source **3×**, so a 12 MP
photo became **~110 MP**, then `fastNlMeansDenoising` + triple-PSM OCR + OpenCV ran on THAT → minutes of
work. RealSense frames are ~600 px so this path was always fast; only an oversized upload triggers it.

- ✅ **Bound the working copy for the expensive steps** (`api/main.py`). New `_bound_for_processing(bytes,
  max_dim=1600)` downscales (LANCZOS, aspect-preserving) only when the longest side exceeds 1600 px, else
  returns the **original bytes unchanged**. `vision_test_analyze` computes `proc_bytes` once and feeds it to
  OpenCV, OCR, `_enhance_for_ocr`, AND the optional Claude call. **Tier-1 aHash stays on the full-res
  original** (`compute_hash` resizes to 16×16 itself, so its match semantics are byte-identical). The
  enhance upscale is also capped: `fx = min(3.0, 2400/longest)` (never < 1.0) so its output can't explode —
  a 600 px frame still gets exactly 3× (unchanged), anything larger is bounded to a 2400 px working image.
- **No regression by construction:** real camera frames (~600 px) and browser screenshots (~1400 px) are
  under the 1600 cap → `_bound_for_processing` returns them unchanged and every downstream step is
  byte-identical. Verified: new `test_camera_vision` tests (12/12) assert sub-cap frames pass through
  untouched (identity), a 4032×3024 frame is capped ≤1600 with aspect preserved, bad bytes return the input,
  and `analyze` completes end-to-end on a large frame with the enhanced output bounded ≤2400.
- **Empirical result on the phone photo** (analyze now **~19 s**, well under 90 s): the phone camera is
  MUCH clearer than the RealSense frames — raw-frame **OCR actually reads real text** ("ROBOTICS KIOSK
  AUTOMATION / Generic Kiosk POS / Card sharing off…"), and OpenCV finds a few rects (vs 0 on RealSense).
  BUT **Tier-1 aHash is still `no_match`** (nearest `smart_card_kiosk_station` at distance 22 > 20): even a
  crisp phone photo doesn't match the **browser-captured** reference hashes — the camera↔browser domain gap
  again (see [[camera-vision-test-2026-07-16]]); the real Tier-1-on-real-robot fix remains a camera-domain
  reference pass, not image quality. Also observed: on this already-sharp photo the **enhanced** OCR is
  WORSE than raw (3× upscale + CLAHE + denoise adds artifacts) — expected, since enhancement targets soft
  low-res frames; the operator sees both side by side. **Takeaway for the hardware effort:** a better
  physical camera/framing lifts OCR/OpenCV (Tier-2/3 legibility) but NOT Tier-1 aHash, which needs
  camera-domain references regardless of frame quality.

### Full Robot-API spec-alignment pass of the real backend (`real_robot.py`, 2026-07-15)

A line-by-line re-review of `vision_agent/robot/real_robot.py` against the robotics team's
`robot_kiosk_api.md` spec surfaced several request-body / polling mismatches. All fixed generically
(real backend ONLY — playwright/demo/stubs untouched, so the working playwright suites can't regress;
AGV `move`/`check_state`/`wait` plans don't call any of these paths, so the verified TC-AGV-001 path is
unaffected). New `test_api_alignment` 29/29; all prior suites still green (`base_goto_target` 11,
`agv_poll_status` 17, `plan_normalize` 34, `inline_fast` 16, `exec_integration` 11, `intent_rescue` 12).

- ✅ **`/screen/click` (tap) never sent `capture_after_last` → the "reused post-tap frame" never came
  back.** Per spec, `image_b64` is present in the `/arm/state` completion ONLY if the click was sent
  with `capture_after_last: true`. `tap()` relied on that frame (`click_result.image_b64`, surfaced as
  `image_path` and reused by the runner's dynamic-value `capture`) but never requested it — so on real
  hardware the reuse silently got nothing. Fix: tap now sends `capture_after_last: true` +
  `delay_between_ms: 800` (the 800 ms doubles as the post-tap settle so the returned frame is captured
  AFTER the kiosk transition, not mid-render). type/swipe don't reuse a frame, so they don't set it.
- ✅ **Non-spec `kiosk_id` scrubbed from `/screen/click` (tap/type/swipe) and `/card/tap`.** The spec
  bodies carry no `kiosk_id` — the target kiosk is whatever the last `/capture` localized, not a passed
  field. We just got burned by a schema mismatch (the `/base/goto` 422), so stray fields are a real
  422 risk on a strict server. Removed everywhere; the `card_tap(reader_kiosk_id)` arg is kept for
  logging/parity but no longer sent. (`_kiosk()` became dead code and was deleted.)
- ✅ **`/capture` now carries a `cmd_id`.** Spec: "every command must include a caller-generated
  `cmd_id`." `/capture` omitted it; added `{cmd_id, type:"screen"}`.
- ✅ **`/setup` body aligned:** `nav_map` → **`map`** (the spec field name — the old key would've been
  ignored, uploading no navigation map), added `robot_id`, dropped `cmd_id` (setup is a BLOCKING 200,
  not a 202 command). NB: `setup()` is defined but not yet wired into the runner, so this is
  correctness/future-proofing.
- ✅ **Card pick/tap polled for `idle` → would've hung until timeout.** Per spec a successful
  `/card/pick` and `/card/tap` settle in **`holding_card`** (the arm keeps gripping the card), NOT
  `idle` — the old `_poll` only accepted `idle`, so every card op would time out then abort. Fix:
  `_poll` gained a `terminal_states` param; card pick/tap pass `("holding_card","idle")`, replace keeps
  `idle`. (Card ops aren't wired into execution yet either — future-proofing, but now correct.)
- ✅ **409 "screen not localized" pre-empted.** Spec: `/screen/click` and `/card/*` REQUIRE a `/capture`
  since the last base motion, else 409. A structured Tier-1/2 tap skips `analyze` (no capture), so the
  FIRST tap right after an AGV arrival would 409. Fix: a `_screen_localized` latch — cleared by
  `navigate_to_kiosk`/`move_to_position`, set by a successful `/capture`; new `_ensure_localized()`
  auto-captures on demand before the first screen/card op. No-op in the vision flow (analyze already
  captured); fires once per arrival for structured plans.
- **Confirmed still-correct (no change needed):** `/base/goto {target}` (the 2026-07-13 fix); dual-
  controller routing via `_base_for` (`/base/*` → AGV, else arm) — re-asserted by the test; `_poll_base`
  accepting `ready` (spec lists base states as idle/moving/error, but real hardware returns `ready`
  with `nav_feedback` — our `_BASE_READY_STATES` covers both, and the extra nav fields are read
  optionally); `/arm/state` reading `click_result` from the poll (now populated, since tap requests the
  frame); `/base/abort` + `/arm/abort` bodies (`{cmd_id}`).
- **Still open / hardware-gated:** the arm/camera/card paths (`/screen/click`, `/capture`, `/card/*`)
  remain UNVERIFIED against physical hardware — a pure-AGV test (TC-AGV-001) exercises none of them.
  First real arm test should watch: the localization auto-capture firing before the first tap, the
  `capture_after_last` frame actually returning, and card ops settling in `holding_card`. See
  [[agv-hardware-test-2026-07-13]].

### Cross-kiosk E2E round-trip fixed live (`TC-E2E-001`, 2026-07-10)

A live Playwright re-run of `TC-E2E-001` (load at VPS → buy at RPS → verify balance back at VPS)
surfaced four compounding, generic defects on the cross-kiosk path. All are fixed in
`test_runner/nodes/run_vision_step.py` and **verified live end-to-end** (playwright + Claude API — a
full green 25-step PASS: load $4000 → capture card → RPS purchase `$756.67 paid with card 9993` →
back to VPS → reduced balance + PURCHASE at KIOSK-ID-2 newest-first). No VPS/RPS-specific code — the
fixes apply to every app and every backend.

- ✅ **The browser didn't follow the robot across kiosks → `verify` saw the wrong app.** Originally
  reported as `move: AGV → RPS` failing `Wrong screen: expected 'login', got
  'smart_card_kiosk_station'`. Root cause was broader than the `move` action: the browser app-switch
  was driven by an ad-hoc device-tag check that mishandled EVERY plan shape the LLM emits — explicit
  `move` actions (switch lived only in the device-routing block, skipped for `move`), bare `kiosk_id`
  targets (`"kiosk-2"` instead of alias `"RPS"` → device_map miss), and an untagged `verify login`
  placed BEFORE the first RPS-tagged interaction step (lazy switch hadn't fired yet). Fix — one
  **ground-truth-driven** router: before each step, resolve the kiosk it targets and switch the
  browser if different from the current one. The target kiosk comes from (a) the step's device/target
  tag, or (b) for a `verify`, the **kiosk that owns its `expected_screen`** (app_map screens carry
  `app_id`=kiosk_id) — so a `verify` for the next app switches even when untagged and out of order.
  The shared `_switch_browser_to` helper does the playwright URL nav (no-op on real); the `move`
  handler and the router both call it. `_load_device_map` now indexes devices by **both alias and
  kiosk_id**, so a target given in either form resolves. Backers: `current_kiosk` tracks the shown
  kiosk_id; `home`/`wait`/`check_state` never trigger a switch.
- ✅ **`capture` read an empty field → the issued card number never carried to RPS.** The plan's
  capture pointed at `card_number_input` (the empty *Check-Balance* input) because the issued number
  is shown in a **transient result box** (`[data-testid="station-card-loaded"]` → "Card Number: 9013")
  the App Explorer never charted, so the direct read returned `''` and RPS payment failed ("Card 1234
  not issued" — Tier-3 had invented a number). Fix: `capture` now has a **Claude-vision fallback** —
  when the direct element read is empty (or a label-noisy blob, per `_looks_like_value`), it
  screenshots the current screen and asks Claude to read the named value (`_capture_value_via_vision`),
  returning just the raw token. Generic: capture is inherently a runtime read, so one LLM call is the
  only way to carry an app-generated value forward. Confirmed live: `capture card_number → vision read
  '4934'`, and the RPS purchase then completed with the real card.
- ✅ **A mid-plan `vision_required` stalled the cross-kiosk return trip.** `_execute_structured_plan`
  used to RETURN at the first `vision_required` and hand the ENTIRE remaining plan to Tier-3 resume —
  which cannot perform the later structured `move` back to VPS, so after completing the RPS purchase it
  wandered on RPS (order_result → cart → payment…) and never verified the VPS balance. Fix: a
  `vision_required` is now handled **inline** (`_run_inline_vision`) — a bounded vision segment clears
  just that uncharted patch (stops as soon as the screen advances, so it can't drift into a second
  transaction), then structured execution **resumes** and runs the `move: AGV → VPS` + balance-check
  steps normally. Only if the segment makes no progress does it fall through to the outer Tier-3
  resume (unchanged safety net). A stall on the LAST plan step (a pure verify sub-task whose screen
  doesn't change by design) is NOT treated as failure — nothing remains to resume, so we keep the
  segment's own results and let the conclusive-verdict node judge, avoiding a redundant end-of-plan
  Tier-3 pass. Captured runtime values are also threaded into Tier-3 (`_run_tier3_continue(...,
  captured=…)`) so any `{{captured.*}}` in the resume path resolves to the real value, not a hallucination.
- ✅ **Test-data typo made the verdict non-deterministic.** `TC-E2E-001` step 1 loads **$4000** but its
  expected-result text asserted a "**$400** minus order total" balance — an internal contradiction (a
  $400 card can't buy the $756.67 phone). The arithmetic-checking conclusive-verdict node flip-flopped
  PASS/FAIL across identical runs on it. Corrected the expected text in the DB (`$400`→`$4000`), after
  which the balance ($3,243.33 = 4000−756.67) verifies deterministically. **This edited the user's test
  case data** — flag it; other suites may carry the same typo.
- The plan itself was regenerated via the `/tc-plan` path (force) — the Tier-2 re-planner sometimes
  emits a leaner plan that drops the `capture`/return-trip, whereas `_TC_PLAN_PROMPT` produces the full
  round-trip. **User: regenerate the `TC-E2E-001` plan in Test Intake, then re-run from the Studio** to
  confirm in the live monitor (capture vision-read + inline-vision resume both need the browser + Claude
  API). The card-number capture depends on the VPS "Smart Card Loaded" result box being on screen after
  *Use Mock Card*; if a future app hides it, capture degrades gracefully to an empty value → the step
  fails into Tier-3 rather than crashing.

### E2E step-19 speed, multi-kiosk suite isolation, number-format tolerance (2026-07-10)

Three follow-ups from a clean live re-run of `TC-E2E-001` and a mixed suite. All in
`test_runner/nodes/run_vision_step.py` + `vision_agent/nodes/validate_pipeline.py`, verified live,
and applied identically for playwright AND real-robot modes.

- ✅ **Issue #1 — the inline `vision_required` step (enter captured card + pay) stalled ~100 s.** It
  invoked the FULL VisionAgent (analyze→plan→execute→validate per step, re-analyzing before each tap
  → ~6 slow Opus calls) even though it already HAD the data. Fix: a **fast single-call path**
  (`_inline_vision_fast`) — ONE Claude-vision call returns the concrete actions ("type <captured
  value> into that field, tap the pay button"), executed directly via `robot.tap()/type_text()`. Uses
  the SAME coordinate convention as `analyze`/`execute` (`_norm_to_px` on the captured image →
  `robot.tap`, which the real backend scales viewport→camera), so it is correct on both backends. The
  full agent remains the fallback (playwright: only if the screen didn't advance). Confirmed live:
  `[VISION-FAST] 2 action(s) from 1 vision call → advanced payment → order_result`, TC-E2E-001 still a
  full 25-step PASS — step 19 dropped from ~6 LLM calls to 1.
- ✅ **Real-robot dynamic-value capture uses the /screen/click frame.** The `capture` step's vision
  read now, on the real backend, REUSES the post-tap camera frame the `/screen/click` response already
  returned (`last_screenshot`) — no extra `/capture` arm cycle — falling back to a fresh camera capture;
  playwright still takes a fresh browser screenshot. So the just-issued card number is read from what
  the robot's camera actually saw, then reused at RPS and back at VPS. (Coordinate handling for the fast
  path is identical to the existing real-robot VisionAgent path; still unverified vs physical hardware.)
- ✅ **Issue #2 — a mixed suite ran the 2nd test on the 1st test's app.** Selecting `TC-RPS-001` +
  `TC-VPS-001`: RPS passed, then VPS "launched RPS again and logged in". Root cause: `settings.kiosk_url`
  is set ONCE per run to the primary kiosk, and `reset_to_entry()` between tests reused it. Fix: new
  `_position_for_test(tc)` runs before each test's reset — playwright points the browser at THAT test's
  kiosk URL (reset then clears storage + lands on the right app); real robot drives the AGV to that
  test's kiosk device (parity). Confirmed live: `TC-RPS-001` (login→products on RPS) then `TC-VPS-001`
  (ValuePass station on VPS) both PASS, each on its own kiosk.
- ✅ **Issue #3 — number-format tolerance (5000 vs 5,000).** Confirmed already handled by
  `_value_matches` (strips whitespace/commas; now also currency `$€£₹`) — the mixed run's VPS verify
  matched `$3,000.00`. Added `_is_format_only_diff` + a **human-review flag**: when a value matches only
  after ignoring formatting (normalized/numeric equality, so `3000`==`$3,000.00` but `5000`≠`50000`),
  the verify PASSES and the step carries `human_review: true` + a note ("value correct — confirm the
  formatting difference is acceptable"), surfaced to the live monitor.
- **Verification:** TC-E2E-001 PASS (fast path), TC-RPS-001 + TC-VPS-001 mixed suite both PASS on the
  correct kiosks. Real-robot code paths are consistent but need hardware to confirm.

### Capture robustness + no false human-review on a clean run (`TC-E2E-001`, 2026-07-11)

A live re-run of `TC-E2E-001` surfaced two more generic defects (both in `test_runner/nodes/`,
verified live end-to-end — a clean 28-step PASS, and applied identically for playwright AND real
robot). No VPS/RPS-specific code.

- ✅ **A wrong `element_id` in the plan poisoned the `capture` step → the card number never carried
  to RPS.** The `TC-E2E-001` plan bound `capture card_number` to `card_service_connected_status` (a
  card-*service status* element), whose text ("Connected") is short and colon-free, so the old
  `capture` logic ("direct element read first; use Claude vision only if the read is empty or a
  label-noisy blob") accepted `_looks_like_value("Connected") == True` and typed the status word into
  the mock-card input → the RPS payment failed. This also explains the user's "it worked hours ago
  with the same plan" — that earlier run's element read *empty*, so the vision fallback fired and got
  the real number; this run's element read a plausible-looking status word, so vision was skipped.
  Fix (`run_vision_step._execute_structured_plan`, `capture` handler): **Claude vision is now the
  AUTHORITATIVE source for `capture`.** A capture reads an app-GENERATED, transient value (issued card
  number, confirmation code) the App Explorer usually can't chart reliably, so the plan's element_id
  is inherently untrustworthy — read the named value from the CURRENT screen via one fast-LLM call and
  fall back to the direct element read ONLY when vision genuinely can't see it. One LLM call on a rare
  step makes a wrong/stale element_id harmless. Real robot REUSES the post-tap `/screen/click` camera
  frame (`last_screenshot`, no extra `/capture` arm cycle); playwright takes a fresh browser shot.
  Confirmed live: `capture: card_number card_number='8816'` despite the bad element_id, then `8816`
  entered at RPS and re-checked back at VPS. (The stale plan is left as-is — the fix makes it correct;
  regenerate via Test Intake only if you want a cleaner element_id.)
- ✅ **A trailing `vision_required` verify re-entered the card number and re-checked the balance, then
  the conclusive verdict falsely demanded human review — on an all-correct run.** Two compounding
  issues in one symptom (`suite_1783687301`: after the structured balance-check verify PASSED, steps
  28-33 re-typed the card number, re-tapped Check Balance, and ran the full VisionAgent; the verdict
  then came back AMBIGUOUS "the log does not confirm the arithmetic" with `requires_human_confirmation`).
  Fixes:
  - **Inline vision recognises "already satisfied"** (`_inline_vision_fast`): the fast-path prompt now
    returns `{"actions": [], "verified": true}` when a VERIFICATION sub-task's answer is ALREADY
    visible on screen (balance/transaction already displayed). The handler emits ONE clean verify step
    and reports done — no re-entry, no re-tap, and NO fall-through to the slow full agent (which used
    to redundantly repeat the whole check). `{"actions": [], "verified": false}` still means "can't do
    it here → full-agent fallback", so genuine uncharted patches are unaffected. Run dropped 33 → 28
    steps; step 28 is now `verify … Already satisfied on the current screen (no re-entry needed)`.
  - **No fabricated ambiguity on a clean run** (`conclusive_verdict`): the prompt now states the
    step-level pipeline ALREADY compared each `verify` step's on-screen value to the expected value
    (tolerant of currency/thousands formatting), so a passed verify means the value was confirmed —
    the verdict LLM must not re-derive arithmetic or invent doubt, and a run with zero gaps + zero
    failures defaults to PASS. A deterministic guard also overrides any non-PASS verdict to PASS when
    every step passed with zero gaps/failures (a legitimate step-level `human_review` format-only flag
    is preserved and re-surfaced; a fabricated one is dropped). Confirmed live: `verdict PASS`,
    `requires_human_confirmation False`, no human prompt.

### Inline vision must return control so the cross-app return trip always runs (`TC-E2E-002`, 2026-07-11)

`TC-E2E-002` (a card with INSUFFICIENT balance is declined at RPS, then balance re-checked unchanged
at VPS) reached the decline correctly but then failed the last half: every VPS balance verification
"landed on a Loyalty Rewards page", the return trip to VPS never happened, and the conclusive verdict
went AMBIGUOUS/human-review. Root cause was NOT the cross-app switch (which is solid and ground-truth
driven) — it was that the `vision_required` RPS-payment segment never returned control to the
structured plan, so the subsequent structured `verify (VPS)` / balance-check steps (which trigger the
switch back to VPS) never ran.

- ✅ **Generic fix — the inline-vision fast path hands control back after executing its actions,
  instead of gating on a forward screen transition** (`run_vision_step._inline_vision_fast`). The old
  logic treated the sub-task as "done" only if the DOM screen ADVANCED (else it fell into a
  3-iteration full VisionAgent). That is wrong for a valid TERMINAL result that stays in place: an
  APPROVED payment advances (`payment → order_result`, so `TC-E2E-001` worked), but a DECLINED payment
  shows an error banner on the SAME `payment` screen — no DOM change → the full agent kicked in and
  WANDERED off the result screen (onto RPS "Loyalty Rewards"), consuming the rest of the run so the
  structured return trip to VPS never executed. Now: once the fast path executes its vision-directed
  actions (enter value(s) + submit) without error, the uncharted patch is cleared → it returns
  `advanced=True` and the STRUCTURED plan resumes. Its own `verify` steps validate the result and its
  cross-app `verify`/`move` steps drive the return trip — the cross-app switch ALWAYS gets its turn.
  The full-agent fallback is now reserved for when the fast path could produce NO actions at all; if
  the actions genuinely didn't take, the next structured verify fails and the existing Tier-3 resume
  is the safety net. This is not a per-test patch: it fixes the general contract that a `vision_required`
  segment clears one uncharted patch and returns, for EVERY app and outcome (approved/declined/error).
- **Backend parity:** identical for playwright and real robot — the fast path already sends
  screenshot-space coordinates straight to `robot.tap()/type_text()` (real robot scales
  viewport→camera and reuses the `/screen/click` frame), and the return-control change is
  backend-agnostic. Real robot still needs hardware to confirm.
- **Verified live (playwright + Claude):** `TC-E2E-002` now a clean 24-step PASS — decline confirmed
  in place at RPS (step 20), browser switched back to VPS (step 21), balance still `$75` unchanged
  (step 24), verdict PASS with no human review. `TC-E2E-001` re-run as a regression check still PASSES
  (26 steps) — the approved-payment `advanced payment → order_result` path and the "already satisfied"
  verify both intact.

### Enter-before-submit + stale-error intelligence on the inline-vision path (`TC-E2E-001`, 2026-07-11)

A repeated live re-run of `TC-E2E-001` intermittently FAILED at the RPS payment sub-task (a single
`vision_required` step: "enter the card number issued at VPS ({{captured.card_number}}) and complete
the order" — the payment screen has no card-input element charted, so it is handled inline by the
fast vision path). Two compounding, generic defects — both fixed in `run_vision_step._inline_vision_fast`
+ the `PLAN_STEPS`/`VALIDATE_STEP` prompts, unit-tested across 6 scenarios (16/16 checks), and applied
identically for playwright AND real robot. No RPS/VPS-specific code.

- ✅ **Clicked Pay WITHOUT entering the card number (random).** Root cause was generic, not a bad
  element id: the fast path's `type` handler only focuses the field `if px and py`, and a controlled
  (React) input silently DROPS text typed into an UNFOCUSED field. When the fast vision call returned
  a `type` with a missing/`[0,0]` center — or skipped the `type` entirely and returned only the Pay
  `tap` — the value never landed, yet the submit tap still fired, so RPS showed "enter a card number".
  This is why it failed only *sometimes* (depended on whether that run's vision reply carried a valid
  field center). Fix: an **ENTER-BEFORE-SUBMIT GUARD** — the fast path now tracks whether the required
  value actually LANDED (a `type` with a non-empty value AND a valid field center that was focused),
  and NEVER taps the submit/pay/confirm button unless it did. If entry didn't land it re-plans the
  ENTRY once (a 2nd vision call told "your entry didn't land; return the field's pixel center + the
  value, then the submit tap"), then submits. "Entry required" is inferred from the model emitting a
  `type` OR the sub-task text asking to enter/type/fill a value we actually hold (`captured`), so a
  model that skips the type is still caught. If the 2nd attempt still can't enter, it hands off to the
  full VisionAgent (unchanged safety net).
- ✅ **Stale error made the solution give up / re-enter without ever clicking submit.** RPS keeps a
  prior attempt's error banner on screen until a corrected value is re-submitted — so after a bad
  submit, the agent saw the SAME error even after entering the correct card, concluded "RPS rejects
  every card", and (in the full-agent path) kept re-entering the value WITHOUT tapping Pay, watching
  the stale banner. Confirmed by testing RPS manually: entering the correct card and clicking Pay
  succeeds even with the old error still visible. Fix — **stale-error tolerance**, three places:
  (a) the fast-path prompt now states an already-visible error may be LEFT OVER from a previous failed
  attempt — enter the correct value and tap submit anyway; the result is re-checked AFTER the submit;
  (b) `VALIDATE_STEP` no longer fails a `type` merely because an error/toast is visible (it is often
  stale), and adds a STALE-ERROR RULE: a submit is judged by the RESULT produced AFTER the tap, not by
  a pre-existing error; (c) `PLAN_STEPS` gained a STALE-ERROR RECOVERY rule: when re-entering a
  corrected value, tap the field → type → tap submit → THEN verify; never re-enter and stop at the old
  error without submitting; conclude failure only if the error remains AFTER the corrected submit.
- **Division of labour (why this is generic and regression-safe):** the fast path guarantees the value
  is actually entered, then ALWAYS clicks submit, then HANDS CONTROL BACK to the structured plan — the
  structured `verify` step compares the on-screen result to the EXPECTED outcome and concludes (i.e. it
  "checks the result after clicking"). So a legitimate in-place DECLINE (`TC-E2E-002`, correct card /
  insufficient funds, no DOM change) is unaffected — the fast path enters+submits once and returns; the
  verify judges decline-vs-success. An approved payment (DOM advances) is unaffected. Only OUR failure
  to enter the value triggers the re-plan. Unit tests cover: happy path, model-skips-type, zero-coord
  type, in-place decline (no wrong retry), already-satisfied verify, and no-actions→full-agent fallback.
- **Backend parity:** the guard is coordinate/value-based (backend-agnostic); the fast path already
  sends screenshot-space coords to `robot.tap()/type_text()` (real robot scales viewport→camera and
  reuses the `/screen/click` camera frame). Real robot still needs hardware to confirm.
- **User: re-run `TC-E2E-001` from the Studio** to confirm live (the intermittent card-entry failure
  should be gone; a wrong first submit now self-corrects and re-submits before concluding).

### Reusing a captured value (same card) never bypassed by a charted button (`TC-E2E-003`, 2026-07-11)

`TC-E2E-003` buys TWO products in one RPS session, paying for EACH with the SAME card issued at VPS,
then checks the reduced balance back at VPS. It FAILED: the first payment never happened, the run
drifted into the second product, then desynced (`verify payment … expected 'payment', got 'cart'`)
and Tier-3 got stuck tapping "Use Mock Card" which does nothing without a card number. Root cause
(confirmed generic, not a bad element id):

- The RPS `payment` screen is only **partially charted** — the App Explorer captured its completion
  buttons (`use_mock_card_button`, `start_card_reader_session_button`) but NOT the card-number INPUT
  (it's transient/uncharted, which is exactly why `TC-E2E-001` needed a `vision_required` there). Both
  planner prompts (`_TC_PLAN_PROMPT`, `PLAN_FROM_MAP`) already say "capture the value, reuse it via
  `{{captured.NAME}}`, use vision_required if the element isn't charted" — but a *plausible-looking*
  charted button tricked the LLM: it mapped "pay with the SAME card" to a single `tap
  use_mock_card_button` and **never entered `{{captured.card_number}}`** for EITHER payment. An app_map
  tap always "passes" at the executor, so the un-completed first payment went undetected and the flow
  desynced. (`TC-E2E-001` only worked because at plan time that screen wasn't charted, so it got the
  vision_required; re-exploration charting the button made the planner regress.)

Fixed in two layers — generic (any reused runtime value, not just cards) and identical for playwright
AND real robot (it only edits the plan / drives the existing inline-vision path):

- ✅ **Deterministic net — `test_runner/plan_normalize.py` `normalize_captured_reuse(plan, app_map)`.**
  Rewrites a completion/submit `tap` that is meant to REUSE a value captured earlier into a
  `vision_required` step that enters `{{captured.NAME}}` and completes, but ONLY when (a) a `capture`
  actually preceded the tap, (b) the step's description signals BOTH reuse ("same"/"issued"/"reuse"/
  "that card|code"/"captured"/a capture name) AND completion ("pay"/"mock card"/"complete"/…), (c) the
  value is NOT already entered on that screen by a `{{captured.*}}` type step, and (d) the screen has
  NO charted input element to receive it (so entering it genuinely needs vision). Idempotent and
  conservative — it leaves correctly-planned reuse (value typed on the screen, or an input charted)
  untouched, and never fires without a preceding capture (so a FIRST use like "use the mock card to
  load $600" is unaffected). Wired into ALL plan paths so cached AND fresh plans are covered:
  `POST /tc-plan` (before caching), `parse_steps` Tier-2 (after generation), and `parse_steps` Tier-1
  (on cache HIT — so an OLD wrong cached plan is corrected at run time without a forced regenerate).
  The converted step keeps its `device`/`screen_id` (so cross-kiosk routing still switches apps) and
  drops the misbound `element_id`/`px`/`py`.
- ✅ **Prompt hardening — new "CONSUMING A CAPTURED / SPECIFIC VALUE" rule in BOTH planners**
  (`_TC_PLAN_PROMPT` rule 6c, `PLAN_FROM_MAP`): reusing a specific captured value MUST be an explicit
  `{{captured.NAME}}` entry (or a `vision_required` when its input isn't charted) — NEVER a generic
  charted completion button (Use Mock Card / Apply / Start Card Reader / Confirm), which pays with a
  generic/blank value and breaks the balance check; the value must be entered AGAIN for EACH payment
  in a multi-payment flow; and add a verify of the RESULT after each payment so a silent no-op is
  caught immediately instead of desyncing the next step.
- **Why it fixes it + regression safety:** the converted `vision_required` payment runs through the
  same inline-vision fast path hardened earlier today (enter-before-submit guard + stale-error
  tolerance), so it focuses the RPS card field, types the captured number, submits, and hands control
  back — for EACH payment. The captured value is resolved before it reaches vision (`_sub_captured` on
  the description + the `captured` dict passed through), so vision gets `4272`, not the placeholder.
- **Tests (all pass, no live browser/API needed):** `normalize_captured_reuse` — 19 checks incl. the
  REAL `TC-E2E-003` plan + real `app_map` (exactly the two `use_mock_card_button` payment taps convert;
  VPS load, capture, and balance-check untouched; idempotent; conservative when an input is charted or
  the value is already typed; works for a non-card value too). Executor integration — 6 checks: the
  converted plan flows through `_execute_structured_plan`, capture populates `card_number=4272`, and
  BOTH payments invoke inline vision with the RESOLVED number. The earlier inline-fast suite (16) still
  passes (no regression). The stale `TC-E2E-003_b2e8a64c5d.json` plan was deleted.
- **User: regenerate the `TC-E2E-003` plan** (Test Intake → force) and re-run from the Studio to
  confirm live (both payments now enter the captured card; needs the browser + Claude API). Any other
  test that reuses runtime data across steps (same code/id/reference) benefits from the same net.

### Reuse-payment consistency: canonical [deterministic completion tap] + [enter+complete vision] (`TC-E2E-003`, 2026-07-12)

Two iterations on the SAME symptom (TC-E2E-003 first RPS payment). Both are captured because the first
attempt's DESIGN was wrong and the second corrected it — do not reintroduce the first.

**Iteration A (WRONG — do not repeat): "enter-only vision, then tap".** After the prior day's fix,
TC-E2E-003 still failed: a LONE `vision_required(enter + complete)` on the partially-charted RPS
`payment` screen forced LIVE VISION to choose the completion button, and `_inline_vision_fast` executed
EVERY tap the model returned (`for a in submit_actions:`), tapping BOTH `use_mock_card_button` AND
`start_card_reader_session_button`. The first fix split entry from completion into `[vision_required
entry_only]` (type the card, tap NOTHING) + `[tap use_mock_card_button]`. **This FAILED live** — it
FALSELY PASSED without ever completing the payment. Reason: on the real RPS app **"Use Mock Card" must
be tapped BEFORE the card number is entered** (it starts the mock-card flow / reveals the field);
entering the card first and tapping after does NOT complete the order. The order was backwards.

**Iteration B (CORRECT — matches the proven-live TC-E2E-001 order).** The canonical shape for EVERY
reuse-of-a-captured-value payment on a screen with a charted completion button but NO charted input is
now, IN THIS ORDER:
  1. `tap <charted completion button>` — deterministic method selection (e.g. tap "Use Mock Card"),
     which starts the mock-card flow / reveals the card field. Tapped from the app-map element/coords,
     so live vision never has to choose between two method buttons → the double method-button tap is
     impossible.
  2. `vision_required` (enter + complete) — live vision types `{{captured.NAME}}` into the field that
     now appears and confirms the payment.
This is exactly TC-E2E-001's proven-working structure. Generic (any reused runtime value); identical
for playwright AND real robot (only edits the plan / drives the existing inline path); no VPS/RPS code.

- ✅ **`test_runner/plan_normalize.py` is authoritative** — `normalize_captured_reuse` converges ANY
  variant to `[tap B]` + `[vision enter+complete]`: a lone `vision_required(enter+complete)` (inserts
  the tap BEFORE it), a lone reuse completion `tap` (adds the vision AFTER it), the pair in either
  order, AND the prior wrong-order `[vision][tap]` (FLIPS it). It absorbs an adjacent completion tap in
  either position so the re-emitted order is always tap→vision. `_find_completion_element` scores the
  charted completion button (`mock card`/`complete`/`pay`/`confirm`…) and EXCLUDES pure await-input
  controls (reader/session/"tap your card") — so it never picks `start_card_reader_session_button`
  (@766,660); it picks `use_mock_card_button` (@766,718). Fires ONLY when a capture preceded the step,
  the description signals reuse AND completion, the value isn't already typed on that screen, the screen
  has NO charted input, AND a charted completion button exists. Steps it produces carry an internal
  `reuse_norm` marker so a second pass is inert (idempotent). Wired (unchanged) into all plan paths —
  `POST /tc-plan`, Tier-2 generation, and **Tier-1 cache HIT** — so the CURRENT wrong-order cached
  `TC-E2E-003` plan is FLIPPED to the correct order on load, without a forced regenerate.
- ✅ **`_inline_vision_fast` reverted to its prior-session state** — the Iteration-A `entry_only` mode
  was removed entirely (it was a wrong-model artifact). The step-2 vision runs the normal
  enter+complete fast path (with the existing enter-before-submit guard + stale-error tolerance): after
  the deterministic Use-Mock-Card tap, it only has to type the card into the revealed field and tap the
  form's confirm — it never sees `start_card_reader_session_button`, so no double method-tap.
- ✅ **Both planner prompts steer to the tap-first canonical** (`_TC_PLAN_PROMPT` rule 6c in
  `api/main.py`; the CONSUMING section in `PLAN_FROM_MAP`): when the input isn't charted, TAP the
  charted completion button FIRST, THEN emit a `vision_required` to enter `{{captured.NAME}}` and
  confirm — never a lone vision that both selects the method and enters, and never the reverse order.
  Repeat BOTH steps for each payment in a multi-payment flow. The normalizer enforces it regardless.
- **No-regression design:** the cached `TC-E2E-001` plan is LEFT UNTOUCHED — its reuse `vision_required`
  carries no `screen_id`, so no completion button is discoverable and the normalizer skips it (verified
  by test), AND its own preceding `tap use_mock_card_button` already gives it the canonical order.
  The user confirmed TC-E2E-001 still PASSES live after these changes.
- **Tests (all pass, no live browser/API):** `plan_normalize` — 34 checks incl. the REAL current
  wrong-order `TC-E2E-003` plan + real `app_map` (both RPS payments FLIP `[vision][tap]` → `[tap
  use_mock_card_button @766,718][enter+complete vision]`; no `entry_only` remains; VPS load/capture/
  balance-check untouched; idempotent; conservative when an input is charted or only an await-button
  exists; the completion finder rejects `start_card_reader_session_button`; the REAL `TC-E2E-001` plan
  is left byte-identical; a lone vision inserts the tap before, a lone tap adds the vision after — a
  generic `ref_code` too). `inline_fast` — 16 checks (prior-session enter-before-submit/stale-error
  suite, unchanged, entry_only removed). Executor integration — 11 checks: the wrong-order plan is
  flipped, then flows through `_execute_structured_plan` so capture populates `card_number=4272` and
  for BOTH payments the deterministic `use_mock_card_button` (766,718) tap fires IMMEDIATELY BEFORE the
  vision call (event order `tap→vision, tap→vision`), the vision gets the RESOLVED value, and
  `start_card_reader_session_button` (766,660) is NEVER tapped. Pre-existing `test_vision_agent.py`
  live-Claude flakiness (2 screen-classification tests, full-VisionAgent path, untouched) is unrelated.
- **User: re-run `TC-E2E-003` from the Studio** to confirm live (needs the browser + Claude API).
  Runtime normalization flips the current cached plan to the correct order on load, so a plain re-run
  works even without regenerating; regenerate (Test Intake → force) only to refresh the frontend copy /
  get a clean cached plan. TC-E2E-001 already confirmed still passing.

### Verify tolerates a stale `expected_screen` — intent rescue (`TC-E2E-003`, 2026-07-12)

With the tap-first payment fix in place, TC-E2E-003's first RPS payment now COMPLETES — but the run
then FAILED on the very next step: `verify "First order completed successfully paying with the same
captured card"` had `expected_screen: "payment"`, yet a successful payment had already advanced the
app to `order_result`. The screen shown DID match the step's described outcome, but the executor
hard-failed on the screen-id mismatch (`expected 'payment', got 'order_result'`) and desynced into a
Tier-3 resume that wandered (it started checking a later VPS step).

**Root cause:** a `verify` step's real intent is its DESCRIPTION; `expected_screen` is a plan-time
GUESS. The planner guessed 'payment' because the payment→`order_result` transition is UNCHARTED — the
mock-card completion is done by live vision, so the App Explorer never recorded that transition, and
the planner has no `order_result` target to point at. So **the plan alone cannot be relied on to get
`expected_screen` right here** — execution must tolerate a stale one. Separately, `run_validate_pipeline`
short-circuits to a hard `screen_id` failure on any definitive screen mismatch; its Claude-vision node
(which judges the DESCRIPTION) only runs when the screen check is INCONCLUSIVE, never on a mismatch.

**Fix (the user's option 3 — execution intelligence as the robust net + a plan-side nudge). Generic;
identical for playwright AND real robot; no VPS/RPS-specific code.**
- ✅ **Intent rescue in the `verify` handler** (`run_vision_step.py`): AFTER the existing nav-settle
  retries, when a verify's ONLY failure is a `screen_id` mismatch AND it carries NO `expected_text`,
  capture a FRESH screenshot and ask a strict Claude judge, new `verify_intent_satisfied` in
  `validate_pipeline.py`, whether the CURRENT screen satisfies the step's DESCRIBED outcome. If yes →
  the verify PASSES (`method: "intent_vision"`, observation records that the planned `expected_screen`
  was stale) and the structured plan CONTINUES normally (so the next `products_nav` + second purchase
  run, instead of desyncing to Tier-3). The judge is STRICT — a genuinely wrong screen (nav failed,
  an error/decline, an unrelated screen) does NOT satisfy a specific description, so real failures
  still fail. Backend-agnostic: judges a screenshot both the playwright browser and the real-robot
  camera produce.
- ✅ **Scoped to avoid regressions.** The rescue fires ONLY on `success is False AND method ==
  "screen_id" AND not expected_text`: a screen MATCH passes normally (no rescue, no extra LLM call); a
  value assertion (`expected_text`/`expected_value`) that fails is a real failure and is NEVER rescued;
  a verification-GAP (unknown screen) path is unchanged. Runs after the nav-settle retries, so a
  transient mid-navigation screen isn't mistaken for the result. Costs ONE extra LLM call only on a
  persistent screen mismatch (a rare, already-failing path).
- ✅ **Plan-side nudge (defense-in-depth)** — both planners (`_TC_PLAN_PROMPT` in `api/main.py`,
  `PLAN_FROM_MAP` verify rule) now say: set a verify's `expected_screen` to the RESULT screen the
  tester actually sees when the outcome is true (the order-result/confirmation screen, NOT the
  'payment' screen the action started on); if the exact result screen isn't charted, pick the closest
  charted screen and write a precise `description` — the runtime validates the described outcome and
  tolerates a stale `expected_screen`. This reduces wrong guesses going forward; the runtime rescue is
  the guarantee.
- **Tests (all pass, no live browser/API):** new `test_intent_rescue` — 12 checks: (1) stale
  `expected_screen 'payment'` on `order_result` with intent satisfied → verify PASSES `intent_vision`
  and the plan does not desync; (2) genuinely-wrong screen (intent not satisfied) → still FAILS,
  `method` stays `screen_id` (no over-pass); (3) a screen MATCH passes normally and the intent judge
  is NEVER called (no regression / no added cost); (4) a verify WITH `expected_text` + screen mismatch
  does NOT invoke the rescue (a value assertion stays a real failure). The prior suites still pass
  (`plan_normalize` 34, `inline_fast` 16, `exec_integration` 11). Pre-existing `test_vision_agent.py`
  live-Claude flakiness (2 tests, unrelated) unchanged.
- **User: re-run `TC-E2E-003` from the Studio** to confirm live — the first-order verify should now
  pass on the result screen and the run should proceed to the second purchase and the final VPS
  balance check (needs the browser + Claude API). Regenerating the plan (Test Intake → force) will also
  give a cleaner `expected_screen` on the result verifies, but is not required — the runtime rescue
  handles the current cached plan.

### Studio / infrastructure (sibling `kiosk-test-studio`)

- ✅ **`fetch` had no timeout** — dashboard hung on "Loading…", Reset froze uncancellably, readiness
  showed API ✅ before it responded. Fix: `req()` uses `AbortController` (10s default, 30s for
  `/reset`), Cancel aborts the in-flight request, `apiOnline` starts `false`.
- ✅ **`ECONNREFUSED` — Vite proxy IPv6 vs uvicorn IPv4.** On Win11 + Node 17+, `localhost` resolves
  to `::1` first but uvicorn bound IPv4-only. Fix: proxy target set to explicit
  `http://127.0.0.1:8001`.
- ✅ **Reset didn't clear data / exploring a down app "succeeded".** ORM bulk-deletes silently skipped
  rows (and an old uvicorn predated the `/reset` route); explore never checked URL reachability. Fix:
  raw SQL `DELETE FROM …`; reachability check in `start_explore()` returns HTTP 400 immediately when
  the URL is unreachable.

### Lingering / open items

- ⚠️ **Kiosk-URL lifecycle + per-kiosk plan scoping unverified live (2026-07-08)** — the
  wrong-kiosk-URL, remembered-URL, multi-app-preservation, and per-kiosk plan-scoping fixes are
  unit-tested (merge/scope/alias/scoping logic verified with an in-memory DB) and import-clean, but
  need a live browser + Claude API run against the real kiosk apps to confirm end-to-end. Requires the
  user re-steps (re-explore per kiosk; align Device Map alias→kiosk_id; regenerate plans).
- 🔲 **Explorer identifier reuse (captured_values) not working live, DE-PRIORITISED** — the explorer
  still didn't reuse the issued card number for check-balance/add-money. Accepted for now: real
  test execution is expected to navigate those flows from on-screen elements. Revisit only if
  test-time navigation proves insufficient.
- 🔲 **Mixed-scale coordinate fix unverified live** — clear existing app maps and re-explore with a
  real Claude API + browser to confirm end-to-end.
- ⚠️ **Real-robot backend parity** — `real_robot.tap()` already carries the post-tap settle
  (`time.sleep(0.8)` with rationale) since the real backend has no DOM to `wait_for_load_state` on, and
  `navigate_to_url` is a correct Playwright-only no-op (a physical robot faces the device it drove to).
  So the interaction-timing parity is as close as the DOM-less backend allows; still untested vs real
  hardware. Keep arm vs AGV routing/timing in sync when editing either backend.
- 🔲 **Unexplained coordinate regression** — the layout-shift episode was worked around ("restore the
  previous layout"), not fixed in code.
- 🔲 **Verification debt** — the validation-field fix and per-run screenshot routing had backend edits
  but frontend build + browser verification were still pending at the last checkpoint.

## AWS production-readiness assessment (2026-07-20)

Reviewed `Design/aws_environment_stack.svg` (edge → ingestion → stream processing → agent
orchestration → vision inference → storage → web → observability) against the current
implementation. **Analysis only — NO code changes were made**, per the standing priority: the
immediate goal is a **local-lab, real-robot, end-to-end run with nothing on the cloud**; AWS migration
is explicitly deferred until after that. This section records the assessment so it isn't re-derived.

**Readiness verdict (if we had to use AWS "tomorrow"):**
- **Lift-and-shift** (same app on ECS/EC2 + Bedrock + S3 + RDS, no topology change): **~80% — days.**
  The three config toggles already exist and are wired end-to-end: `VISION_BACKEND=bedrock`
  (`llm.py` → `ChatBedrockConverse`), `STORAGE_BACKEND=s3` (`storage/aws.py` `S3Storage` +
  `SQSImageQueue` already written), `db_url=postgresql://…` (SQLAlchemy is engine-agnostic;
  `api/database.py` migrations are plain SQL). `boto3`/`langchain-aws` are declared under the `[aws]`
  extra. Remaining work is provisioning + secrets + auth + containerization, not app rewrites.
- **Full diagram topology** (IoT Core/MQTT → MSK/Kafka → Lambda → SQS → SageMaker → Fargate + Redis
  + API Gateway WS): **~30–40% — weeks-to-months.** The event-driven fabric does not exist: we use
  **synchronous point-to-point REST** to the robot (`real_robot.py` POST→poll), **in-process threads**
  (`supervisor/` `ThreadPoolExecutor`, `api/main.py` daemon threads) instead of Fargate containers,
  **in-memory LangGraph state + a `run_id→callback` broadcaster dict** instead of ElastiCache Redis,
  and **an in-process FastAPI WebSocket** instead of API Gateway WS. There is no MSK, no IoT Core, no
  Greengrass, no CloudWatch/X-Ray.
- **Gating risk is orthogonal to cloud:** the `real` backend is still **unverified against physical
  arm/camera/card hardware** (only the AGV `/base/*` path has run live). Cloud migration should not
  start until the local hardware loop is proven — moving an unverified integration into a distributed
  event mesh multiplies debugging cost.

**Key architectural divergence — "SageMaker endpoint" ≠ our vision.** The diagram's VISION INFERENCE
layer assumes a **self-hosted, trained CV model on a SageMaker GPU endpoint**. Our differentiator is
the opposite: **Claude Opus general vision, zero per-app training**. So for us that box maps to a
**Bedrock Claude invocation** (already the `bedrock` toggle), NOT SageMaker. Keep SageMaker only if we
later host an auxiliary open-vision/OCR model to push more validation into the 0-LLM tiers (relevant
to the camera-frame Tier-1 gap — see [[camera-vision-test-2026-07-16]]). Adopting SageMaker as the
primary path would discard the training-free advantage.

**Compatibility by layer (what's ready / what changes):**
- **Storage → S3 / RDS:** HIGH. `S3Storage`, `SQSImageQueue`, `db_url` swap all present. Change: point
  `s3_bucket`/`sqs_queue_url`/`db_url` at real resources; migrate SQLite rows to Postgres once.
- **Vision → Bedrock:** HIGH. One flag. Change: IAM role for Bedrock; confirm `claude-opus-4-8` model
  id in the target region.
- **Web API + WS → ECS + API Gateway:** MEDIUM. FastAPI containerizes cleanly, but the **in-process
  broadcaster** must move to a shared bus (Redis pub/sub or API Gateway WS) before the web tier can
  scale beyond one instance; today WS clients must hit the same process running the suite.
- **Orchestration → Fargate + Redis + Lambda scheduler:** LOW. `supervisor` is single-host threads;
  agent state is in-memory. Needs externalized state (Redis) and a per-suite container model.
- **Robot comms → IoT Core / API Gateway command dispatch:** LOW. We assume a **LAN-reachable robot
  HTTP server** (matches the diagram's "REST receiver" edge box) and drive it directly. The
  MQTT/IoT-Core command path + ACK-over-MSK is unbuilt. For a lab and many customer sites, direct REST
  (or REST-over-VPN) may stay simpler than MSK; MSK/IoT earns its keep only at real fleet scale.
- **Ingestion/stream (MSK, Lambda telemetry) & Observability (CloudWatch/X-Ray):** NONE yet. Net-new.
- **Security (VPC/IAM/Secrets Manager/WAF/mTLS):** LOW. Today: `.env` secrets, `CORS *`, no auth. All
  net-new for production.

**Main advantages of the AWS stack (why it's worth it later):** horizontal scale (one Fargate
container per suite → many kiosks/robots concurrently, vs our single-host thread pool); decoupling &
resilience (MSK/SQS buffer robot events so a slow consumer or restart doesn't drop telemetry);
managed durability (S3 for images/sensor bags, RDS for results — no local disk/SQLite ceiling); elastic
vision cost (Bedrock/SageMaker autoscale vs a fixed box); security & compliance posture for customer
demos (IAM per service, Secrets Manager, WAF, IoT mTLS with per-robot certs); observability (CloudWatch/
X-Ray traces, command-latency & inference-SLA alarms) we currently approximate with `print()` + WS
events; and OTA/edge management via Greengrass. Trade-off to weigh: MSK + SageMaker are heavyweight for
current lab scale — a leaner first cloud step (ECS + Bedrock + S3 + RDS + SQS, deferring MSK/IoT/
Greengrass) captures ~80% of the benefit at a fraction of the ops cost.

**When we do migrate — regression-safety rules (nothing here changes local behaviour):** every cloud
hook stays behind the existing `*_backend` toggles defaulting to local; add cloud config only via
`Settings` (never `os.environ`); externalizing the broadcaster must keep the in-process path as the
default so playwright/demo runs are byte-identical; keep `local`/`playwright` fully functional so the
lab loop never depends on cloud. See [[aws-readiness-2026-07-20]].

## Conventions

- Add config via `vision_agent/config.py` `Settings` (never read `os.environ` directly).
- New robot capability → add the function to **all three** backends (`stubs.py`,
  `playwright_stubs.py`, `real_robot.py`) with an **identical signature**; agent code stays backend-agnostic.
- New API route → add to `api/main.py`; new persisted field → add to `api/models.py` and an
  idempotent migration in `api/database.py`.
- New app screen → no code change needed; re-run the App Explorer (Claude vision discovers elements).
- Windows-centric repo; UTF-8 stdout reconfiguration is intentional.
