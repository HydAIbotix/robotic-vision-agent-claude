# Cloud-Agnostic Architecture — Decision Document

**Branch:** `cloud-agnostic-agent` (backend) · created from `mvp-vision-agent`
**Status:** foundation implemented (Phase 0); see "Implementation status" at the end
**Audience:** engineering + solution architecture
**Last updated:** 2026-09-09

> **Purpose.** The `aws-based` branch runs the platform natively on AWS (DynamoDB, S3, AgentCore,
> Step Functions, API Gateway, Lambda). This document defines the **cloud-agnostic** equivalent: the
> same functionality using open-source, run-anywhere components so the **one codebase** deploys on
> **Azure, Google Cloud, AWS EC2, or on-prem** with no lock-in. **Claude is unchanged** — it is a
> remote API call (`VISION_BACKEND=anthropic` via the Anthropic API, or `bedrock` on AWS) and powers
> the App Explorer, Test Planner, and Auto-Repair exactly as today, wherever the code runs.

---

## 1. Cloud-agnostic components

Every managed-AWS component maps to an open-source, portable equivalent, **selected by config
alone** (ports-and-adapters). Defaults reproduce the pure-local MVP, so nothing changes until a
backend is switched.

| # | AWS-based component | Purpose | Cloud-agnostic replacement | Runs on | Selected by (config) |
|---|---|---|---|---|---|
| 1 | **Bedrock** (Claude) | Reasoning + vision core | **Anthropic API** (direct) | Any cloud / on-prem | `VISION_BACKEND=anthropic` |
| 2 | **DynamoDB** | Relational metadata (runs, results, defects, config, test cases) | **PostgreSQL** (managed or self-hosted) | Azure DB for PostgreSQL · Cloud SQL · RDS · any | `PERSISTENCE_BACKEND=postgres` + `DB_URL` |
| 3 | **S3** | Blobs: app_map, screenshots, results, plans | **MinIO** (self-hosted) or any **S3-compatible** store (GCS interop, Azure via S3 gateway) | Anywhere | `STORAGE_BACKEND=minio\|s3\|gcs\|azure` + `S3_ENDPOINT_URL` |
| 4 | **API Gateway + Lambda (Mangum)** | HTTP front | **FastAPI/uvicorn as a normal container** behind an ingress (NGINX/Traefik) | Any container runtime | (default — the app *is* an ASGI service) |
| 5 | **API GW WebSocket + DynamoDB Streams → Lambda** | Realtime push | **FastAPI native WebSocket** + **Redis pub/sub** (cross-replica fan-out) | Azure Cache · Memorystore · ElastiCache · self-hosted | `EVENT_BUS_BACKEND=redis` + `REDIS_URL` |
| 6 | **AgentCore Runtime** | 5 agent execution envs | **Containers** + a **Redis task queue** (`SERVICE_ROLE=worker` consumer) | Any container runtime | `TASK_QUEUE_BACKEND=redis` + `SERVICE_ROLE=worker` |
| 7 | **AgentCore Browser** | Managed Chrome/CDP | **Self-hosted Playwright/Chromium** (already the `playwright` backend) | In the worker image | `ROBOT_BACKEND=playwright` |
| 8 | **AgentCore Memory** | Semantic memory | **pgvector** (in Postgres) or **Chroma** (`ports/memory.py`) | Anywhere | `MEMORY_BACKEND=pgvector\|chroma` |
| 9 | **AgentCore Gateway** | Tool serving (OpenCV detection) | **In-process call** (local default) or an internal microservice/MCP | In the image | (in-process) |
| 10 | **AgentCore Identity** | Tenant-scoped creds | **HashiCorp Vault** / K8s Secrets / any managed secret store behind env | Anywhere | `MULTI_TENANT_ENABLED` + secret store |
| 11 | **AgentCore Observability** (CloudWatch) | GenAI traces | **OpenTelemetry** → Jaeger/Grafana Tempo, or **Langfuse** (`ports/tracing.py`) | Anywhere | `TRACING_BACKEND=otel\|langfuse` |
| 12 | **Step Functions** | Orchestration / fan-out | **In-process** (via the task queue) → **Temporal** for durable scale-out (`ports/orchestration.py`) | Anywhere | `ORCHESTRATOR_BACKEND=inprocess\|temporal` |
| 13 | **ECR** | Image registry | **GHCR / Harbor / ACR / GAR / Docker Hub** | Anywhere | (CI config) |
| 14 | **CloudFront + S3** (frontend) | SPA hosting | **NGINX serving the static build**, or Cloudflare Pages / any CDN | Anywhere | (studio/POS build) |
| 15 | **Fargate** (card-service) | Container runtime | A container on K8s / Compose | Any container runtime | (manifest) |
| 16 | **CDK** | IaC | **Docker Compose** (single host) + **Kubernetes manifests / Helm** (cluster); **Terraform** for cloud primitives | Anywhere | which files you apply |
| 17 | **GitHub Actions + OIDC** | CI/CD | **GitHub Actions is already agnostic** (or GitLab CI / Jenkins) | Anywhere | workflow files |

**Design principle.** Each replacement sits behind a **port** (interface) chosen at call time from
`vision_agent/config.py`. No caller imports a concrete backend, so swapping DynamoDB→Postgres or
S3→MinIO or memory→Redis is a config change, not a code change — and adding a *new* backend later
means adding one adapter, not touching call sites. This is what keeps the components **replaceable
and loosely coupled**, and keeps the change surface small if a component or model changes.

---

## 2. Multi-tenancy: three customers, two deployment models, one codebase

Take three customers: **TransitCo** (ticket kiosks), **RetailMart** (self-checkout), **BankX**
(account kiosks). Each `tenant_id` prefixes every object-store key and DB row for isolation.

### Model A — Pooled SaaS (all three in *our* cloud, one deployment)

One deployment, one Postgres, one object store, one app cluster. The three are **logical tenants**
separated by `tenant_id`. Cheapest for us; we operate everything; isolation is logical (keys + auth).

```
                         our cloud (one deployment)
   ┌──────────────────────────────────────────────────────────────┐
   │  ingress → API replicas → workers → Claude (Anthropic API)     │
   │      │                                                         │
   │   Postgres          Object store (MinIO/S3)      Redis         │
   │   transitco#run-42  tenants/transitco/screens/   run:transitco │
   │   retailmart#run-7  tenants/retailmart/screens/  run:retailmart│
   │   bankx#run-3       tenants/bankx/screens/       run:bankx     │
   └──────────────────────────────────────────────────────────────┘
   Isolation = tenant_id-leading keys + tenant-scoped auth (logical)
```

- Config: `MULTI_TENANT_ENABLED=true`. Each request carries its tenant (`X-Tenant-Id` header / JWT
  claim); `ports/tenancy.py` binds it and `tenant_key()` namespaces storage.
- Best for **small / mid customers** and fast onboarding — the near-term target.

### Model B — Dedicated / self-hosted (each customer in *their own* cloud)

Each customer deploys the **same image + manifests** into **their** Azure / GCP / AWS account. Now
each deployment serves exactly one customer, so the **cloud-account boundary IS the isolation** —
the strongest possible (BankX's data never leaves BankX's account). Customers ask for this for
network reach (the Explorer must hit *internal* kiosk URLs and private Git repos), data residency,
and compliance.

```
   TransitCo's cloud        RetailMart's cloud         BankX's cloud
   ┌───────────────┐        ┌───────────────┐          ┌───────────────┐
   │ full stack     │        │ full stack     │          │ full stack     │
   │ tenant=default │        │ tenant=default │          │ tenant=default │
   │ (or their teams)│       │ (or their teams)│         │ (or their teams)│
   └───────────────┘        └───────────────┘          └───────────────┘
   Isolation = separate cloud accounts (physical)
```

- Config: `MULTI_TENANT_ENABLED=false` → one implicit `default` tenant. `tenant_id` optionally
  separates that customer's **own** sub-teams/brands.
- Best for **regulated / network-isolated / enterprise** customers (e.g. BankX).

### One codebase for both

`ports/tenancy.py` makes this a **config choice, not a fork**:

| | Pooled SaaS (Model A) | Dedicated (Model B) |
|---|---|---|
| `MULTI_TENANT_ENABLED` | `true` | `false` |
| `current_tenant()` returns | per-request tenant | always `default` |
| `tenant_key("results/run.json")` | `tenants/transitco/results/run.json` | `tenants/default/results/run.json` |
| Runs where | our cloud | customer's cloud |
| Isolation | logical (keys + auth) | physical (accounts) |

Because single-tenant mode makes `current_tenant()` a constant, **call sites prefix keys/rows
unconditionally** and behave correctly in both models — no branching in business logic. Keeping
`tenant_id` first-class from day one is the single decision that lets one build serve both
go-to-market motions and lets a customer migrate between them without a data reshape.

> **Why the cloud-agnostic work enables Model B.** Model B is only feasible if the app isn't welded
> to AWS. The Postgres + MinIO + container stack, packaged as **Compose / Helm / Terraform**, is
> exactly what you hand a customer to stand up in *their* cloud. The `aws-based` branch supports only
> Model A, in *our* AWS account.

---

## 3. When to use Docker vs. Kubernetes

Both run the **same image** with the **same env vars** — the only difference is the orchestrator, so
switching is a deploy-time choice, not a rebuild.

| Situation | Use | Why |
|---|---|---|
| Demo / pilot / a few low-volume tenants | **Docker Compose** (single host) | One `docker compose up`; minimal ops; the whole stack (Postgres+MinIO+Redis+app) on one VM |
| One dedicated customer, modest load | **Docker Compose** or a small managed container service | Simplicity beats elasticity at this scale |
| Horizontal scale-out (many concurrent explorations / runs) | **Kubernetes** | Run N API + N worker replicas; spread across nodes |
| Bursty load (quiet nights, heavy release days) | **Kubernetes (HPA)** | Auto-scale pods up/down; scale API and worker tiers independently |
| Fan-out of parallel executors (the supervisor pattern) | **Kubernetes** | Each executor a pod, scheduled across the cluster |
| Per-tenant resource isolation / quotas | **Kubernetes** | Namespaces + CPU/memory quotas per tenant |
| Zero-downtime deploys across many services | **Kubernetes** | Rolling updates, health probes, self-healing |

**Rule of thumb:** **one VM (Docker Compose) → a handful of tenants / bounded concurrency.
Kubernetes → you've outgrown one box, or you need elasticity + independent API/worker scaling +
per-tenant quotas.** Don't adopt K8s before the scale demands it — it is pure operational cost until
then. Lighter stops between the two (a managed container service, Nomad) are valid.

**Easy switch (by construction):**
- `docker-compose.yml` and `deploy/k8s/*` read the **same env keys**; moving a value from the
  Compose `environment:` block to a K8s `ConfigMap`/`Secret` is the whole migration.
- State is external (Postgres / object store / Redis), so pods/containers are stateless and
  disposable in both.
- `deployment_mode` (`docker`|`k8s`) is surfaced in `/api/health` so you can confirm what a given
  environment resolved to.

---

## Implementation status (see `CLAUDE.md` for the running log)

- **Done (Phase 0 — foundation, no regression):** config-driven backend selection for persistence /
  object store / event bus / tenancy; S3-compatible object store (MinIO/GCS/Azure) via
  `S3_ENDPOINT_URL`; `ports/event_bus.py` (memory|redis) and `ports/tenancy.py` (single|multi);
  `/api/health` reports active backends; `Dockerfile` + `docker-compose.yml` + `deploy/k8s/`;
  `.[cloud]` extra; unit tests proving defaults == MVP.
- **Done (Phase 1a — tenant-scoped storage + Postgres confirmed):** every blob **storage call site**
  is now tenant-aware, **gated on `MULTI_TENANT_ENABLED`** (single-tenant = byte-identical to the
  MVP): the object store (`get_storage()` writes wrapped by `_TenantScopedStorage`), and the
  filesystem roots — `app_map`, `results`, `screenshots`, `test_plans` — via `ports/paths.py`
  accessors wired through `api/main.py` (all `settings.app_map_path` refs, results/screenshots/plan
  helpers, reset, run counter), `test_runner/plan_cache.py`, and `finalize_tests.py`. The tenant is
  captured in the request context and re-bound across the run-thread and explorer-subprocess
  boundaries (`APP_MAP_PATH` env). **Postgres for persistent data is confirmed**: models use only
  PG-safe SQLAlchemy types, `db_url` selects the engine (`postgresql+psycopg://…`), and `init_db()`
  now logs the active engine and warns on a `persistence_backend`/`db_url` mismatch. Compose/K8s set
  Postgres by default. Tests: 109 passed (5 new scoping tests), same 8 pre-existing env-only fails.
- **Done (Phase 1b — row-level DB isolation):** every business table carries a `tenant_id` (via a
  `TenantMixin`, default `"default"`, indexed). Two **global SQLAlchemy session events**
  (`api/database.py::_register_tenant_scope`) enforce isolation with **no edits to the ~40 query
  sites** and **gated on `MULTI_TENANT_ENABLED`**: a `do_orm_execute` filter adds
  `WHERE tenant_id = current_tenant()` to every SELECT/UPDATE/DELETE, and a `before_flush` stamps
  `tenant_id` on inserts. Single-tenant = no filter/stamp (byte-identical to the MVP). An idempotent
  migration backfills the column on existing DBs (verified on the live `management.db`). Row isolation
  now matches the blob isolation. **Caveat:** existing unique constraints (`kiosk_id`/`test_id`/
  `run_id`/`alias`) remain GLOBAL; reusing the same id across tenants in a pooled DB needs a composite
  `(tenant_id, <key>)` unique migration on Postgres — a scoped follow-up.
  Request binding is done: a pure-ASGI `_TenantASGIMiddleware` binds the `X-Tenant-Id` header per
  request (gated), so DB queries + blob writes are tenant-correct end-to-end through the API.
- **Done (Phase 1c — the four follow-ups):**
  1. **Composite per-tenant uniques** — natural keys (`kiosk_id`/`test_id`/`run_id`/`alias`) are now
     unique `(tenant_id, key)`, and the FKs that reference them (`app_maps`, `test_results`,
     `defects` → parents) are composite. Two tenants can reuse the same id; a same-tenant duplicate is
     rejected. Fresh SQLite/Postgres get this from `create_all`; single-tenant behaves like the old
     global unique. Existing-pooled-Postgres migration SQL is in `CLOUD_AGNOSTIC_DEPLOY.md`.
  2. **JWT-claim tenant resolution** — `TENANT_JWT_ENABLED` reads the tenant from a signed
     `Authorization: Bearer` JWT claim (PyJWT, lazy import), with the `X-Tenant-Id` header as
     fallback; an invalid token never 500s (falls back).
  3. **Supervisor / CLI path** — `supervisor/worker.py` uses the tenant-scoped app_map;
     `run_parallel.py` binds the tenant from `TENANT_ID` and writes its aggregate under the tenant's
     results root; worker threads re-bind from the env.
  4. **Redis-backed WebSocket** — `_broadcast` now PUBLISHES to the event bus and each replica
     SUBSCRIBES on WS connect, so live run/step/repair events reach clients on ANY replica (Redis
     bus) — the missing piece for horizontal scale-out. In-memory bus = the original single-replica
     behaviour (verified end-to-end). Channels are tenant-namespaced.
- **Done (Phase 2 — the AgentCore-layer analogues, all config-gated to MVP defaults):**
  1. **Queue-driven worker** — `ports/queue.py` (`inline`=in-process thread, MVP | `redis`=API
     enqueues, `SERVICE_ROLE=worker` consumes). `start_run` submits through `ports/orchestration.py`;
     `python -m worker` is the consumer (`docker-entrypoint.sh` dispatches by role). Composes with the
     Redis WS: the worker publishes, the API replica delivers.
  2. **Temporal orchestration** (optional) — `orchestrator_backend=temporal` runs the suite as a
     durable Temporal workflow/activity wrapping the SAME run handler (`orchestration/temporal_app.py`,
     `python -m worker --temporal`). Lazy `temporalio`; default `inprocess` = the queue path.
  3. **Tracing** — `ports/tracing.py` (`none` | `otel` OTLP spans | `langfuse` LLM traces), wired into
     the central `vision_agent/llm.invoke_json` via `llm_callbacks()`; lazy SDKs, no-op by default.
  4. **pgvector Memory** — `ports/memory.py` (`none` | `chroma` | `pgvector`), populated by the App
     Explorer's finalize (gated); tenant-namespaced; reuses the repair embedding model.
- All four are optional extras in `pyproject.toml` (`[temporal]`, `[tracing]`, `[memory]`) and appear
  in `/api/health`. Nothing is pulled in or changes until a backend is selected.
- **Done (Phase 2b — hardening):** (a) **per-test fan-out** in the Temporal workflow — when the
  payload carries `fanout_test_ids`, one `run_test` activity runs PER test in parallel, each an
  independent sub-run (`<parent>::<test_id>`) so shards never race; (b) **OTel spans around agent
  work** — a run-level `agent.run.execute` span in `_execute_run` with nested per-call `llm.invoke`
  spans (labelled planner/explorer/validate/repair) at the central LLM path; (c) **memory-informed
  exploration** — the explorer recalls semantically-similar prior screens (`ports/memory.search`)
  when `MEMORY_BACKEND` is set, completing the write(finalize)/read(explore) loop.
- **Diagrams:** a component view + a run sequence view are in **`docs/Cloud_Agnostic_Architecture.docx`**.
- The cloud-agnostic build now has a complete, config-selected analogue of every AWS-native
  component. Remaining ideas are product-level (a UI for tenant/deploy management, per-tenant billing
  from trace/token data) rather than infrastructure.

See **`CLOUD_AGNOSTIC_DEPLOY.md`** for step-by-step deployment on Azure / Google / EC2 and a
code-level walkthrough of every change.
