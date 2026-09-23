# Progress log — robotic-vision-agent-claude

> Archived, change-by-change history moved out of `CLAUDE.md` to keep that file within its
> size budget. See the **"Recent milestones"** subsection in `CLAUDE.md` for the high-level
> arc and the still-true invariants; this file is the append-only detail behind each item:
> every fix, run, and root-cause from **2026-08-21 onward**. The earlier 2026-06-25 →
> 2026-08-18 log is in [`DEBUGGING_HISTORY.md`](DEBUGGING_HISTORY.md).

---

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

### Recent progress (2026-09-09) — cloud-agnostic foundation [branch `cloud-agnostic-agent`]

A run-anywhere counterpart to `aws-based`: the SAME functionality using open-source, portable
components so ONE codebase deploys on **Azure, GCP, EC2 or on-prem** with no lock-in. **Claude is
unchanged** (remote call via `VISION_BACKEND=anthropic`|`bedrock`) — it powers App Explorer / Test
Planner / Auto-Repair exactly as before, wherever the code runs. Branch cut from `mvp-vision-agent`
(NOT `aws-based`), so this is the clean local core + agnostic infra bindings. Design + rationale:
`docs/CLOUD_AGNOSTIC_DECISION.md`; deploy + code walkthrough: `docs/CLOUD_AGNOSTIC_DEPLOY.md`.
**Nothing committed yet** (per the commit-only-when-asked rule).

- **Ports-and-adapters, config-only selection.** Every AWS-managed piece maps to an agnostic one,
  chosen purely from `vision_agent/config.py` (new **"CLOUD-AGNOSTIC DEPLOYMENT"** section):
  DynamoDB→**Postgres** (`persistence_backend`/`db_url`), S3→**MinIO/any S3-compatible**
  (`storage_backend`∈`{s3,minio,gcs,azure}` + `s3_endpoint_url`), API-GW-WS+Streams→**FastAPI WS +
  Redis pub/sub** (`event_bus_backend`), Step-Functions/AgentCore→**in-process now, containers/
  Temporal later**, AgentCore-Browser→**self-hosted Playwright** (already present). Full mapping
  table in the decision doc.
- **No-regression by construction — defaults reproduce the pure-local MVP byte-for-byte.** All new
  config fields default to local/sqlite/memory/single-tenant; new code paths are additive and
  config-gated. `tests/test_cloud_agnostic.py` asserts defaults==MVP + tenancy + event-bus behaviour
  (6/6 pass). Full suite: 104 passed, **same 8 pre-existing env-only failures** (template-match asset
  paths + vision tests needing a live Claude key) — all in code paths this branch never touched.
- **New `ports/` package** (hexagonal seams, all with lazy imports so no new hard deps until used):
  `ports/event_bus.py` — `EventBus` with `InMemoryEventBus` (default, = MVP single-server) and
  `RedisEventBus` (cross-replica realtime, the agnostic analogue of API-GW-WS + DynamoDB-Streams);
  `get_event_bus()` singleton built from config. `ports/tenancy.py` — `current_tenant()` /
  `set_current_tenant()` / `tenant_key()` + a FastAPI `tenant_dependency`; single-tenant mode makes
  `current_tenant()` the constant `default`, so call sites prefix keys/rows unconditionally and work
  in BOTH deployment models (pooled SaaS vs. dedicated).
- **Object store made cloud-agnostic:** `vision_agent/storage/aws.py::S3Storage` now takes
  `endpoint_url`/`region`/static-keys/`path_style` → one class drives AWS S3, MinIO, GCS (interop),
  Azure-via-S3; `get_storage()` routes `s3|minio|gcs|azure` to it, `local` unchanged. All extra knobs
  inert when blank (real-AWS default cred chain preserved).
- **Observability:** `GET /api/health` now returns a `platform` block (active vision / persistence /
  object-store / event-bus / tenancy / deployment backends) so any environment self-reports what it
  resolved to. Only route touched.
- **Multi-tenancy = one codebase, two go-to-markets** (see decision doc §2): **pooled SaaS** (many
  customers in our cloud, `MULTI_TENANT_ENABLED=true`, isolated by `tenant_id`-leading keys) and
  **dedicated/self-hosted** (each customer runs the same image+manifests in THEIR cloud;
  account boundary = isolation; `tenant_id` then separates their internal sub-teams). The
  cloud-agnostic packaging is what makes the dedicated model feasible — `aws-based` only supports
  pooled-in-our-AWS.
- **Docker↔K8s is a deploy-time switch (same image, same env keys).** New `Dockerfile` (uvicorn
  WITHOUT `--reload`, per the #1 gotcha) + `.dockerignore`; `docker-compose.yml` brings up the whole
  agnostic stack (postgres + minio + redis + app) on any Docker host; `deploy/k8s/` (namespace,
  configmap, secret.example, api-deployment **+ Service + HPA**, worker-deployment, ingress,
  kustomization, README) runs the identical image with env in a ConfigMap/Secret — stateless pods,
  HPA auto-scales the API tier, worker tier scales independently. **When to use which:** Compose for
  demo/pilot/a-few-tenants; K8s only when you need horizontal scale / independent API+worker tiers /
  per-tenant quotas (decision doc §3).
- **Packaging:** new `pyproject.toml` `[cloud]` extra (`psycopg[binary]`, `redis`, `boto3`) — all
  optional; `pip install -e .` unchanged. `storage_backend` Literal widened to include minio/gcs/azure.
- **Next (each incremental + still config-gated):** thread `tenant_key()` through object-store call
  sites + add a `tenant_id` DB column & filter; wire the Redis bus into the WebSocket broadcaster for
  multi-replica realtime; a queue-driven `SERVICE_ROLE=worker` consumer; optional Temporal
  orchestration; OpenTelemetry/Langfuse tracing; pgvector Memory. See the decision doc's "Next" list.

#### Update (2026-09-09, Phase 1a) — tenant-scoped storage call sites + Postgres confirmed

- **All blob STORAGE call sites are now tenant-aware, gated on `MULTI_TENANT_ENABLED`** — single-tenant
  is byte-identical to the MVP (all default-mode tests green), multi-tenant isolates every tenant's
  blobs under `.../tenants/<id>/...`. Primitives in `ports/tenancy.py`: `scoped(key)` (identity when
  single-tenant, else `tenants/<id>/<key>`) and `scoped_dir(base)`; filesystem roots in
  **`ports/paths.py`**: `app_map_path()` / `results_dir()` / `screens_dir()` / `test_plans_dir()`.
- **Object store:** `get_storage()` wraps the backend in `_TenantScopedStorage` **only** in
  multi-tenant mode (single-tenant returns the raw `LocalStorage`/`S3Storage`, so the existing type
  contract holds). Writes (`save`/`save_json`) are scoped; `load()` is NOT (it takes explicit,
  already-resolved paths like a captured screenshot). Covers `vision_agent/nodes/finalize.py` result
  docs + any future S3 write with zero call-site edits.
- **Filesystem wiring** (`api/main.py`): `_base_screens_dir`/`_run_results_dir`/`_next_run_number`
  reset now go through `tenant_paths`; **all 19 `settings.app_map_path` refs → `tenant_paths.app_map_path()`**;
  `test_runner/plan_cache.py` (`_cache_dir()`) and `test_runner/nodes/finalize_tests.py` scoped too.
  `screens_dir()` derives from `app_map_path()` so a tenant's map + its screenshots stay co-located.
- **Thread/subprocess boundaries handled** (contextvars don't cross them): the tenant is captured in
  the request context and (a) passed to `_execute_run(..., tenant_id)` which re-binds it at the top of
  the run thread, and (b) passed to `_run_explorer(..., app_map_path)` which sets `APP_MAP_PATH` in the
  explorer subprocess env so exploration writes the correct tenant's map. `app_map_path()` idempotently
  mkdirs the tenant dir so writers never hit a missing-parent error.
- **Postgres for persistent data — confirmed and hardened.** The ORM models
  (`api/models.py`) use only PG-safe SQLAlchemy types (`Integer/String/Text/JSON/Float/DateTime/
  ForeignKey`), so `db_url=postgresql+psycopg://…` works with **no model changes** (the `store/`
  rewrite the `aws-based` branch did for DynamoDB is NOT needed for Postgres — SQLAlchemy already
  abstracts the engine). `api/database.py::init_db()` now logs the active engine and **warns on a
  `PERSISTENCE_BACKEND` vs `DB_URL` mismatch** (e.g. label says postgres but URL is sqlite).
  `docker-compose.yml` + `deploy/k8s/configmap.yaml` set Postgres by default;
  `check_same_thread` is applied only for sqlite. Local dev stays on SQLite (`sqlite:///./management.db`).
- **Not yet scoped (documented next steps):** DB **rows** (add a `tenant_id` column + `current_tenant()`
  filter for row-level isolation — the blob level is done); the `supervisor`/`run_parallel.py` path.
  Multi-tenant remains opt-in/experimental until those land; single-tenant (the default, and every
  current deployment) is fully covered and unchanged.

#### Update (2026-09-09, Phase 1b) — row-level DB tenant isolation

- **Every business table now carries `tenant_id`** (`api/models.py::TenantMixin`, `declared_attr` →
  per-table `String(64)` col, default `"default"`, `server_default`, indexed). Mixed into all 8
  models (`KioskConfig`, `AppMapRecord`, `TestCase`, `TestRun`, `TestResult`, `Defect`,
  `DeviceConfig`, `RobotEvent`).
- **Isolation is enforced centrally by two global SQLAlchemy Session events**
  (`api/database.py::_register_tenant_scope`, registered once in `init_db`), so the ~40 query sites in
  `api/main.py` are UNTOUCHED and no filter can be forgotten:
  - `do_orm_execute` → `with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tid)` adds
    `WHERE tenant_id = current_tenant()` to every SELECT **and** ORM UPDATE/DELETE. ⚠️ Gotcha: the
    tenant must be resolved OUTSIDE the lambda and closed over as a literal (`tid = current_tenant()`)
    — a `current_tenant()` call *inside* the lambda raises `InvalidRequestError` (the lambda-SQL
    system extracts bound values without invoking the fn).
  - `before_flush` → stamps `tenant_id = current_tenant()` on `session.new` rows that didn't set it.
- **Both handlers no-op unless `MULTI_TENANT_ENABLED`** → single-tenant is byte-identical to the MVP
  (rows default to `"default"`, nothing filtered). Verified: multi-tenant → each tenant sees only its
  rows (auto-stamped); single-tenant → all rows visible. Full suite 111 passed (+2 DB isolation
  tests), same 8 pre-existing env-only fails.
- **Migration:** `_run_lightweight_migrations` idempotently `ALTER TABLE … ADD COLUMN tenant_id
  VARCHAR(64) NOT NULL DEFAULT 'default'` + `CREATE INDEX IF NOT EXISTS` on all 8 tables — existing
  rows become the `"default"` tenant. Verified on the live `management.db` (all 8 migrated; re-run
  skips). Works on SQLite and Postgres.
- ⚠️ **Known caveat (follow-up):** the unique constraints on `kiosk_id`/`test_id`/`run_id`/`alias`
  are still GLOBAL. A pooled DB where two tenants use the SAME id needs a composite
  `(tenant_id, <key>)` unique migration — straightforward on Postgres (the pooled target), awkward on
  SQLite. READ/WRITE isolation is complete; only cross-tenant id REUSE is constrained until then.
- **Request tenant binding is wired:** a pure-ASGI `_TenantASGIMiddleware` in `api/main.py` binds the
  `X-Tenant-Id` header per request (pure-ASGI, NOT `BaseHTTPMiddleware`, so the contextvar reliably
  reaches sync endpoints via anyio's threadpool context copy). No-op unless `MULTI_TENANT_ENABLED`.
  Combined with the run/explorer-thread binding, every DB query + blob write is now tenant-correct
  end-to-end through the API.

#### Update (2026-09-09, Phase 1c) — composite uniques · JWT · supervisor · Redis WebSocket

The four follow-ups from Phase 1b, all config-gated (single-tenant/in-memory = MVP), 116 tests pass:

- **Composite per-tenant unique constraints** (`api/models.py`): the natural keys are now unique
  `(tenant_id, key)` via `UniqueConstraint` in `__table_args__` (`kiosk_id`, `test_id`, `run_id`,
  `alias`), and the three FKs that referenced them (`app_maps.kiosk_id`, `test_results.run_id`,
  `defects.run_id`) are now composite `ForeignKeyConstraint(['tenant_id','<key>'], [...])`. The
  `TestRun.results ↔ TestResult.run` relationship carries an explicit `primaryjoin`+`foreign_keys`
  (composite FK → SQLAlchemy needs the join spelled out). Verified: two tenants can hold the same
  `run-1`; a same-tenant duplicate raises IntegrityError; the relationship loads. Fresh SQLite/PG get
  this from `create_all` (single-tenant behaves like the old global unique since tenant_id is
  constant); existing-pooled-PG migration SQL is in `docs/CLOUD_AGNOSTIC_DEPLOY.md`. ⚠️ SQLite FK
  enforcement is off by default, so the composite FK is enforced on Postgres (the pooled target).
- **JWT-claim tenant resolution** (`ports/tenancy.resolve_tenant_from_headers` + `_tenant_from_jwt`,
  `vision_agent/config` `tenant_jwt_*`): when `TENANT_JWT_ENABLED`, the tenant is read from a signed
  `Authorization: Bearer` JWT claim (`tenant_jwt_claim`, HS256 default, PyJWT **lazy-imported** — in
  the `[cloud]` extra), with the `X-Tenant-Id` header as fallback. A malformed/wrong-secret token
  returns None → header fallback (never 500s). The tenant middleware + WS endpoint both use this
  resolver.
- **Supervisor / CLI path:** `supervisor/worker.py` loads the app_map via `tenant_paths.app_map_path()`
  and each worker thread re-binds the tenant from the `TENANT_ID` env (contextvars don't cross the
  ThreadPoolExecutor boundary); `run_parallel.py` binds `TENANT_ID` at startup and writes its
  aggregate JSON under `tenant_paths.results_dir()`. Single-tenant unchanged (`./results`).
- **Redis-backed WebSocket for multi-replica realtime** (`api/main.py`): `_broadcast` now
  **publishes** the event to the event bus (`_ws_channel = ws:run:<tenant>:<run_id>`) instead of
  writing sockets directly; the WS endpoint **subscribes** this replica to that channel on connect
  (ref-counted per run_id, unsubscribed when the last local socket closes) and delivers to local
  sockets via `_deliver_local`. In-memory bus → identical single-replica behaviour (verified
  end-to-end: a TestClient WS client receives a `_broadcast`); Redis bus → an event published by the
  worker on ANY replica fans out to every replica's clients. Channel is tenant-namespaced. The WS
  handshake resolves its tenant from headers (the HTTP middleware doesn't see WebSocket scopes).
  ⚠️ This means K8s no longer needs sticky WebSocket sessions — any replica can serve any client.

#### Update (2026-09-09, Phase 2) — worker queue · Temporal · tracing · pgvector memory

The four AgentCore-layer analogues, all config-gated to MVP defaults (120 tests pass, same 8
pre-existing env-only fails). New `ports/` modules + a `worker/` package + an `orchestration/` package:

- **Queue-driven worker** (`ports/queue.py`, `ports/orchestration.py`, `worker/__main__.py`): the
  AgentCore-Runtime analogue. `start_run` now submits via `submit_run(payload, _run_job)` instead of
  spawning a thread directly. `TASK_QUEUE_BACKEND=inline` (default) → a same-process daemon thread
  (byte-identical to the MVP); `=redis` → the API `RPUSH`es a job and a `SERVICE_ROLE=worker` process
  (`python -m worker`, BLPOP loop) runs it. `_run_job(payload)` reconstructs the `RunRequest` and calls
  the SAME `_execute_run`, so every backend shares the run logic. **Composes with the Redis WS:** the
  worker publishes events to the bus, the API replica holding the client's socket delivers them — the
  API/worker split works end-to-end. `docker-entrypoint.sh` dispatches API vs worker by `SERVICE_ROLE`;
  compose gained a `worker` service; the k8s configmap sets `TASK_QUEUE_BACKEND=redis`.
- **Temporal orchestration (optional)** (`orchestration/temporal_app.py`): `ORCHESTRATOR_BACKEND=temporal`
  runs the suite as a durable Temporal workflow + `run_suite` activity that wraps the same `_run_job`
  (durability/retry/visibility around unchanged run logic). `python -m worker --temporal` hosts it.
  `temporalio` lazy-imported; default `inprocess` = the queue path. `[temporal]` extra.
- **Tracing** (`ports/tracing.py`): `TRACING_BACKEND=none|otel|langfuse`. `otel` = OTLP spans to any
  collector; `langfuse` = LLM-native traces via the LangChain callback handler. Wired at the ONE central
  LLM path — `vision_agent/llm.invoke_json` passes `config={"callbacks": llm_callbacks()}` (empty →
  plain `invoke`, so `none` is a true no-op). `span(name)` context manager for work units. Lazy SDKs,
  `[tracing]` extra.
- **pgvector Memory** (`ports/memory.py`): `MEMORY_BACKEND=none|chroma|pgvector` — the AgentCore-Memory
  analogue. `remember()/search()` over a Chroma or Postgres+pgvector collection (reuses the repair
  HuggingFace embedding singleton), **tenant-namespaced**. Wired (gated) into
  `app_explorer/nodes/finalize_map.py::_index_screens_in_memory` — each charted screen is indexed when
  enabled, so an agent can recall similar screens; `none` (default) = no-op, explorer unchanged.
  `[memory]` extra.
- All four surface in `/api/health` (`task_queue`/`orchestrator`/`tracing`/`memory`) and are documented
  in `.env.example` + the decision doc's component table. Nothing is imported or changed until selected.
- ⚠️ **Worker needs the same env as the API** (DB_URL, S3_*, REDIS_URL, ANTHROPIC_API_KEY). ⚠️ With
  `TASK_QUEUE_BACKEND=redis` the API only enqueues — a worker MUST be running or runs never execute
  (health still 200). ⚠️ `docker-entrypoint.sh` is CR-stripped in the Dockerfile (Windows-authored).

#### Update (2026-09-09, Phase 2b) — hardening + architecture doc

- **Per-test fan-out (Temporal):** `orchestration/temporal_app.py` `RunSuiteWorkflow` now runs one
  `run_test` activity PER id in parallel when `payload["fanout_test_ids"]` is set — each an INDEPENDENT
  sub-run (`<parent>::<test_id>`, its own filter) so parallel shards don't race on shared run state /
  global settings (run shards on separate workers, or activity-concurrency=1 on one worker). Falls back
  to the single `run_suite` activity otherwise. Worker registers both activities.
- **OTel spans around agent work:** `_execute_run` opens a root `agent.run.execute` span (manual
  enter/exit in the finally, no reindent); `vision_agent/llm.invoke_json` wraps each Claude call in a
  child `llm.invoke` span **labelled by role** (planner/explorer/validate/repair/…). No-op unless
  `TRACING_BACKEND=otel`; nests into an end-to-end per-run trace.
- **Memory-informed exploration:** `app_explorer/nodes/explore_screen._recall_similar_screens` queries
  `ports.memory.search` for semantically-similar prior screens and logs them (gated on `MEMORY_BACKEND`;
  never raises). Completes the loop with `finalize_map._index_screens_in_memory` (write side). Purely
  additive — does not alter screen identification.
- **Architecture doc:** `docs/Cloud_Agnostic_Architecture.docx` — a component-view diagram (tiers +
  AWS→agnostic mapping table) and a run sequence diagram (14-step start-to-finish use case), generated
  with PIL + python-docx. 120 tests still pass; single-node/single-tenant behaviour unchanged.

### Recent progress (2026-09-10) — first live GCP VM deployment (cloud-agnostic) + 4 deploy fixes

The `cloud-agnostic-agent` backend + the Kiosk POS were deployed and verified end-to-end on a **fresh
Google Compute Engine VM** (the first real run-anywhere deploy). Full runbook in
`docs/CLOUD_AGNOSTIC_DEPLOY.md` §6 "Provision & deploy on GCP (worked runbook)". **All fixes are
committed + pushed**; the VM clones from GitHub.

- **VM:** `e2-standard-4` (4 vCPU / 16 GB), `us-central1-a`, **Ubuntu 22.04 LTS x86/64**, `pd-balanced`
  50 GB, Standard provisioning, no time limit, graceful-shutdown ON, termination action = **Stop**.
  Docker + compose via `get.docker.com`. This is the recommended all-in-one size (app + Postgres +
  MinIO + Redis on one box). For K8s use it as a **node-pool size** (2+ nodes) with managed datastores,
  not one node — see the deploy doc's sizing table.
- **Backend (`cloud-agnostic-agent`, HEAD `949f425`):** whole compose stack up
  (postgres+minio+redis+app+worker); `GET /api/health` 200 with the platform block resolving
  `anthropic / postgres / minio / redis(event bus + task queue) / inprocess / docker / api`. Playwright
  Chromium verified in-container (`151.0.7922.34`). Runs from `~/robotic-vision-agent-claude`; the only
  hand-created file is `.env` (holds `ANTHROPIC_API_KEY`, gitignored — NOT in the clone).
- **Four deploy-blocker fixes (each committed + pushed, defaults unchanged so no MVP regression):**
  1. **`python-multipart`** added to core `pyproject.toml` deps (`4e78efd`) — the clean image lacked it,
     so FastAPI crashed at import validating the `/api/test-cases/upload` Form route. (The historical
     "imported-but-undeclared" gap — now closed for the last offender.)
  2. **`ROBOT_BACKEND=playwright`** set in compose for **app + worker** (`e32cb9f`) — was defaulting to
     `demo`; exploration + queued test runs both need live Chromium.
  3. **`PLAYWRIGHT_HEADLESS` config flag** (`vision_agent/config.py`, default **False** = desktop demo
     window) + `chromium.launch(headless=settings.playwright_headless)` + compose sets it `true`
     (`949f425`) — a headless VM has no X server, so the hardcoded `headless=False` died with "Missing
     X server or $DISPLAY". Tap/screenshot math is display-independent → headless is byte-identical.
  4. **POS `card-service/Dockerfile`** seeds `RUN echo '{}' > /app/cards.json` instead of
     `COPY cards.json` (`f1973e2` on `pos-cloud-agnostic`) — `cards.json` is gitignored runtime data,
     absent from a fresh clone, so the COPY failed the build. `server.js` tolerates a missing file.
- **POS on the VM = the EXPANDED feature set.** New branch **`expanded-cloud-agnostic`** (on
  `srik-g/robotics-kiosk-pos`) = `expanded-version` (more features) **merged with** the
  `pos-cloud-agnostic` packaging (clean merge — packaging only adds files). `expanded-version` and
  `pos-cloud-agnostic` are both left intact. The VM's POS clone (`~/robotics-kiosk-pos`) tracks
  `expanded-cloud-agnostic`; built single-origin (nginx :80 serves SPA + proxies `/api/`→card-service),
  `VITE_CARD_SERVICE_URL` baked from `PUBLIC_BASE_URL=http://<VM_EXTERNAL_IP>`.
- **Interconnect:** the backend tests the POS by URL. Both on one VM → the kiosk URL is the VM's
  **internal** IP on :80 (`hostname -I`), reachable from inside the app container; `POST /api/explore`
  upserts it. GCP firewall opens only 80/443 by default (datastore ports 5432/6379/9000/9001 stay
  VM-internal, correct); expose 8001 only via a rule **restricted to your IP** if a remote Studio needs it.
- **⚠️ RESUME POINT (paused 2026-09-10, continuing in a few hours):** last action was pushing the
  headless fix (`949f425`). **On the VM, next:** (1) backend `git pull && docker compose up -d --build`
  (fast — source layer only), confirm `settings.playwright_headless == True`; (2) POS already switching
  to `expanded-cloud-agnostic` (`git pull && docker compose up -d --build` may still be running);
  (3) **re-run `POST /api/explore`** against `http://<VM_INTERNAL_IP>/?screenLayout=standard&flowMode=full`
  — it had failed ONLY on the headless bug, now fixed. The expanded POS may use different
  screens/`screenLayout`/`flowMode`, so adjust the kiosk_url to the intended RPS/VPS view if the map
  looks wrong. **After a green exploration:** test execution — `POST /api/test-cases/upload` (Excel),
  then `POST /api/runs`. Operator UI (`kiosk-test-studio`) is NOT deployed (no cloud-agnostic branch
  yet) — interact via API/curl, or point a Studio at `http://<VM_EXTERNAL_IP>:8001`.

### Recent progress (2026-09-11) — single-VM run execution fix + data-store access

- **Run execution on the VM now runs IN-PROCESS (`TASK_QUEUE_BACKEND=inline`) + in-process event
  bus (`EVENT_BUS_BACKEND=memory`)** — byte-identical to the local MVP. The Redis API/worker split
  (`task_queue=redis` + a `SERVICE_ROLE=worker` service) was the cause of a run stuck at **"pending"**
  with an empty live feed and no results: on a single VM, if the separate worker isn't consuming the
  queue the job never executes. The `worker` service was removed from `docker-compose.yml`; the
  Redis queue/pub-sub path stays available for **multi-replica K8s scale-out** (`deploy/k8s/`), which
  is the only place it's needed. No regression: inline+memory is exactly what every local run used.
- **App_map path regression fixed** (`run_explorer.py`): the explorer now honors the `APP_MAP_PATH`
  env the API passes it, so it writes the map to the same path `GET /api/app-map` reads. (It had
  hardcoded `"app_map.json"`; relocating `APP_MAP_PATH=/app/data/app_map.json` left the Studio App
  Map empty.) Rule reinforced: when relocating a path, verify BOTH the writer and the reader resolve
  to it. `[[no-regression-rule]]`
- **Verify false-positive fixed (TC-RPS-001):** the error-banner guard `get_page_error_text`
  (playwright_stubs) selects `[class*="danger" i]` and returned the **"Sign Out"** button on the
  authenticated products screen, failing a CORRECT `products` verify ("misclassified a Sign Out
  control as an error banner"). An error banner is a MESSAGE, not a control — the scan now SKIPS any
  element inside `button/a/[role=button|link|menuitem|tab]/input/select/label`. Genuine error-message
  divs ("No card found" / "not issued" / declined) still match → no regression to VPS card-error detection.
- **Auto-Repair deps declared (`[repair]` extra) — fixes "No module named 'langchain_community'":**
  `repair_agent/parse_code_and_store.py` imports `langchain_community` (HuggingFaceEmbeddings + Chroma)
  + tree-sitter; installed in dev but never declared, so a clean image failed. Added a `[repair]` extra
  (langchain-community, chromadb, sentence-transformers, tree-sitter[-typescript]); the Docker image now
  builds `.[cloud,playwright,repair]` (sentence-transformers pulls torch → larger image). ⚠️ After
  checking out a demo bug branch, REBUILD the RAG index before running a repair (existing rule).
- **Demo bug for Auto-Repair on the cloud-agnostic POS:** re-planted the cross-kiosk transaction bug on
  **`expanded-cloud-agnostic`** (`srik-g/robotics-kiosk-pos`, `createApprovedStatusFromCardNumber` outer
  guard → `if (balanceAfter !== undefined && !issuedSmartCard)`) so an RPS purchase on an ISSUED smart
  card skips `recordCardTransaction` — identical to `demo/rps-vps-txn-bug`. No `_demo_fallback_patch`
  (genuine LLM test); the fix reverts the guard. Test flow: checkout is already on this branch → rebuild
  the RAG index → run TC-VPS-009-style check after an RPS purchase → auto-repair diagnoses the guard.

#### Auto-Repair on the VM — enabling the RAG + build + git path

Auto-Repair indexes the POS code, edits it, type-checks + `vite build`s it, and prepares a PR — so the
BACKEND container needs the POS **source + node + git** (it had none). Wired into the VM image/compose:
- **POS repo mounted rw** at `/repos/pos` (`${POS_REPO_DIR:-../robotics-kiosk-pos}`) → `REPAIR_CODEBASE_DIR=/repos/pos`.
- **node 20 + git in the backend image** (node copied from `node:20-slim`; git via apt) for the
  `tsc -b`/`npm run build` verification and the repo-state guard/commit.
- `REPAIR_PERSIST_DIR=/app/data/chroma_code_db` (index persists under host-mounted `./data`),
  `REPAIR_PR_BASE=expanded-cloud-agnostic`, `GIT_AUTHOR_*`/`GIT_COMMITTER_*` identity, and the entrypoint
  marks `/repos/pos` a git `safe.directory` (the mounted repo is owned by the host user).
- **Auto-raise the PR (`REPAIR_AUTO_PR=true`, default now):** after a green build, `open_pull_request`
  commits the fix on a `repair/<test>-fix-<id>` branch, **pushes** it to origin, and **creates a real
  PR via the GitHub REST API** (`gh` isn't installed, so `_create_pr_via_api` in `repair_failed_test.py`
  posts to `/repos/{owner}/{repo}/pulls`; a 422 "already exists" returns the existing PR). Needs a
  **`GITHUB_TOKEN`** (repo scope) in `.env` — the entrypoint applies it to `git push` via
  `url.insteadOf` (transport-time, not stored), and `settings.github_token` drives the API call.
  Falls back to the prefilled compare URL if the token is blank. `POST /api/repair/{id}/open-pr`
  (manual) and `/delete-pr` still work. Set `REPAIR_AUTO_PR=false` to keep repairs local.
- **One-time on the VM** — populate node_modules in the mounted repo (the build step needs it), then
  rebuild the index against the on-disk (buggy) code:
  - `docker run --rm -v ~/robotics-kiosk-pos:/app -w /app node:20-alpine npm ci`
  - Studio "Rebuild index" (or `POST /api/repair/index`).
- ⚠️ Add `GITHUB_TOKEN=<repo-scoped PAT>` to `.env` on the VM for the push+PR. The token must allow
  pushing to `srik-g/robotics-kiosk-pos`. `.env` is gitignored — never commit the token.

#### Data-store access (cloud-agnostic VM deployment)

Two stores + a durable object mirror; browse them during a demo:

- **PostgreSQL** (relational metadata — kiosks, test cases, runs, results, defects):
  - In-container host `postgres:5432`; on the VM `localhost:5432`. **DB `kioskqa` · user `kioskqa` ·
    password `kioskqa`** (set in `docker-compose.yml`; change for anything beyond a demo).
  - Browser: **Adminer** at `http://<EXTERNAL_IP>:8081` → System **PostgreSQL**, Server `postgres`,
    Username/Password/Database all `kioskqa`. CLI: `docker compose exec postgres psql -U kioskqa -d kioskqa`.
- **MinIO** (the S3-equivalent object store — mirrored app_map, screenshots, plans, results):
  - Console **`http://<EXTERNAL_IP>:9001`** · **user `minioadmin` · password `minioadmin`** · bucket
    **`kioskqa`**. S3 API on `:9000` (`S3_ENDPOINT_URL=http://minio:9000`, path-style).
  - Populated only when `ARCHIVE_TO_OBJECT_STORE=true` (default in the VM compose); keys mirror the
    working layout (`app_map.json`, `screenshots/…`, `test_plans/…`, `results/<run>/…`).
- **Local working files** (what the agents read/write; MinIO mirrors these): host
  `~/robotic-vision-agent-claude/data/` (bind-mounted to `/app/data`).
- Open `tcp:8081`/`tcp:9001` in the GCP firewall **restricted to your IP** — never `0.0.0.0/0`.
  These are demo credentials; rotate them (compose env + a real secret store) for any shared/hosted use.

#### Live-browser viewer (noVNC) — watch Chromium during exploration/execution

A headless VM has no display, so Chromium can't show a window. `ENABLE_VNC=true` (default in the VM
compose) starts a virtual display (**Xvfb**) that headed Chromium renders onto, exposes it via
**x11vnc**, and serves a browser VNC client (**noVNC**) at **`http://<EXTERNAL_IP>:6080/vnc.html`** —
so you can watch App Explorer and test execution live. Implemented in `docker-entrypoint.sh` (started
before uvicorn; forces `PLAYWRIGHT_HEADLESS=false`) + the `xvfb/x11vnc/novnc/websockify` packages in
the `Dockerfile`; port `6080` published by the `app` service. Open `tcp:6080` in the firewall
(restrict to your IP; the VNC has no password). Set `ENABLE_VNC=false` + `PLAYWRIGHT_HEADLESS=true`
for the lightweight headless mode (no viewer). Tap/screenshot math is display-independent, so headed
vs headless is behaviourally identical — no regression.

### Recent progress (2026-09-12) — auto-repair PR verified end-to-end + two fixes

- **Auto-repair full loop confirmed on the VM:** failed run → diagnose → apply → tsc/vite build →
  push `repair/*` → **PR created via the GitHub API** (`_create_pr_via_api`, `REPAIR_AUTO_PR=true`,
  `GITHUB_TOKEN` in `.env`) → the fix on the repair branch makes the test pass. See the Auto-Repair-on-VM
  subsection above.
- **Faster rebuilds (Dockerfile):** the dependency layer is now keyed only on `pyproject.toml` (deps
  installed against a STUB `vision_agent/__init__.py`; real source arrives via `COPY . .`). Editing app
  source no longer re-runs pip/Playwright/node — a source-only rebuild is CACHED at the pip layer.
  (Also: config *values* are env-settable, so `docker compose up -d` without `--build` picks them up.)
- **Studio: repair view auto-opens inline when the popup is blocked**
  (`../kiosk-test-studio` `studio-cloud-agnostic`, `LiveMonitor.tsx`). `window.open` fired from the run
  WebSocket handler is not a user gesture → popup blockers silently block it (returns `null`), which is
  why the auto-launch "stopped working". Now, when blocked, the repair opens INLINE as an in-tab overlay
  (`<AutoRepair standaloneRepairId>`) — so it launches automatically with no popup permission/click. If
  popups are allowed, behaviour is unchanged (separate window). No regression: the banner + "Open repair
  window" button remain (the button also falls back to inline). Rebuild the studio container to apply.

### Recent progress (2026-09-12, run-9 GCE) — TC-RPS-003 card-number-not-entered + 3 fixes

TC-RPS-003 (RPS mock-card payment) failed with "card number not entered — all fields selected", and
auto-repair then produced a FALSE-POSITIVE commit. Root cause chain + fixes:
- **Root cause:** the app_map `payment` screen charts the "Use Mock Card"/"Start Card Reader Session"
  BUTTONS but NOT the mock-card-number input (revealed only AFTER clicking "Use Mock Card" — the
  explorer never completed that reveal). The Tier-2 planner therefore **guessed** a
  `mock_card_number_input` element with fabricated coords (766,772). At execution the coord focus
  landed on nothing → `type_text`'s Ctrl+A selected the whole PAGE (the "all fields selected"
  symptom) → the number was dropped, yet the step "succeeded" (no Tier-3). The later payment verify
  then failed.
- **Auto-repair commit `20f7740` was a FALSE POSITIVE** — it reordered `refreshCard()` before the
  empty-number guard in `confirmMockCard`, but `refreshCard()` pulls a card's *balance*, not the input
  value, so `if (!digits)` is unchanged; a cosmetic no-op. The app is correct; the failure was
  harness-side (number never typed). Delete that PR.
- **Fix 1 — exploration mock card = `0005322931`** (`demo_card_number`, was the stale `4111…`). The
  expanded POS issues `0005322931` as the mock card (`App.tsx:1340-1341 issueCard('0005322931','mock')`),
  so the explorer's typed/`?demoCard=1` number now matches → the "Use Mock Card" reveal completes and
  the mock-card input + confirm get charted (with testids) → Tier-1/2 types correctly, no guessing.
  **Re-explore RPS after deploying** so the payment reveal is charted.
- **Fix 2 — uncharted plan elements now route to Tier-3** (`run_vision_step._element_charted`): a
  `type`/`tap` step whose `element_id` is NOT in the app_map for its charted screen (a planner guess)
  is treated like no-coordinates → hands off to Tier-3 vision instead of focusing fabricated coords and
  silently mis-typing. Conservative: fires ONLY when the screen IS charted but the element is absent
  (charted elements unaffected → no regression). This is the always-works dynamic-data fallback.
- **Fix 3 — `run_console.log` was empty in MinIO** (`api/main.py` run `finally`): `archive()` ran
  BEFORE the tee'd console log was flushed/closed, uploading an empty file. Moved the archive to AFTER
  the log is closed, so the object-store copy has the full console log. (The host `./data` copy was
  always complete; only the archived copy was empty.)

### Recent progress (2026-09-12, run-2 GCE) — DETERMINISTIC plans (same test → same plan every time)

After re-exploring, TC-RPS-003 got a DIFFERENT plan than before — the new one skipped login entirely
(first step `verify products` → failed on step 1). Root cause was TWO compounding non-determinism
sources; both fixed for ALL test cases (permanent, config-driven, no regression):
- **`get_llm`/`get_explorer_llm`/`get_fast_llm` set NO temperature** → Claude defaulted to 1.0, so the
  Tier-2 planner (and verify/Tier-3/repair-diagnose) produced different output for identical inputs — a
  re-plan could drop the login steps. **Fix:** new `llm_temperature` setting (**default 0.0**) applied to
  all three (Anthropic + Bedrock). Deterministic generation is the right default for a reproducible QA
  tool; raise it only to want variety. This is the primary lever for "same test → same plan".
- **`app_map.version_hash` hashed `explored_at` (a timestamp)** → EVERY re-exploration bumped the plan
  cache key → every cached plan was regenerated (by the then-non-deterministic planner). **Fix:** the
  hash is now **content-based** (screen ids + each screen's sorted element ids, no timestamp, coords
  excluded so vision jitter doesn't churn it). A re-explore that charts the SAME structure now yields the
  SAME version → the cached plan is reused verbatim; it re-plans ONLY when a screen/element is actually
  added or removed. Verified: identical-structure maps with different `explored_at`/element-order → same
  hash; adding an element → different hash. (One-time effect: existing cached plans re-key once and
  regenerate deterministically.)
- **Hard auth rule in `PLAN_FROM_MAP`** (`test_runner/prompts.py`): the app starts LOGGED OUT, so unless
  a raw step/app_map proves a session, the plan MUST begin with the login sequence before any post-login
  step — never open with a post-login `verify`. Belt-and-braces for correctness now that generation is
  deterministic.
- **Note on the run's auto-repair:** it filed a defect + attempted a fix, but this was a PLANNER failure
  (login skipped), not an app bug — the user correctly does not expect auto-repair to "fix" it. The plan
  determinism fixes above are the real fix; the earlier `20f7740` (TC-RPS-003) is likewise a false
  positive to delete.
- ⚠️ **`temperature` is DEPRECATED on Claude Opus 4.8** — sending it 400s ("`temperature` is deprecated
  for this model"). The determinism commit above briefly set `temperature=0` and broke plan generation.
  Fixed: `llm_temperature` now defaults to **None** and `vision_agent/llm._temp_kwargs()` OMITS the
  param unless a numeric value is set, so it is never sent to Opus 4.8. Plan consistency does NOT depend
  on temperature — it comes from the content-based `version_hash` (cached plan reused verbatim across
  re-explores) + the auth-first prompt rule. (Set a numeric `LLM_TEMPERATURE` only for an older model.)
- **Reset now clears the object store too (cloud parity).** `/api/reset` deletes DB rows (works on
  Postgres) + local files as before, and now also deletes the ARCHIVED copies in MinIO/S3 via
  `ports.archive.delete_prefix("test_plans/", "results/", "screenshots/run-")` — scoped to exactly what
  Reset removes, so exploration artifacts (`app_map.json`, `screenshots/exploration_*`) are preserved.
  No-op unless `ARCHIVE_TO_OBJECT_STORE`. (Single-tenant key layout; multi-tenant tenant-prefixing is a
  follow-up.)

#### Plan-gap recovery is GENERIC (Claude planning + Tier-3) — no app-specific step injection

The planner (Claude, Tier-2) sometimes OMITS a navigation step on regeneration — first observed as a
skipped login (`verify products` first → fail), then as a skipped cart-open (`add to cart` → `verify
cart` with no `tap cart` between → `expected 'cart', got 'products'`). These are the SAME class:
a missing navigation action. **An earlier fix (`_ensure_login_prefix`, deterministic login-step
injection) was REVERTED** — hardcoding the login sequence (or any per-screen step) is tightly coupled
to one app/flow and does not generalise. The design principle: plans come from Claude, and **Tier-3
vision recovers whatever the plan misses** — so it works for any app with no rework. Two generic fixes:
- **Prompt: NAVIGATION COMPLETENESS rule** (`test_runner/prompts.py` `PLAN_FROM_MAP`): a screen never
  appears on its own — every step that runs on a screen different from the previous one MUST be
  preceded by the explicit tap that opens it (from the app_map transitions); never `verify <screen>`
  unless an earlier step navigated there. States login and cart-open only as *instances* of the general
  rule, not special cases.
- **Execution: a wrong-SCREEN verify failure now HANDS OFF TO TIER-3** instead of failing terminally
  (`run_vision_step.py`, `settings.verify_wrong_screen_recovers`, default True). A verify that fails
  because we're on a DIFFERENT screen than expected (no `expected_text`) is almost always a missing
  navigation step → Tier-3 navigates to the objective from the current screen and continues. Text/value
  mismatches on the RIGHT screen (`expected_text` set, e.g. TC-VPS-009 'PURCHASE') stay TERMINAL — a
  genuine assertion Tier-3 can't fix by navigating (so run-20's anti-wandering intent holds for real
  assertions). Verified classification: cart-skip & login-skip → recover; VPS-009 text mismatch →
  terminal. This makes login-skip, cart-skip, and any future plan gap self-heal via vision, generically.
- Temperature 400 confirmed fixed (`llm_temperature=None`; a direct Claude call returns text;
  langchain_anthropic 1.7.2 omits the param). Plan CONSISTENCY still comes from the content-based
  `version_hash` (cached plan reused) — regeneration is rare, and when it happens the prompt + Tier-3
  recovery keep it correct.

### Recent progress (2026-09-12, run-2 GCE) — Tier-3 UX + repair-stage status (4 fixes)

A TC-RPS-003 run PASSED via Tier-3 recovery (the wrong-screen handoff works), surfacing four polish items:
- **Tier-3 wandering reduced (generic prompt).** On the cart screen Tier-3 invented a `card_button` and
  clicked a LEFT-MENU item at (81,822) before the correct `proceed_to_card_payment_button`, then
  corrected. Added a "STAY ON THE DIRECT PATH" rule to `vision_agent/prompts.py::PLAN_STEPS`: prefer the
  primary forward/content action, do NOT tap sidebar/nav/menu items unless the task needs that section,
  prefer main-content over side-menu when ambiguous. Generic (any app), best-effort (vision is not
  perfect) — no behaviour coupling.
- **Live feed no longer freezes during Tier-3** (issue: it stopped at the failed step for several silent
  seconds). Cause: `LiveMonitor.tsx` had NO branch for `'log'` events, so Tier-3's progress logs were
  dropped. Added a `'log'` branch (renders the message), and the backend now emits feed `log` events at
  the Tier-3 handoff and on each Tier-3 step-through/re-plan (`run_vision_step.py`).
- **Recovered steps are shown as RECOVERED, not failed.** When Tier-3 recovers, `step_results` still
  contains the Tier-1/2 step that failed → the drill-down showed it red under a PASSED run. Backend flags
  it (`recovered_by_tier3` per step + `tier3_recovered` on the result); the UI (`Results.tsx` +
  `LiveMonitor.tsx`) renders a failed step **in a PASSED run** as amber "⟲ recovered by Tier-3" (icon,
  label, "Initial attempt: … — recovered by Tier-3") plus a card badge "⟲ Tier-3 recovered N". Generic:
  the signal is simply *failed step + passed run* (the flag is a bonus), so it needs no per-app logic.
- **Auto-Repair sub-tasks no longer stick on "Working" when a repair errors.** `apply_node` emitted
  `apply,"running"` then `apply_patch` RAISED (e.g. "Patch find-text was not found"), so the stage never
  advanced. Now `apply_node` emits `apply,"failed"` on the exception, AND `api/main._repair_fail_running_stages`
  flips EVERY still-"running" stage to "failed" whenever a repair job errors (generic — covers apply,
  build, diagnose, …). The AutoRepair page already renders `failed` (✕ red), so the pipeline now shows
  exactly where it stopped alongside the "repair incomplete" banner.

### Recent progress (2026-09-12, run-3 GCE) — Tier-3 BRIDGES the gap then RESUMES the plan; stop-at-objective; no weak fuzzy misclick

A TC-RPS-003 run (plan had NO login step; login was uncharted) PASSED via Tier-3, but with two design
flaws the user flagged: (1) after Tier-3 completed the payment it clicked **"Card Inventory"** in the
left menu (a bad fuzzy match `pay_with_mock_card_button`→`Card Inventory`, keyword overlap=1 on "card"),
restarted, failed, and triggered an **unnecessary auto-repair**; and (2) after login was resolved, Tier-3
kept driving **every** subsequent step by vision instead of resuming the fast structured plan. The user's
principle: *"Tier-3 should be invoked only if it is going off the test plan at each step"* — bridge only the
off-plan gap, then resume Tier-1/2. Three GENERIC fixes (no app-specific ids/steps), 120 tests pass (same 8
pre-existing env-only fails):

- **Bridge-and-resume for a wrong-screen verify** (`test_runner/nodes/run_vision_step.py::_execute_structured_plan`).
  A `verify` that lands on a DIFFERENT screen than expected **with no text/value assertion** (`_screen_only`,
  gated by `verify_wrong_screen_recovers`) is almost always a MISSING NAVIGATION/entry patch in the plan (e.g.
  the run reset to login but step 1 expects `products`). Instead of failing the whole plan → a full Tier-3
  takeover, it now runs the SAME bounded `_run_inline_vision` segment used for `vision_required` (vision only
  until the screen advances/stalls), **RE-VERIFIES**, and on success records the step as `method:"tier3_bridge"`
  (+ `recovered_by_tier3`) and **`continue`s the structured loop** — so the 0-LLM plan drives every step it can
  and stops at its OWN final verify. If the bridge/re-verify doesn't reach the expected screen it falls through
  to the existing outer Tier-3 resume (safety net) — so worst case is identical to before (no regression), best
  case the fast plan resumes. This is the direct fix for "Tier-3 ran vision for every step after login".
- **Tier-3 stops at the objective — no wandering after completion** (`_run_tier3_continue`). The step-through
  loop now **breaks the moment the VisionAgent reports `outcome=="success"`** (its finalize verdict is the
  authoritative "done" signal), and goal-screen matching uses `_screens_equivalent` (separator/substring
  tolerant) instead of `==`. This is why payment could complete yet the loop ran again and clicked "Card
  Inventory": the DOM landed on `payment_successful` while the plan's last verify named `order_result`, so a
  name-only `==` never matched and it looped. The success verdict + equivalent match stop it cleanly.
- **Fuzzy element matcher rejects weak cross-purpose matches** (`vision_agent/nodes/execute.py::_find_element`,
  step-5 keyword-overlap fallback). A **multi-word** target now requires **≥2** shared significant words; only a
  single-word target may match on 1. So `pay_with_mock_card_button` no longer matches `Card Inventory` on the
  lone shared word "card" — a too-weak match is treated as **not found**, so the caller (Tier-3 vision /
  uncharted-element handling) locates the real element instead of tapping an unrelated nav link. Generic — no
  app-specific ids; it just refuses to bet on one weak keyword.
- **Consequence for the unnecessary auto-repair:** with the bridge-and-resume + stop-at-objective + no-misclick,
  TC-RPS-003 now completes and the run PASSES, so `auto_repair_on_failure` never fires — the spurious repair was
  a downstream symptom of the wander-and-fail, not a separate bug. **RESTART the backend** (uvicorn has no
  `--reload`) to load these `run_vision_step.py` / `execute.py` changes.

### Recent progress (2026-09-12, run-7 GCE) — Tier-3 SHARES the plan's input values (no more invented '1234')

`run_console (3).log`: the bridge-and-resume worked (login/cart gaps bridged, steps 1–8 ran on the fast plan
and typed the RIGHT card `0005322931`), but the final `order_result` verify failed on the payment screen and
the handoff to Tier-3 then typed an **invented `1234`** at the mock-card field → "card 1234 is not issued" →
Tier-3 re-planned against 1234 forever (RETRY 1/2/3) and the run FAILED. Root cause: **Tier-3 had no way to
learn the card number.** The value lived in a plan `type` step (`type: 0005322931`) that was *behind* the
handoff index, so it was in neither `captured` (runtime-captured values only) nor the remaining-steps hint —
Tier-3 saw a card input with no value and guessed. The user's ask: *the 0-LLM plan and Tier-3 must share data
(input values, screen state) so a field the plan already fills is entered with the plan's exact value, not a
re-invented placeholder.* Fixed generically (120 tests pass, same 8 pre-existing env-only fails):

- **New `_plan_input_values(plan_steps, captured, scenario, credentials)`** (`run_vision_step.py`) collects
  every literal `type` value from the WHOLE structured plan, **keyed by the step's `element_id`** (falls back
  to screen id / positional key), resolving credential + `{{captured.*}}` placeholders. Blanks and unresolved
  placeholders are dropped; the **configured password is excluded** (already threaded via `cred_hint`, so it
  never appears as a plain field value — the email is kept as non-secret signal). Generic — no app-specific ids.
- **Threaded into BOTH recovery paths so plan⇄Tier-3 share data:**
  - `_run_tier3_continue` adds an **"INPUT VALUES the test plan specifies"** block to the vision task
    (alongside the existing captured-values + remaining-steps hints): *use the EXACT value for that field;
    NEVER invent or type a placeholder such as '1234'.* So the full Tier-3 planner emits `type: 0005322931`.
  - `_run_inline_vision` gained a `plan_values` param; it merges `{**plan_values, **captured}` (captured wins —
    it's live) into the `known` dict used for BOTH the fast path (`_inline_vision_fast`'s provided values) and
    the full-agent `cap_hint`. Both `_execute_structured_plan` call sites (the `vision_required` sentinel and
    the wrong-screen **bridge**) pass `plan_values=_plan_input_values(plan steps, captured, …)`, computed at
    call time so a value captured mid-run is included.
- **Effect:** whenever a recovered/bridged screen needs an input the plan already specifies, vision reuses the
  plan's exact value (the issued card `0005322931`) instead of inventing `1234` — the plan and Tier-3 now
  operate in sync, sharing input values (and, already, screen state via the no-reset handoff + `captured`).
- ⚠️ **Note on the Tier-1/2 step-9 miss (separate, app-map charting):** step 7 focused `mock_card_number_input`
  by COORD (47px snap), not testid — that element has no `testid` in the current app_map, so a re-exploration
  that charts the mock-card reveal WITH testids would make the fast path more robust (see the run-9 note on
  re-exploring RPS so the "Use Mock Card" reveal is charted). The input-sharing fix above is what makes the
  Tier-3 recovery itself correct regardless. **RESTART the backend** to load the `run_vision_step.py` changes.

### Recent progress (2026-09-13) — Auto-Repair LOCAL stack: GraphRAG + Neo4j + Llama (data-sovereignty backup)

A config-only switch to run Auto-Repair **entirely inside the customer's environment** — no code or
documents sent to a third-party service — as a SECONDARY/backup option. The primary, default
combination is unchanged: **HuggingFace sentence-transformers + Chroma + Claude**. The alternative is
**graph-RAG over Neo4j + a local Llama via Ollama**. Flipping between them is env-vars only; there is
no code change and unsetting the vars restores the exact default behaviour. **120 tests pass (same 8
pre-existing env-only fails) — no regression by construction (defaults untouched, all new deps lazy).**

- **Retrieval switch** `repair_retrieval_backend` (`chroma` default | `graphrag`) — the whole surface.
  `parse_code_and_store.search()` and `build_codebase_index()` DISPATCH on it: `chroma` runs the exact
  existing path; `graphrag` lazily imports the new `repair_agent/graphrag_store.py`. The document
  collection was extracted into `collect_documents()` so BOTH backends index the identical chunks
  (same chunking/skip rules) — only the vector STORE differs.
- **`repair_agent/graphrag_store.py`** — the local backend. Uses the SAME local HuggingFace embeddings
  (`_get_embedding_model`, reused — embeddings computed in-process, never sent out) stored as a Neo4j
  **vector index** PLUS a lightweight **code graph** (`(:RepairChunk)-[:PART_OF]->(:File)`).
  `search(query,k,where)` returns langchain `Document`s with the IDENTICAL metadata keys
  (source/type/start_line/end_line/section) so `retrieve_context` is backend-agnostic; the
  file-neighbourhood expansion (`search(where={"source":src})`) is a **graph traversal** (Cypher fetch
  of a File's chunks), and type/code filters (`{"type":{"$in":[...]}}`) map to Neo4jVector filters
  (with a robust over-fetch + Python post-filter fallback across langchain-neo4j versions).
  `build_index()` reuses `collect_documents()`, resets nodes/index (batched), builds the vector index,
  then adds the File graph. `langchain-neo4j` + `neo4j` are imported **lazily and only here** (new
  `[graphrag]` extra), so the default carries no new dependency; a missing extra raises a clear,
  actionable ImportError.
- **LLM switch reuses the existing `repair_llm_backend`** (`claude` default | `local`) →
  `get_local_llm()` (Ollama). The **local stack = `REPAIR_RETRIEVAL_BACKEND=graphrag` +
  `REPAIR_LLM_BACKEND=local`**. For the demo on the **current CPU VM** (e2-standard-4, no GPU), the
  lightweight Llama is **`llama3.2:3b`** (`REPAIR_LOCAL_MODEL=llama3.2:3b`) — Llama-4-class models
  (Scout/Maverick) need a GPU and are deferred (review later; see `docs/GCP_Cost_Estimate.docx` §8).
  This is a Neo4j-backed graph-RAG (vector + graph expansion) — the practical, VM-runnable form; the
  heavier Microsoft GraphRAG entity/community pipeline can populate the SAME Neo4j store later.
- **`retrieve_context` Chroma-dir guard** now only enforces `PERSIST_DIR` existence for the chroma
  backend (graphrag stores in Neo4j, no local index dir). `/api/health` platform block now reports
  `repair_retrieval` + `repair_llm` so the VM self-declares the active combination.
- **To enable on the VM (test flow):** `pip install -e ".[graphrag]"` + `pip install langchain-ollama`;
  run Neo4j (`docker run -p7687:7687 -p7474:7474 -e NEO4J_AUTH=neo4j/neo4jpassword neo4j`) and Ollama
  (`ollama pull llama3.2:3b`); uncomment the "AUTO-REPAIR: LOCAL stack" block in `.env`; **rebuild the
  index** (`POST /api/repair/index`, now writes to Neo4j) on the on-disk (buggy) code; run the failing
  test → Auto-Repair diagnoses locally. **RESTART the backend** after the `.env` change (uvicorn has no
  `--reload`). Unset the vars to revert to Chroma + Claude.

### Recent progress (2026-09-15) — local Auto-Repair verified on GCE: timeouts, air-gap, logs, patch-quality

The in-house stack (GraphRAG + Neo4j + local Llama) was run end-to-end on the GCE CPU VM
(e2-standard-4). Retrieval worked; the DIAGNOSE by `llama3.2:3b` was the weak link. A series of fixes,
all config-gated / additive, **no regression to the default Chroma + Claude path** (22/22
`test_cloud_agnostic` green; `_reject_reason` unit-checked; studio `tsc` clean):

- **Deploy ergonomics.** One-command switch: `graphrag`/`ollama` deps baked into the image (lazy, inert
  by default — no rebuild to toggle); **CPU-only torch** in the Dockerfile (avoids a ~2.5 GB CUDA
  download from `sentence-transformers`); profile-gated `neo4j` + `ollama` services (`--profile
  local-repair`); a commented `REPAIR_*` env block with switch/revert recipes. Recommended on the VM:
  keep VM-specific env in an untracked **`docker-compose.override.yml`** so `git pull` never conflicts.
- **RAG-index status fix.** `GET /api/repair/index` reported `exists` from the Chroma dir, which the
  graphrag path never creates → the UI badge stuck on "building" forever after a *successful* Neo4j
  build. Status is now backend-aware (`graphrag_store.index_exists()` = a cheap Neo4j node count); the
  studio also polls index status on its normal interval so the badge self-heals.
- **Timeout fix (local model was always abandoned).** The 90s per-provider DIAGNOSE deadline applied to
  the CPU model too — and was even shorter than its own 120s Ollama client timeout — so `llama3.2:3b`
  never finished and the chain fell to Claude. The **local** provider now gets its own budget
  (`repair_local_timeout_s`, default 120→**600**, +30s margin) and a **single** attempt; Claude keeps
  the tight 90s bound. `_invoke_with_deadline` gained a heartbeat.
- **Air-gap toggle** `repair_local_only` (default False): when true, DIAGNOSE uses ONLY the local model
  then the deterministic demo rule and **never calls Claude** — even if local is slow/errors/unreachable
  (no code leaves the box). Surfaced in `/api/health`. The LOCAL stack for data sovereignty is
  `repair_retrieval_backend=graphrag` + `repair_llm_backend=local` + `repair_local_only=true`.
- **Explicit logs + live progress.** `[REPAIR] RETRIEVE via GraphRAG + Neo4j`; `[REPAIR] DIAGNOSE via
  Llama · <model> (air-gapped) [providers: local]`; a heartbeat every ~10s (`still working… Ns/Ms`); a
  clear `✓ patch produced by LOCAL Llama`. Nodes stream a per-stage `tool` + `note`, so the studio shows
  the REAL tool per stage (GraphRAG + Neo4j, Llama · model) and a live elapsed note instead of the old
  hard-coded "Chroma + HuggingFace RAG"/"Claude Opus 4.8".
- **Patch-quality guards (the observed failure).** On TC-VPS-009 (the hardest, cross-kiosk persistence
  bug) `llama3.2:3b` returned a degenerate patch — `find:"amount"` → `replace:"amount"` (a no-op, and
  `amount` occurs 26× → apply refused) and chose the *relabel* symptom, not the root cause. Two
  additive guards: the DIAGNOSE prompt now requires `find` to be a **distinctive, verbatim multi-line
  snippet unique in the file** (never a bare token) and to DIFFER from `replace`; and a cheap
  `_reject_reason()` sanity check rejects no-op/empty/bare-token patches BEFORE apply, giving the LOCAL
  model **one corrective retry** with the exact reason. These help all models and can't regress Claude
  (they only reject patches that would fail at apply anyway).
- **⚠️ The core limit is model capability, not config.** A 3B general model can't reliably root-cause a
  reasoning-heavy bug or emit a precise unique patch. The main quality lever is **model choice**, not
  fine-tuning: use a **code-specialised** model — `qwen2.5-coder:7b` is the practical CPU ceiling
  (slow); `qwen2.5-coder:14b/32b` or a Llama-4-class model on a **GPU** (see `docs/GCP_Cost_Estimate`
  §8) approaches Claude quality. Change via `REPAIR_LOCAL_MODEL` (no code change). Demo tip: show the
  local path on the SIMPLER planted bugs (login `-bug`, products `0`) which 3B can handle, and reserve
  the hard design bug for Claude or a GPU model. Fine-tuning is high-effort (GPU + curated dataset) and
  a tuned 3B still won't match a larger off-the-shelf coder — not recommended as a first step.

### Recent progress (2026-09-16) — REAL Microsoft GraphRAG backend (3rd retrieval option, ONE local model)

Added the genuine **Microsoft GraphRAG** entity/community indexing pipeline as a THIRD, config-only
Auto-Repair retrieval backend, alongside Chroma (default) and the Neo4j graph-RAG. It is switchable
with the SAME env/parameter mechanism as the existing local stack — **no code rework to switch tools
or models** — and uses **ONE local model for both graph-building AND code-fixing**. Defaults are
untouched (Chroma + Claude), so **no regression by construction** (22/22 `test_cloud_agnostic` green,
compose/YAML + `start-all.sh` syntax validated, lazy imports keep the default image identical).

- **New value `repair_retrieval_backend="msgraphrag"`** (chroma | graphrag | **msgraphrag**). The whole
  surface is the same seam the other backends use — `parse_code_and_store.build_codebase_index()` /
  `search()` DISPATCH on it (lazy import), `retrieval_tool_label()` + `/api/repair/index` +
  `/api/health` are backend-aware — so `retrieve_context` and the whole diagnose pipeline are unchanged.
- **`repair_agent/ms_graphrag_store.py`** (new, mirrors `graphrag_store.py`'s public API:
  `build_index()/search()/index_exists()`):
  - **build** = the REAL pipeline. Writes each collected chunk as its own input file + a `metadata.json`
    sidecar (preserves source/lines through GraphRAG's processing), **scaffolds `settings.yaml` via
    `graphrag init`** (so it matches the INSTALLED version's schema) and PATCHES only the model wiring
    to point at Ollama's **OpenAI-compatible** endpoint, then runs `graphrag index`. The LLM does the
    entity/relationship extraction + Leiden community detection + community summaries.
  - **search** = similarity over the graph's **text units** (re-embedded with the same cached local
    HuggingFace model → fast + version-independent of LanceDB), mapped back to Documents with the
    IDENTICAL metadata keys; supports the `{'source': …}` file-neighbourhood expansion and the
    `{'type': {'$in': …}}` code filter; for an unfiltered query it also appends the top **community
    reports** (the graph's synthesised "big picture"). Version-tolerant parquet loading (`text_units` /
    `create_final_text_units`, etc.).
- **ONE model for retrieval-graph + fix (the consistency ask).** `GRAPHRAG_LLM_MODEL` defaults empty →
  `ms_graphrag_store._llm_model()` falls back to `repair_local_model`, and `graphrag_api_base` defaults
  to `repair_local_base_url + "/v1"`. So the SAME Ollama model (e.g. `qwen2.5-coder:7b`) both BUILDS the
  graph and DIAGNOSES the patch. Embeddings for the build use a small `nomic-embed-text` (separate, tiny
  — not "the model"). New config block: `graphrag_root_dir`, `graphrag_llm_model`,
  `graphrag_embedding_model`, `graphrag_api_base`, `graphrag_api_key`, `graphrag_search`,
  `graphrag_chunk_size`, `graphrag_community_level`.
- **Env-switchable, no compose edits — same mechanism as the local stack.** The `docker-compose.yml`
  app service now reads `REPAIR_*` / `GRAPHRAG_*` from `${VAR:-default}` (defaults reproduce Chroma +
  Claude byte-for-byte), so switching stacks is env-only. Three one-command recipes are documented IN
  the compose file (A: Chroma+Claude, B: graphrag+Llama, C: msgraphrag+Qwen).
- **`start-all.sh` parameter mode extended** (as requested — "add one more value"):
  `LOCAL_REPAIR=graphrag` (=1, current: Neo4j graph-RAG + Llama) and **`LOCAL_REPAIR=msgraphrag`** (=2:
  Microsoft GraphRAG + Qwen). The script EXPORTS the right `REPAIR_*`/`INSTALL_MSGRAPHRAG` env (so
  compose's `${VAR:-default}` picks them up), starts `--profile local-repair` (Ollama; Neo4j idles for
  msgraphrag), and pulls the model **+ the embedding model** (msgraphrag). Both local modes air-gap by
  default (`REPAIR_LOCAL_ONLY=true`).
- **Heavy deps are opt-in (no default-image regression).** New `[msgraphrag]` extra
  (`graphrag`,`pandas`,`pyarrow`,`pyyaml`). The Dockerfile installs it only under
  `--build-arg INSTALL_MSGRAPHRAG=true` (compose `build.args` wires it to the `INSTALL_MSGRAPHRAG` env;
  `LOCAL_REPAIR=msgraphrag` sets it). Default build = unchanged image. `graphrag` is lazy-imported, so
  even when installed it changes nothing until msgraphrag is selected.
- **To run on the VM/GPU:** `LOCAL_REPAIR=msgraphrag ./start-all.sh` (or the compose recipe C), then
  `POST /api/repair/index` (this RUNS the pipeline — slow on CPU, meant for a GPU with `qwen2.5-coder`),
  then a failing test. Verify: `/api/health` → `"repair_retrieval":"msgraphrag","repair_llm":"local"`.
  ⚠️ The GraphRAG index build is LLM-heavy; a 3B/CPU box is impractical for it — this is the GPU-track
  quality option. The graph workspace persists under `./data/graphrag`.

#### Update (2026-09-16, later) — verified live on a GCE **g2-standard-8 (L4)**; 5 follow-ups

Ran the real Microsoft GraphRAG build end-to-end on the L4 (`qwen2.5-coder:14b`). Fixes/additions after
the live run (all committed on `cloud-agnostic-agent`):
- **GraphRAG 3.1.2 schema.** `graphrag init` is INTERACTIVE in 3.x (prompts for the model → aborts under
  subprocess), so `_scaffold_settings` was silently using a wrong hand-written template. Now it builds
  `settings.yaml` from GraphRAG's own `init_content.INIT_YAML` (always version-matched) and wires it to
  Ollama: `completion_models`/`embedding_models` maps (NOT `models:`), `input.type: text` is the READER
  (`file` was invalid → "not registered in InputReaderFactory"), storage/base_dir under a SEPARATE
  `input_storage:`, NO `file_pattern` (text reader defaults to `.txt`, which also killed a `$`-anchor
  `string.Template` "Invalid placeholder" error), and prompt-file paths DROPPED so GraphRAG uses built-in
  default prompts (no `prompts/` dir since we skip `init`). Ollama reached via its OpenAI-compatible
  endpoint (`api_base = repair_local_base_url + /v1`); overriding `api_key` removes the template's
  `${GRAPHRAG_API_KEY}` (no `.env` needed).
- **GPU wiring.** `docker-compose.gpu.yml` reserves the NVIDIA GPU for the `ollama` service; `start-all.sh`
  `GPU=1` layers it (with a pre-check on the nvidia runtime). Host needs the driver + nvidia-container-
  toolkit; on GCE Shielded VMs **Secure Boot must be OFF** or the module won't load. `OLLAMA_CONTEXT_LENGTH`
  (default 8192) on the ollama service so community-report prompts (~8k) aren't truncated (Ollama defaults
  to 4k).
- **Knowledge docs are config-driven** (`repair_design_doc` / `repair_requirements_doc` (new) /
  `repair_test_cases` under `repair_docs_dir`). The VM compose points them at the mounted POS repo
  `docs/Expanded_Version` (Design + Requirements + Test Cases). Requirements doc indexed as authoritative
  intent.
- **GraphRAG → Neo4j export** (`graphrag_export_neo4j`, default on): after the build, entities/relationships/
  community summaries load into Neo4j as `(:Entity)-[:RELATED]->(:Entity)` + `(:Community)` (distinct from
  the graphrag(Neo4j) RAG store's `RepairChunk/File`), so the graph is BROWSABLE at `:7474`. Best-effort;
  a down Neo4j never fails the build.
- **INCREMENTAL builds** (`graphrag_incremental`, default on): after the first full `index`, subsequent
  `POST /api/repair/index` runs GraphRAG's **`update`** — only NEW/CHANGED code+docs are re-extracted and
  merged (community detection still re-runs globally). Chunk files are named by a CONTENT hash (source +
  text, line-independent), so an unchanged function/doc keeps the same identity and is skipped. Delete
  `data/graphrag/output` (or set `graphrag_incremental=false`) to force a full rebuild. The `update` diffs
  against the OUTPUT parquet, which is never deleted (only the input dir is rewritten).
- ⏱️ Live timing: `extract_graph` ≈ 4–5 units/min for 513 units on the L4 (~2–3 h full build). Incremental
  makes routine re-indexes after a code/doc edit far cheaper. `python -m graphrag` is the working CLI form
  (the `graphrag` console `init` prompts); `_run_graphrag` uses it for `index`/`update`.
- **POS-domain typing + whole-function retrieval** (after the first live diagnose miss — qwen-14b relabelled
  `RPS_STATION_ID`→`VPS_STATION_ID` instead of removing the `&& !issuedSmartCard` guard). Two additive fixes:
  (a) `graphrag_entity_types` (default `Function,SmartCard,Transaction,Balance,KioskStation,Endpoint,Screen`)
  is written into `extract_graph.entity_types`, and — best-effort — a short POS preamble (`_POS_PREAMBLE`) is
  prepended to GraphRAG's OWN default extraction prompt (fetched via `_default_extract_prompt()`; if the
  template can't be located, entity_types still apply) → the Neo4j graph now uses POS types.
  (b) `graphrag_whole_function` (default on): GraphRAG re-chunks code into ~1200-token text units, which can
  SPLIT a function so the buggy guard and its symptom land in different units; `search()` now expands each
  hit to the ORIGINAL whole tree-sitter chunk (read from `input/<content-hash>.txt` via the sidecar,
  deduped) so the cause + symptom are shown together, matching the Chroma/Neo4j whole-function behaviour.
  Both improve retrieval/graph quality but do NOT change the reasoning ceiling — a correct root-cause fix on
  the hardest bug still needs a stronger model (qwen2.5-coder:32b / Claude). No regression (defaults inert
  for non-msgraphrag; 22/22 `test_cloud_agnostic` green). A demo walk-through doc lives at
  `docs/Auto_Repair_GraphRAG_Demo_Walkthrough.docx`.


---

### 2026-09-18 — repair robustness: retrieval re-rank + build-vs-diagnose VRAM split + diagnose debug dump

Context: on the GCE `g2-standard-8` (L4 24 GB) with `repair_retrieval_backend=msgraphrag` +
`repair_local_model=qwen2.5-coder:32b`, a TC-RPS-003 repair (the planted `quantity <= 1` product-quantity
bug) failed twice over: (a) the buggy line was in the index/text-units but never SURFACED in retrieval, and
(b) the 32B diagnose ran ~10 min and TIMED OUT with "(no output)". Root causes and fixes (all no-regression;
defaults inert for the Chroma+Claude primary path):

**1. Retrieval-ranking rescue (`repair_agent/repair_failed_test.py::retrieve_context`).**
- Root cause: pure vector similarity ranks by the failure's DOMINANT vocabulary (design-intent + navigation
  words), which buries the exact buggy function even when it is indexed. TC-RPS-003's failure reads as
  "product quantity / cart" but the design-doc + a `goTo(...)` nav chunk out-ranked the `quantity <= 1`
  guard, so it fell outside the retrieved top_k on BOTH Chroma and msgraphrag.
- Fix: pull a WIDER candidate pool (`_POOL_MULT=3` × top_k) and run a **lexical re-rank** that PROMOTES up
  to `_SIGNAL_PROMOTE=2` code chunks sharing ≥ `_SIGNAL_MIN=2` DISTINCT signal tokens with the failure.
  Signals (`_signal_tokens`) = identifiers (camelCase or ≥4 chars), small integer literals, and quoted
  values, minus a jargon/English stoplist (`_SIGNAL_STOP`). `_lexical_score` counts distinct matches
  (word-boundary for numbers, substring for compound code identifiers). Promoted chunks are slotted after
  the top-4 vector hits.
- Properties: ADDITIVE (the vector top_k is untouched; slicing the wider pool back to top_k reproduces the
  prior top_k exactly since vector order is stable) and a STRICT NO-OP when nothing clears the threshold —
  so the login / cross-kiosk design-bug behaviour is unchanged. Backend-agnostic: it runs over whatever
  `search()` returned, so it covers Chroma AND the msgraphrag whole-function chunks.
- Verified in isolation: on a simulated TC-RPS-003 failure the `quantity <= 1` chunk scores 4 vs 0 for a
  nav helper and is the ONLY chunk promoted, even when the "vector top hits" list deliberately excludes it.

**2. Build-vs-diagnose parallelism split — the 10-min timeout (`docker-compose.yml`, `vision_agent/llm.py`,
`vision_agent/config.py`).**
- Root cause: Ollama reserves KV cache = `num_parallel × num_ctx` PER LOADED MODEL. `OLLAMA_NUM_PARALLEL`
  had been hard-set high (8) to speed the 14B msgraphrag BUILD; that same value applied to the 32B DIAGNOSE
  model → 8 × 16384 KV ≫ 24 GB VRAM → Ollama offloaded layers to CPU → generation ~10× slower → the diagnose
  blew its `repair_local_timeout_s + 30` = 630 s budget and returned "(no output)".
- Fix (primary): `OLLAMA_NUM_PARALLEL` default changed `1 → 0` (auto). In auto mode Ollama sizes parallel
  slots PER MODEL by free VRAM — a 14B build model gets up to 4 slots (still parallel/fast), a 32B diagnose
  model gets 1 slot (VRAM-safe). This is the split, done automatically; no hand-toggling, and no separate
  Ollama server needed unless builds/diagnoses run on different GPUs.
- Fix (belt-and-braces): `vision_agent.llm.effective_local_num_ctx()` clamps a ≥30B diagnose model's
  `num_ctx` to `repair_local_num_ctx_cap_large` (12288 → ~3 GB KV + 20 GB weights ≈ 23 GB, on-GPU at 1
  slot). 14B/7B are never clamped (keep 16384). `get_local_llm()` sends the effective value AND
  `retrieve_context`'s context fit-trim reads the SAME effective value, so the prompt is packed to the
  window the model actually runs with (no silent truncation). `_is_large_local_model` matches a param count
  ≥30 in the model name (32b/70b true; 14b/7b false).
- Diagnostic: on a local diagnose TIMEOUT, `propose_patch` now calls `_ollama_vram_report()` (GET
  `/api/ps`) and logs each model's VRAM/CPU split, warning loudly on `⚠ OFFLOAD` with the exact remedy.

**3. Diagnose debug dump — "what did we send QWEN and what did it say" (`vision_agent/llm.py::invoke_json`,
`repair_agent/repair_failed_test.py::propose_patch`).**
- `invoke_json` gains an optional `on_raw(text)` hook, called with the model's RAW response before JSON
  parsing (default None → no behaviour change for any existing caller).
- With `REPAIR_DEBUG_DUMP=true` (config `repair_debug_dump`, dir `repair_debug_dir=./data/repair_debug`),
  `propose_patch` writes a per-call text file: the EXACT prompt, and for each provider the raw response,
  parsed patch, reject reason, elapsed time, outcome, and the `ollama ps` VRAM report. Written in a
  `finally` so even a full timeout ("no output") is captured. Off by default (zero I/O in normal runs).

No-regression: `pytest tests/` = 120 passed, 5 skipped, and the SAME 8 pre-existing failures
(`test_template_match` ×4 needing local reference PNGs, `test_vision_agent` screen-analysis ×4 needing an
API key) that fail identically on clean HEAD (verified by stashing the edits). `py_compile` clean;
`effective_local_num_ctx` / `_is_large_local_model` unit-checked (32b→12288, 14b→16384).

---

### 2026-09-20 — Auto-Repair v2: failure-anchored retrieval + precision context + patch verification (P0→P3)

The `REPAIR_DEBUG_DUMP` added on 2026-09-18 immediately paid off: a live dump of a TC-RPS-003 repair
(`diagnose_20260918_120259_TC-RPS-003.txt`) proved the auto-repair failures were NOT a model-capability
problem. The test is titled *"Mock card payment with sufficient balance succeeds"* but it actually failed
much earlier, at **add-to-cart** (`quantity 0 → "Quantity Required" popup → never reached the cart`). Yet
**all 16 retrieved context chunks were PAYMENT / card-reader code** — the add-to-cart bug appeared in NONE
of them. qwen-32B, handed payment code and a payment-titled failure, reasonably (but uselessly) patched the
payment-approval predicate. **No model — not qwen, not Claude — can fix a bug whose code is not in the
context.** Root cause: `_failure_text_for` leads with the test's DESIGN INTENT (payment), which dominates
the embedding, while the single most diagnostic text (the `OBSERVED` symptom) was under-used
(`_action_query` even cut off at `OBSERVED:`).

A generic, phased redesign so this class of failure is fixed for ALL tests, not just TC-RPS-003. All
changes are in `repair_agent/repair_failed_test.py` + `vision_agent/config.py` (+ one line in
`repair_agent/nodes/diagnose.py`); the LangGraph shape (retrieve → diagnose → apply → test → build → pr)
is UNCHANGED, so no graph regression.

**P0a — Failure-anchored, multi-lane retrieval (`repair_failure_anchor`, default on).** Retrieval now runs
several interleaved query lanes, priority-ordered: (optional RCA) → **failure-point** (the `[FAILED HERE]`
step + `OBSERVED` symptom + failing assertions — WHERE it broke) → **action** (executed steps) → **intent**
(the full design-intent-led failure). `_round_robin` picks one hit per lane per round so a heavier lane's
vocabulary can't bury a lighter lane's best hit. The failure-point lane is what makes a payment-titled test
that dies at add-to-cart retrieve ADD-TO-CART code. The intent lane is retained so the cross-kiosk VALUE
demo still surfaces its persistence/spec code (no regression). Degrades cleanly to the old action+intent
behaviour when anchoring is off or the failure carries no failed-step/observed text.

**P0b — Symptom-relevance patch verification (`repair_verify_relevance`, default on).** After a patch is
produced, score how many FAILURE-POINT signal tokens it touches (`_patch_relevance`) vs the best-matching
retrieved chunk (`_hits_best_relevance`). Critically the signals come from `_failure_point_text`, NOT the
whole failure — otherwise the design-intent's payment vocabulary leaks in and a payment patch scores
"relevant" to an add-to-cart failure (measured: 4 vs the correct 0). Three outcomes: patch overlaps the
symptom → trust it; 0 overlap AND nothing relevant was retrieved → logged as a RETRIEVAL miss (a nudge
can't help — the code isn't in context); 0 overlap BUT a ≥3-overlap chunk WAS in context → the model
picked the wrong chunk → one corrective retry nudged toward the symptom, keeping the more-relevant of the
two (never dead-ends). This is the guard that catches the exact dump (payment patch, 0 add-to-cart overlap,
add-to-cart chunk relevance 6 available). Conservative: only acts on a strictly-0-overlap patch when a
strong alternative existed, so a correct-but-lexically-distant fix (a cross-kiosk endpoint) is never
second-guessed.

**P1 — Precision context by bug class (`_bug_class`, scored on the failure POINT).** A `spec` failure
(balance / transaction / cross-kiosk / refund vocabulary) keeps the EXISTING generous profile (5 design
docs, 2 general, 16-block cap) — byte-for-byte, so the design-bug demo does not regress. An `interaction`
failure (wrong screen / popup / unresponsive control — a code bug where doc prose is noise) uses a tight,
code-heavy profile: `repair_max_context_blocks_interaction` (8), `repair_design_docs_interaction` (1),
`repair_general_docs_interaction` (1). This is what cuts the 16-chunk, half-boilerplate context (the dump
had 5 design + 2 dummy-config chunks that said nothing) down to a focused set. Routing on the failure
POINT (not the whole failure) is essential: the payment test's INTENT reads "balance/payment" and would
misroute to `spec`; its failure POINT (cart/popup) correctly routes to `interaction`.

**P2 — RCA localisation pass (`repair_rca_phase`, opt-in / default off).** One lightweight LLM call that
localises the bug (`{bug_class, suspect, search_terms}`) BEFORE the fix; its search terms seed the
top-priority retrieval lane. Off by default because it adds a model call (slow on the local 32B); enable
once anchored retrieval is proven. Best-effort and bounded — any error/timeout returns "" and retrieval
proceeds on the deterministic lanes (no hard dependency).

**P3 — Structured retrieval logging + debug-dump linkage.** One line per repair —
`[REPAIR] retrieval: class=… lanes=… cap=… → N chunks (code=…, design=…, other=…)` — plus the verify line
(`patch_relevance=… best_context_relevance=…`), so "why did it retrieve THAT / is the patch on-target" is
answerable from the console without the full dump. The full-fidelity prompt+response is still in the
`REPAIR_DEBUG_DUMP` file. Deeper POS runtime-log ingestion and a full **re-run-the-failed-test oracle** are
the documented next step (the strongest verification, but operationally coupled to redeploying the patched
POS — deferred to keep this change no-regression). **Screenshot / vision analysis requires Claude or a
multimodal model — qwen2.5-coder is text-only** and cannot read images; the code-fix path stays text.

**Config added** (`vision_agent/config.py`): `repair_failure_anchor`, `repair_verify_relevance`,
`repair_max_context_blocks_interaction`, `repair_design_docs_interaction`, `repair_general_docs_interaction`,
`repair_rca_phase`.

**No-regression:** `pytest tests/` = 128 passed (120 prior + 8 new `tests/test_repair_retrieval.py`), 5
skipped, and the SAME 8 pre-existing failures (`test_template_match` ×4 needing local reference PNGs,
`test_vision_agent` screen-analysis ×4 needing an API key) that fail identically on clean HEAD. The spec/
cross-kiosk retrieval path is unchanged; only interaction-class bugs get the new tight profile, and the
new lanes/verification are additive. Team-facing write-up with flow diagrams:
`docs/Auto_Repair_v2_Code_Review.html`.


## 2026-09-21 — Auto-Repair v3: two separate agents (RCA + code-fixing), code/doc retrieval split, generic verification fix, screenshots for Claude

The v2 debug dump (`diagnose_20260920_171321_TC-RPS-003.txt`) proved the fix still failed for two concrete
reasons: (1) **retrieval never surfaced the buggy add-to-cart code** — every retrieved chunk was payment/
balance, so no model could fix it; and (2) the **verification guard was defeated by a lexical false
positive** — the failure-point signal `action` (from "checkout action") is a SUBSTRING of `transaction`
in the model's explanation, so the off-target balance patch scored relevance 1 (not 0) and was accepted
with no corrective retry. Plus the "RCA" was never a real agent (it was an opt-in, off-by-default search-
term helper that did NOT read the docs to rule the spec/test in or out). This milestone rebuilds it.

### 1. TWO SEPARATE AGENTS (for ALL models, Claude and local)
The pipeline is now `rca → retrieve → diagnose → apply → unit_test → build → prepare_pr` (see
`repair_agent/agent.py`). Two distinct agents:
- **RCA agent** (`repair_agent/nodes/rca.py` → `run_rca` in `repair_failed_test.py`) — reads ONLY the
  design/requirements docs + the test-case workbook (never source code) and returns
  `{verdict: code_bug|spec_bug|test_invalid, confidence, rationale, suspect, search_terms}`. A
  HIGH-confidence `spec_bug`/`test_invalid` **STOPS** the pipeline (`rca_stop`) — we never patch code to
  satisfy a wrong spec or an invalid test. A `code_bug` localises the suspect area + search terms and hands
  them to the fixer.
- **Code-fixing agent** (`retrieve → diagnose`) — retrieves CODE from the efficient code-only vector RAG,
  seeded by the RCA's `rca_query` lane, plus the design doc as authoritative reference, then proposes the
  minimal patch.
`REPAIR_RCA_PHASE` now defaults **True**. NO-REGRESSION is preserved by a **conservative gate**
(`_rca_should_stop`: stop only on HIGH-confidence spec/test) + a code-fixing retrieval that is a **superset**
of the pre-RCA lanes — so on a code bug (every demo bug), Claude proceeds and fixes exactly as before.
`REPAIR_RCA_GATE=false` makes RCA advisory-only (localise + always proceed).

### 2. RETRIEVAL SPLIT — msgraphrag = docs/tests only; a separate efficient code RAG
`parse_code_and_store.py` now exposes `search_code` (code-only vector RAG) and `search_docs` (docs/tests
via the knowledge backend), plus `collect_code_documents` / `collect_doc_documents` and `build_code_index`.
When `repair_retrieval_backend=msgraphrag` and `repair_msgraphrag_docs_only=True` (default), the
entity/community graph is built from **docs + test cases only** (the RCA agent's knowledge base), and a
separate **Chroma code-only index** (`repair_code_persist_dir`) serves the code-fixing agent — pinpoint
code retrieval is faster and more precise from a vector index than from a coarse community graph. One
"Rebuild index" refreshes both. The pure-Chroma default and the graphrag/Neo4j option are unchanged
(`_effective_code_backend` returns the main backend there, so `search_code`/`search_docs` are just the old
`search()` with the code/doc type filters).

### 3. GENERIC lexical-score fix (not TC-RPS-003-specific)
`_lexical_score` now matches at **sub-token granularity** (`_subtokens`): a signal counts only when it
equals a WHOLE sub-token of the text, split at non-alphanumeric AND camelCase / letter-digit boundaries.
So `action` no longer matches inside `transaction` (the exact defeat), while `cart` still matches
`onAddToCart` and `1` still does not match inside `100`. `_signal_tokens` decomposes identifiers the same
way. Fully generic — no per-test rules. New tests prove `action ⊄ transaction`, the real balance patch now
scores 0 (was 1), and the compound-identifier / number-boundary cases still hold.

### 4. RICHER failure detail for the models (all issue types)
`api/main.py::_failure_text_for` now appends (gated by `repair_failure_detail`, default on, bounded) a
per-step **EXECUTION TRACE** (action + method/screen/expected/actual + PASS/FAIL + observation + any
error/stack) and a tail of the run's **console/application log**, so a text model like qwen has the full
flow + logs to diagnose from, not just the failed assertion. Appended AFTER the "Fix the ROOT CAUSE"
marker so the failure-point / action / intent parsers are unaffected.

### 5. SCREENSHOTS for Claude (multimodal only)
`_message_for` attaches the FAILED steps' screenshots (base64, media-type sniffed) to the RCA + diagnose +
verify prompts **only when the provider is Claude** (`repair_use_screenshots`, default on). qwen2.5-coder
is text-only and is always sent plain text. `api/main.py::_failure_images_for` gathers the failed steps'
`screenshot_after`/`before` from `screenshots/<run_id>/`; threaded through `run_repair(images=…)` → state →
the RCA and code-fixing agents. Answers the standing question: previously Claude did NOT use screenshots in
repair; now it does for complex visual issues.

### 6. Debug-dump diagnostics + RCA verdict
`REPAIR_DEBUG_DUMP` now writes a **RETRIEVAL & VERIFICATION DIAGNOSTICS** block: the bug class, the failure-
POINT slice retrieval anchored on, the **per-context relevance RANKING**, the accepted patch's relevance,
the RCA agent's verdict/confidence/rationale/suspect, and the ON-TARGET / OFF-TARGET / **RETRIEVAL-MISS**
verdict — so "why did it retrieve/patch THAT, and what did the RCA decide" is answerable from the dump file
alone.

### Frontend (`../kiosk-test-studio`, branch `studio-cloud-agnostic`)
- **Two-agent pipeline UI** (`AutoRepair.tsx`): a new `rca` stage with an `① RCA Agent` / `② Code-Fixing
  Agent` group header, an RCA stage-detail (verdict badge + confidence + rationale + suspect + the docs it
  read), an `rca_stopped` result banner, and the `rca_stopped` overall badge. Client types extended
  (`RepairRca`, stage `verdict/…`, job status `rca_stopped`).
- **Auto-Repair window controls** (`LiveMonitor.tsx`): the popup-blocked INLINE fallback (which showed only
  a Close button — the "only Close" the user saw) is now a floating in-app WINDOW with a title bar and real
  **minimize / maximize / close** controls (collapse to a docked bar / fill the viewport / close), kept
  mounted while minimized so the repair keeps streaming.

### Config added (`vision_agent/config.py`)
`repair_rca_phase` (now True), `repair_rca_gate`, `repair_use_screenshots`, `repair_failure_detail`,
`repair_code_backend`, `repair_code_persist_dir`, `repair_msgraphrag_docs_only`.

### No-regression
`pytest tests/` = **133 passed** (128 prior + 5 new in `tests/test_repair_retrieval.py`), 5 skipped, and
the SAME 8 pre-existing `test_template_match`/`test_vision_agent` fixture failures that fail identically on
clean HEAD. Frontend `npm run build` clean. The default Chroma + Claude path: same code/design retrieval;
RCA runs but a code bug always proceeds to the unchanged fixer, so the fix result is preserved.

---

## 2026-09-21 (later) — gap-vs-defect verify judge (Option C), debug-dump-on-by-default, Detailed Report window

Three follow-ups on top of the v3 two-agent redesign, after the user observed: (a) the product-quantity
popup bug went through Tier-3 replanning instead of failing fast into Auto-Repair; (b) no debug report was
written on the default Claude + Chroma path; (c) a customer-facing elaborate report of the repair was
wanted.

### 1. Option C — the "gap vs defect" verify judge (Claude vision)
**Problem.** A wrong-screen `verify` failure is ambiguous. `run_vision_step` routed *every* screen-only
miss (`expected != actual`, no text assertion) as a recoverable "missing-navigation gap" → Tier-3
bridge-and-resume. TC-RPS-003's real defect (add-to-cart popped a **Quantity Required** dialog and never
reached the cart) looks structurally identical to a benign nav gap, so it was replanned instead of failing
fast into Auto-Repair.

**Fix (generic, app-agnostic).** Before bridging, ask Claude ONE strict question from the screenshot —
`classify_wrong_screen_failure(image, step, expected, actual)` in `vision_agent/nodes/validate_pipeline.py`
— returns `("gap"|"defect", observation)`:
- **defect** → the app REFUSED/ERRORED (popup, error banner, blocked/broken state). Fail FAST: mark
  `sr["verify_defect"]`, and the outer handoff treats it as terminal (`_verify_terminal`), so Auto-Repair
  fires on the real bug instead of replanning.
- **gap** (or any uncertainty/error) → bridge exactly as before. **Default-safe**: the judge returns
  `"gap"` on any parse error/exception, so working recoveries never regress.

Gated by `verify_defect_judge` (default True). Runs only on the `_screen_only` path — text/value
assertions on the right screen were already terminal, and `verify_defect` never fires for them. Vision is
always Claude in this system, so no multimodal gate is needed. The judge reuses one `get_fast_llm()` vision
call (same pattern as `verify_intent_satisfied`).

**Deferred (documented in CLAUDE.md) — no-LLM equivalents for a future air-gapped/local path:**
- *Option A* — classify the ACTUAL screen deterministically: a `*_popup` / `*_required` / `*_error` /
  error-banner state ⇒ defect (terminal); a neutral other screen ⇒ gap (bridge).
- *Option B* — keep the bridge but BOUND it and self-terminate: if Tier-3 can't reach the expected screen
  within its bounded segment, fail terminally rather than limp forward.
- A+B together give a no-LLM defect gate; wire them behind the same flag when the local scenario arrives.

### 2. `REPAIR_DEBUG_DUMP` on by default
The dump loop already covered every diagnose provider (incl. `claude`), but `repair_debug_dump` defaulted
`False`, so the default Claude + Chroma path wrote nothing to `./data/repair_debug`. Now `repair_debug_dump
= True` (config) and `REPAIR_DEBUG_DUMP:-true` (compose); set `REPAIR_DEBUG_DUMP=false` to silence the I/O.
Every repair now leaves a report capturing the exact prompt + raw response + parsed patch + RCA verdict.

### 3. Auto-Repair "Detailed report" window (customer-facing)
A **📋 Detailed report** button on each Auto-Repair card opens a floating window (minimize / maximize /
close) that walks the whole repair for a non-engineer audience:
- **① RCA agent** — verdict + confidence + rationale + suspect, and the docs/test-cases it read (full
  snippets, no source code).
- **② Code-fixing agent** — the retrieved code chunks; then the **Diagnose** centrepiece: *exactly what was
  sent to Claude* — (1) the failure description, (2) the failed-step **screenshots** (rendered inline,
  loaded from the run via `run_id`), (3) the retrieved code + doc context — followed by *Claude's answer*
  (the minimal find/replace patch); then apply / unit-test / build (command + output) and the PR branch +
  diff.

**Backend:** `repair_agent/nodes/diagnose.py` now records `diagnose.inputs =
{failure, context, screenshots[basenames]}` on the stage (and emits it), so the report is a pure render of
data already on the job — no new endpoints. **Frontend:** `client.ts` `RepairStage.inputs`;
`AutoRepair.tsx` gains `DetailedReportWindow` + section renderers, using the existing
`runScreenshotUrl(run_id, name)` helper for screenshots.

### No-regression
`pytest tests/` = **133 passed**, 5 skipped, same 8 pre-existing `test_template_match`/`test_vision_agent`
fixture failures (identical on clean HEAD). Studio `npm run build` clean. Option C is a strict superset of
the prior verify routing (default-safe to "gap"); the debug-dump flag only adds I/O; the Detailed Report is
an additive read-only view. The default Claude + Chroma repair result is unchanged.

---

## 2026-09-21 (later still) — apply resilience (RAG chunk ≠ on-disk text) + failure-point boilerplate hygiene

A VM re-run of TC-RPS-003 showed **retrieval and diagnose both CORRECT** — Claude found the add-to-cart
guard (`App.tsx:966`) and proposed the right fix (`if (quantity <= 1)` → `if (quantity < 1)`) — but the
patch **failed to apply**: `Patch find-text was not found in src/App.tsx`.

### 1. Apply resilience — the real blocker
**Root cause (NOT a stale index).** The Tree-sitter indexer captures an INNER node, so the chunk stored
`addToCart = (product…) => {` while the file on disk has `  const addToCart = (product…) => {`. The model
faithfully copies the snippet it was shown, so its byte-exact `find` (`  addToCart = …`) does not exist on
disk → the single `str.replace` found 0 occurrences. The old error message blamed a stale index and told
the user to rebuild — misleading, since the index was current and retrieval/diagnose were right.

**Fix (generic, no reindex).** `apply_patch` now falls back to `_resilient_replace(text, find, replace)`
when the exact find is absent:
- WHOLE-LINE match tolerating (a) per-line indentation and (b) a dropped leading **declaration keyword**
  (`const`/`let`/`var`/`export`/`default`/`async`/`function`/access modifiers) — exactly the class of
  difference the RAG index introduces (`_find_line_matches` + `_DECL_PREFIX_RE`). Nothing looser, so it
  cannot latch onto an unrelated line.
- Requires a **UNIQUE** block match AND **equal find/replace line counts**; otherwise returns None and the
  caller raises (improved wording — no longer blames staleness alone).
- Rebuilds the block from the **REAL** file lines (keeping their indentation + any `const` prefix), moving
  only the changed fragment (`core.rfind(fs)` splice). So the on-disk `const` is preserved and only
  `<= 1`→`< 1` changes.
`_locate_file`'s file-search fallback uses the same tolerance (exact substring first, then resilient). No
index rebuild needed; the existing index applies cleanly now.

### 2. `_bug_class` / signal tokens read the SYMPTOM, not our appended guidance
**Observed.** The debug dump said `bug class … : spec` for what is a code/interaction bug. `_failure_text_for`
appends a guidance tail — "Fix the ROOT CAUSE… if the expected outcome is a value that should have been
persisted or shared across screens/kiosks (a balance, a transaction)…". Because the failed step, OBSERVED,
assertions and that tail are all in one semicolon-free trailing segment, `_failure_point_text`'s
`[FAILED HERE]` branch swallowed the **boilerplate** too, whose spec-vocab (balance/transaction/persisted/
shared) then scored ≥2 `_SPEC_SIGNALS` → `spec`, and polluted the lexical re-rank signal tokens.

**Fix.** `_failure_point_text` truncates the `[FAILED HERE]` segment at `OBSERVED:` / `Failing assertions:`
/ `Fix the ROOT CAUSE` (OBSERVED + assertions are already captured verbatim by their own regexes). The
boilerplate is dropped for **every** test. The add-to-cart failure now routes `interaction`; a genuine spec
failure still routes `spec` from its own OBSERVED/assertion text (`SPEC_FAILURE` test unchanged).

> Note: the `spec` misroute was **harmless** on this run — the generous spec profile still surfaced the
> add-to-cart chunk (Context 1, relevance 3) and Claude fixed it. But it was wrong and worth fixing
> generically; the real blocker was the apply mismatch (#1).

### No-regression
`pytest tests/` = **136 passed** (133 + 3 new in `tests/test_repair_retrieval.py`:
`test_resilient_replace_applies_when_index_dropped_const_prefix`,
`test_resilient_replace_is_safe_on_mismatch_and_ambiguity`,
`test_bug_class_ignores_appended_root_cause_guidance_boilerplate`), 5 skipped, same 8 pre-existing
`test_template_match`/`test_vision_agent` fixture failures. Both fixes are additive + default-safe (the
resilient apply only runs when the exact find is absent; the failure-point change only removes appended
boilerplate).

---

## 2026-09-21 (latest) — generic RCA/fixer across bug types + interaction-element retrieval + Executive Summary

A VM re-run of TC-RPS-003 confirmed the apply fix (the add-to-cart bug was patched and a PR raised), but
exposed a SECOND planted bug in the same flow — a **disabled mock-card input** so tapping "Use Mock Card"
never completed payment. Auto-Repair triggered but **mis-diagnosed** it: the actual buggy control was never
retrieved, so Claude produced a plausible-but-wrong fix (adding `'APPROVED'` to an `alreadyResolved` guard).
Per the user's direction this was addressed GENERICALLY (not a point-fix), to make RCA + code-fixing robust
across bug types (coding, design/requirement, environmental, invalid test) and across customer apps.

### 1. Generic, app-agnostic RCA with a 5-category taxonomy
`_RCA_PROMPT` no longer assumes a POS/kiosk domain ("the application under test"). The verdict taxonomy
(`_RCA_VERDICTS`) now spans:
- **code_bug** — the app code is at fault (→ the ONLY category handed to the code-fixing agent).
- **spec_bug** — a requirements/design bug (STOP).
- **test_invalid** — the test contradicts the design / targets the wrong thing (STOP).
- **environment** — infrastructure/environment: page never loaded, blank/spinner, network / API / service
  error or timeout, 5xx, missing dependency, mis-config, deploy problem, auth/session expiry (STOP — a code
  patch can't fix infra; fix the environment and re-run).
- **unknown** — insufficient evidence (→ proceed to the fixer to verify against source).

`_RCA_STOP_VERDICTS = {spec_bug, test_invalid, environment}` halt the pipeline only at **HIGH** confidence
(`_rca_should_stop`) — conservative, so `code_bug`/`unknown` never regress the default path. The RCA node's
human-readable notes were extended for the new verdicts, and the Studio verdict badges too.

### 2. Interaction-element retrieval lane
The live miss: retrieval got payment/reader code but never the disabled input's own JSX/handler, so no model
could fix it. New generic lane `_interaction_query(failure)` extracts the **element / test-ids and button
labels the test interacted with** around the failure (snake/kebab ids like `pay_with_mock_card_button`,
`mock_card_number_input`; ids named in a `type: … (element_id)` step; `tap: <Label> @` labels). Those map
DIRECTLY to the code that renders/handles the control — the strongest localiser for an interaction bug (a
disabled/renamed/removed control, a broken handler). Wired into `retrieve_context` as a lane with priority
**RCA → failure-point → interaction-element → action → intent**; additive + interleaved + de-duped, a strict
no-op when the steps carry no element ids (`repair_interaction_anchor`, default on). Generic across any app's
element identifiers — no per-test wording.

### 3. Diagnose: confidence + anti-fabrication
`_DIAGNOSE_PROMPT` is app-agnostic and now asks the model for two extra fields — `confidence`
(high|medium|low that the retrieved context ACTUALLY contains the cause) and `root_cause` — and explicitly
instructs it to prefer an evidence-backed high-confidence fix over fabricating a change to unrelated code
when the context lacks the real cause. Surfaced on `RepairPatch` (`confidence`, `root_cause`) → the
`diagnose` stage patch → the report. (The existing symptom-relevance guard + one nudged retry remain.)

### 4. Executive Summary report view (Studio)
The 📋 Detailed-report window now opens on a leadership-facing **Executive summary** (default), with a
**Technical details** toggle (the prior view, unchanged):
- **Outcome hero** (fixed & verified / stopped — <verdict> / could not complete), **KPI tiles** (root cause,
  files changed, lines changed, build), a colour-coded **pipeline stepper**, an **"evidence examined" bar
  chart** (docs read / code sections / screenshots), **confidence rings** (root-cause + fix), and **the fix
  at a glance**.
- Every chart carries a **"View technical details →"** link that switches to the technical view and scrolls
  to the matching section (anchors `sec-rca`/`sec-retrieve`/`sec-diagnose`/`sec-apply`/`sec-test`/`sec-build`/
  `sec-pr`). Dependency-free inline SVG/CSS — no chart library.

Naming: "Code-Fixing Agent" alternatives were proposed to the user (e.g. Remediation Agent, Fix Agent,
Resolution Agent, Patch Agent, Code Repair Agent) — pending a pick before any rename.

### No-regression
`pytest tests/` = **138 passed** (133 + 5 new across the two latest sessions in
`tests/test_repair_retrieval.py`), 5 skipped, same 8 pre-existing `test_template_match`/`test_vision_agent`
fixture failures. Studio `npm run build` clean. Everything is additive/default-safe: the RCA gate stays
conservative (code_bug/unknown proceed), the interaction lane is a no-op without element ids, the diagnose
extra fields are optional, and the Executive Summary is a new view that leaves the technical view intact.

---

## 2026-09-21 (latest+) — relevance-centred snippet truncation (the retrieved bug must reach the model)

Follow-up after a VM re-run of TC-RPS-003 (with the latest backend deployed) still mis-diagnosed the second
planted bug — the **disabled mock-card input**: `src/App.tsx:2484` calls `setMockMode(false)` where it must
be `setMockMode(true)`, so tapping "Use Mock Card" never reveals the mock-card entry.

### The real cause was NOT the index — it was prompt truncation
The buggy line WAS inside a retrieved chunk (`PaymentScreen`, lines 2344–2527 ≈ 7.5 KB, retrieved as
Context 4), but the diagnose prompt rendered each chunk as `page_content[:4000]` — a flat head-cut that
stops ~2 KB *before* line 2484. So the model saw the function's opening + the symptom and guessed; it even
self-reported `confidence: low` and wrote "the most defensible root cause **in the retrieved context**"
(the anti-fabrication instruction working). Rebuilding the index would not have helped — the offending line
never reached the model.

### Fix (generic, no reindex)
1. **Backend-aware per-chunk cap.** Claude's window is ~200K tokens, so there is no reason to cap chunks at
   4 KB for it — that cap only protects the LOCAL model. `_SNIPPET_PROMPT` is now `4000` for
   `repair_llm_backend == "local"` and `14000` otherwise, so Claude receives WHOLE functions and a bug
   anywhere in a chunk is visible. (The downstream local-window fit-trim that drops whole blocks is unchanged.)
2. **`_focus_snippet` — relevance-centred truncation.** When a CODE chunk still exceeds the cap (the local
   path, or a pathologically large function), keep the head (signature/opening) PLUS the contiguous line
   window with the MOST failure/interaction signal (a sliding max-sum over `_subtokens` line scores, using
   `_signal_tokens(_failure_point_text) ∪ _subtokens(_interaction_query)`). A sliding *window* (not a single
   centre line) captures a dense region — e.g. an element's whole render block — even when the actual buggy
   line inside it is itself low-signal (`setMockMode(false)` scores ~1). Design docs keep the plain head-cut
   (prose is read top-down). Strict no-op when a chunk fits or its tail carries no signal → no regression.

Verified on the real on-disk `PaymentScreen` chunk: the plain 4 KB cut drops `setMockMode(false)`; the
14 KB Claude cap sends the whole chunk; the 4 KB local focus keeps the mock-card region including the bug.

### Also confirmed / advised
- **Index rebuild is still required after a branch switch** (the index reflects code at BUILD time). The
  user had switched the POS working tree from the repair branch back to `expanded-cloud-agnostic` without
  rebuilding — good hygiene to rebuild, but it was NOT the cause of this miss (truncation was).
- The diagnose model now emits `confidence`/`root_cause`; a `low` confidence with the guard is the tell that
  the context lacked the cause — surfaced in the Executive Summary confidence rings.

### No-regression
`pytest tests/` = 139 passed (1 new: `test_focus_snippet_keeps_a_deep_bug_line_that_a_head_cut_drops`) +
same 8 pre-existing fixture failures. Additive/default-safe: only over-cap code chunks change, and only by
KEEPING more of the relevant region.

---

## 2026-09-21 (UI + note) — report font legibility + "running POS = built dist, not source branch"

- **Fix:** the Auto-Repair Detailed-report code/output boxes rendered with bare `monospace` and no explicit
  text colour, so on the dark theme (`--text` on `--bg`) the step-by-step agent text could be faint/invisible.
  All boxes (`box`, `codeBox`, the RCA/retrieve `<pre>` snippets, the diagnose diff, `DiffBlock`) now use a
  legible monospace stack (`MONO`) + explicit `color: var(--text)` + `line-height: 1.5` and a slightly larger
  size. Studio build clean; no logic change.
- **Note (recurring confusion): the POS app the browser runs is the COMPILED `dist` baked into the nginx
  image at `docker build` time** (POS `Dockerfile` is a 2-stage build → `COPY --from=build /app/dist`), NOT
  the live source. `git status` shows the SOURCE branch; it can differ from the running image if the branch
  was switched without `docker compose up -d --build`. To confirm what's actually running: rebuild the POS
  from the current branch + hard-refresh, or behaviour-probe. The **quantity guard `if (quantity <= 1)`** is
  an off-by-one: product quantity starts at 0 (Add disabled), one `+` → 1 → `1 <= 1` true → "Quantity
  Required" popup (the bug); `+` twice → 2 → adds. In the 11:06 run the cart verify PASSED via
  `method=tier3_bridge` — evidence the bug WAS hit and Tier-3 vision auto-recovered it (incremented/re-added
  to reach the cart), which is why the run continued to the genuine mock-card failure instead of failing at
  add-to-cart.

---

## 2026-09-21 (Option C tuning) — fail real defects fast instead of masking them with a Tier-3 bridge

After the mock-card fix landed, a re-run showed the *quantity* bug no longer failed the test: the popup
auto-dismisses to a normal `products` screen, so the Option C vision judge saw nothing wrong, called it a
"gap", and Tier-3 bridged (re-incremented + re-added) to reach the cart. Net: a real defect was silently
recovered instead of triggering Auto-Repair. Two levers added to push borderline wrong-screen verifies to
fail fast — **both default ON, both configurable** (no-regression: disable/loosen to restore prior bridging).

### (1) Judge confidence threshold — `verify_bridge_min_confidence` (default `high`)
`classify_wrong_screen_failure` now returns `(category, confidence, observation)` (confidence high|medium|
low; prompt asks for it). The caller BRIDGES only when the verdict is `gap` AND its confidence ≥
`verify_bridge_min_confidence`. A `defect` (any confidence) OR a lower-confidence `gap` fails fast (marks
`sr["verify_defect"]` → the outer handoff is terminal → Auto-Repair). Default-safe: on judge error / no
image it returns `("gap","high",…)`, so an unavailable judge bridges exactly as before. `_conf_at_least`
implements the ordering (unknown confidence → lowest; unknown threshold → strictest).

### (2) Unresponsive-interaction rule — `verify_unresponsive_interaction_defect` (default True)
Deterministic, no LLM, runs BEFORE the judge (cheap + authoritative). `_last_interaction_screen` returns the
`screen_id` of the most recent structured tap/type; if the app is STILL on that screen at the failed
wrong-screen verify, the interaction did not advance the flow (a blocked / refused / unresponsive control)
→ defect, fail fast. Conservative: only structured (app_map) interactions record a `screen_id`, so it fires
only when we know where the interaction happened; a true nav gap (the interaction advanced off its screen,
or there was no preceding interaction) is not flagged and bridges as before.

Wiring: both live in the `_screen_only` verify branch of `run_vision_step`; when neither fires, the bridge
runs unchanged. The design tradeoff (more fail-fast = fewer silent self-heals) is called out in CLAUDE.md.

### No-regression
`pytest tests/` = 141 passed (2 new in `tests/test_verify_defect_gate.py`: `_conf_at_least`,
`_last_interaction_screen`) + same 8 pre-existing fixture failures. Text/value assertions (`_screen_only`
false) never enter this path, so the cross-kiosk VALUE demos are untouched; loosening either knob restores
the prior always-bridge behaviour.

---

## 2026-09-21 (RCA display + failure enrichment + report font) — "Invalid test case" mislabel & legibility

After the quantity bug started failing fast into Auto-Repair, the repair **succeeded** (build passed, PR
raised, "Bug fixed & verified"), but the report's ROOT CAUSE tile read **"Invalid test case"**, and the
report boxes were still hard to read on the dark theme.

### Why RCA said "test_invalid" (and why it was harmless but confusing)
The debug dump showed `verdict=test_invalid confidence=medium stop=False`. Two causes:
1. **The failure text RCA saw was too thin.** The unresponsive-interaction rule fails the verify FAST, and
   its informative reason ("the add-to-cart interaction on 'products' did not advance — an unresponsive/
   blocked control") was being **dropped**: `observation = observation or _defect_reason` kept the bland
   pre-set "Wrong screen: expected 'cart', got 'products'" and discarded the reason. So RCA (reading the
   design doc, which says add-to-cart stays on Products until you tap Cart/Checkout) reasonably concluded
   the TEST wrongly expected the cart too early → `test_invalid`.
2. It was only **medium** confidence, so the conservative gate did NOT stop (correct — no regression); the
   code-fixing agent proceeded and fixed the real code bug.

**Fix A (backend):** PREPEND the defect reason to the observation instead of dropping it, so the failure
text handed to BOTH agents carries the real symptom ("the interaction did not advance — an unresponsive/
blocked control. Wrong screen: expected 'cart', got 'products'"). That steers RCA toward `code_bug`.

### The report contradiction — "Invalid test case" over a verified code fix
`test_invalid` was an **advisory** (non-stopping) verdict, yet the Executive Summary showed it as the
headline ROOT CAUSE — contradicting "Bug fixed & verified · patched App.tsx".

**Fix B (frontend):** the ROOT CAUSE tile now reflects the VERIFIED OUTCOME. When a fix was applied AND the
build passed AND RCA did NOT stop the pipeline (`patchVerified`), the tile shows **"Code bug"** (with the
diagnose `root_cause` as the sub-line), and a small muted note records RCA's initial advisory read for
transparency. When RCA actually stopped (high-confidence spec/test/env), its verdict stands. The technical
RCA section still shows RCA's actual verdict verbatim (honest audit trail).

### Report font legibility
**Fix C (frontend):** the Detailed-report window container now sets an explicit `color: var(--text)`, so
every descendant inherits a visible light colour regardless of inherited context (belt-and-suspenders on top
of the per-box `MONO` + `color` fix). NOTE: the studio must be REBUILT/redeployed for the font fix to show —
a backend-only `docker compose up -d --build app` does not rebuild the studio container.

### No-regression
`pytest tests/` = 141 passed + same 8 pre-existing fixture failures; studio build clean. Fix A only enriches
the observation string (additive); Fix B/C are display-only.

---

## 2026-09-21 (human review + plan thumbnails + report legibility)

### Report font (the recurring "invisible text")
The Executive-Summary pipeline stepper labels were dark-on-dark: they live inside native `<button>` elements,
which RESET text colour to a system default (so a window-level `color` can't reach them). Fixed by setting
`color: var(--text)` on the stepper button + label (and, defensively, on the report window container). All
code/output boxes already use the legible `MONO` stack + explicit colour.

### Task 1 — Human-in-the-loop review gates (3 stages, all default OFF)
New `Configuration → Human Review` section with three independent toggles
(`PATCH /api/config/human-review`, persisted; `settings.human_review_explorer/test_plan/rca`, default False
so OFF ⇒ no behaviour change ⇒ no regression):
- **App Explorer** — after an explore finishes, the App Explorer page shows Approve/Reject. **Test Plan
  generation is blocked** (Studio `localStorage.explorer_approved`) until approved. A Reject reason is stored
  per-app and passed to the NEXT explore: `/explore` `review_feedback` → env `EXPLORE_REVIEW_FEEDBACK` →
  `settings.explore_review_feedback` → appended to `SUGGEST_EXPLORABLE_ACTIONS` in `explore_screen.py`.
- **Test Plan** — Approve/Reject under a generated plan; the Reject reason is folded into a **Regenerate**
  (`/tc-plan` `review_feedback` appended to the planning prompt with `force=true`).
- **RCA** — the repair pipeline PAUSES after the RCA verdict, BEFORE the code-fixing agent. Mechanism:
  `rca_node` sets `awaiting_review` when `human_review_rca` is on and the verdict doesn't already stop →
  `_route_after_rca` routes to END → `run_repair` surfaces `awaiting_rca_review` + the full rca →
  `_store_repair_result` parks the job as `awaiting_rca_review` with a `_resume` context. The Studio shows the
  verdict + Approve/Reject (`RcaReviewPanel`). **Approve** → `POST /api/repair/{id}/rca-review` re-invokes
  `run_repair(rca_override=verdict)` in a thread streaming into the SAME job (RCA not re-run; the fixer runs).
  **Reject** → re-invokes with `review_feedback=reason` (RCA reconsiders with the feedback and pauses again);
  the code-fixing agent is never called until an Approve. New graph state: `rca_override`, `review_feedback`,
  `awaiting_review`. Applies to both the auto and manual repair paths.

### Task 2 — Test-plan step thumbnails
Each click/type plan step (`isInteractionStep`) now shows a thumbnail of the ANNOTATED exploration screenshot
for its `screen_id` (newest `screenshots/annotated/<screen>_<ts>.png`); click opens a lightbox. Uses the
existing `GET /api/screenshots/annotated/{file}` + a new `annotatedScreenshotUrl` client helper — no new
backend data (the explorer already produced these labelled frames, and the plan carries `screen_id`/coords).

### No-regression
Every gate is config-gated and default OFF; the RCA pause never triggers unless `human_review_rca` is on, so
the default Claude+Chroma repair runs exactly as before. `pytest tests/` = 141 passed + same 8 pre-existing
fixture failures; studio `npm run build` clean; `api.main` imports clean; repair graph compiles.

---

## 2026-09-23 · Bulk plan generation + Approve/Reject review + approval-gated Execution (studio-only)

A Test-Intake UX change so plans are generated for the whole suite up front, reviewed one at a time with
Approve/Reject, and only APPROVED plans are runnable. Pure frontend (`kiosk-test-studio`); no backend change
(reuses `/tc-plan` with `review_feedback`). No-regression: the classic list/detail view is untouched and the
Execution page falls back to its old `selected_tcs` behaviour whenever no review has been recorded yet.

### Test Intake — three phases (`TestIntake.tsx`)
- **list** — the original test-case table + detail panel + per-TC "Generate Plan" flow, UNCHANGED. A new
  **⚙ Generate Test Plans (N)** action card appears once cases exist (plus a **Review plans →** shortcut when
  cached plans already exist).
- **generating** — clicking Generate runs Claude for EVERY case sequentially with a live progress bar
  (`done / total`, current test id, Cancel). Reuses a valid cached plan; only missing/stale ones call
  `/tc-plan`. On finish → `review`.
- **review** — ONE plan at a time:
  - LEFT: the generated plan (rendered by the same `PlanStep`, so annotated-screenshot thumbnails, required
    inputs, Edit and Regenerate all still work), with the raw test case (description / raw steps / expected
    results) BELOW it. A status-dotted `Pager` (`« First ‹ Prev · 1 2 … N · Next › End »`) under the plan.
  - RIGHT: a **Review** panel — Approve, or Reject → reason textarea → regenerates THAT plan via `/tc-plan`
    with `review_feedback=<reason>` and returns it to `pending` for re-review. Prev/Next buttons too.
  - TOP bar: **Approve All** / **Reject All** (Reject All takes one reason and regenerates every plan with it),
    a running `{approved} approved · {pending} pending of {total}` count, Back-to-list, and Proceed-to-Execution.

### Approval store + Execution gating
- New browser-local `tc_reviews` map (`{test_id: {status:'pending'|'approved'|'rejected', reason?}}`) with
  client helpers `getTcReviews` / `saveTcReviews` / `getTcReview` / `setTcReview` / `getApprovedTcs`. Approving
  syncs `selected_tcs` (approved set, in test-case order) via the existing `checked` effect, so Execution reads
  it with no new wiring.
- **`Execution.tsx`** — `reviewMode` = any review exists. When on: the pool is the APPROVED set, each run-order
  row gets an include/exclude checkbox (excludes stored in `exec_excluded`, pruned to approved, and never
  altering approval), `runIds = approved − excluded`, and Start is disabled when `runIds` is empty (so an empty
  run can never fall through to the backend's "run all"). Drag-reorder, backend selector, preview, and start
  are otherwise unchanged. When OFF (no reviews yet): byte-for-byte the previous `selected_tcs` behaviour.

### No-regression
`npm run build` clean (tsc + vite). The list phase and all existing plan functionality (thumbnails, required
inputs, edit, regenerate, human-review gates) are preserved; Execution is unchanged until the review flow is
used. Rebuild the **studio** container to deploy (a backend-only rebuild does not rebuild the SPA).

---

## 2026-09-23 (revised) · Test Intake review flow overhaul (studio-only)

Follow-up UX changes to the bulk-generate/review flow shipped earlier the same day. Pure frontend
(`kiosk-test-studio`); no backend change. No-regression: Execution still falls back to the old
`selected_tcs` behaviour whenever no review has been recorded, and all plan functionality (annotated
screenshots, required inputs) is preserved.

1. **Removed the per-test selection table + detail panel.** The old "click a test case to generate its
   plan" flow and the selectable list/checkboxes/search/selection-banner are gone. Test Intake now has two
   phases: `setup` (import + Generate) and `review`.
2. **Progress shown inline on the same page.** Clicking **⚙ Generate Test Plans (N)** renders the progress
   bar (`done/total`, current id, Cancel) inside the setup card — no separate/blank screen.
3. **Reject now QUEUES instead of regenerating.** Reject (and Reject All) set `status:'rejected'` + reason
   without calling Claude. A new **🗂 Rejected plans (N)** button opens a floating `RejectedWindow`: a table
   of rejected cases + reasons with select-all / individual checkboxes and a **Regenerate selected** action
   that batch-re-plans them (`/tc-plan` `force` + each case's `review_feedback`), showing its own progress,
   and returns them to `pending`.
4. **Per-step annotated screenshots on the review side.** Under the Approve/Reject box, every interaction
   step's annotated exploration screenshot is rendered inline (with `screen_id · element_id · (px,py)` in the
   caption) so coordinates are verifiable without clicking each thumbnail.
5. **Removed the top pager** — the numbered `Pager` now appears only at the bottom of the plan (Prev/Next
   also remain in the review panel).
6. **Removed the Edit / Regenerate buttons** from the review view (regeneration is now the batch
   Rejected-plans flow). The left plan is read-only.

`human_review_explorer` still gates generation (blocks until the exploration is approved). `npm run build`
clean. Rebuild the **studio** container to deploy.
