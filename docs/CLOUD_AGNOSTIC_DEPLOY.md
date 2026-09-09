# Cloud-Agnostic Deployment & Code Walkthrough

**Branch:** `cloud-agnostic-agent`  ·  **Last updated:** 2026-09-09

This guide deploys the platform on **Azure, Google Cloud, or AWS EC2** — the steps are **the same on
all three**, which is the whole point of "cloud-agnostic." It ends with a code-level walkthrough of
exactly what changed versus `mvp-vision-agent`.

> **Claude is unchanged.** Set `VISION_BACKEND=anthropic` and an `ANTHROPIC_API_KEY`; the App
> Explorer, Test Planner, and Auto-Repair call Claude as a remote API from wherever the app runs.

---

## 0. The mental model

The app needs four things, each a swappable backend:

| Capability | Backend (this guide) | Env keys |
|---|---|---|
| Reasoning/vision | Claude via Anthropic API | `VISION_BACKEND=anthropic`, `ANTHROPIC_API_KEY` |
| Relational metadata | PostgreSQL | `PERSISTENCE_BACKEND=postgres`, `DB_URL` |
| Blob/object store | MinIO or any S3-compatible | `STORAGE_BACKEND=minio`, `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_USE_PATH_STYLE` |
| Realtime event bus | Redis | `EVENT_BUS_BACKEND=redis`, `REDIS_URL` |

Everything else (multi-tenancy, deployment mode) is config too. `/api/health` echoes the resolved
backends so you can confirm any environment at a glance.

---

## 1. Fastest path — one VM (Azure / GCE / EC2), Docker Compose

Identical on all three clouds. Provision **one Linux VM** (2 vCPU / 8 GB is comfortable; more for
heavy Chromium exploration), install Docker + the Compose plugin, then:

```bash
git clone <repo> && cd robotic-vision-agent-claude
git checkout cloud-agnostic-agent
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env      # the only secret Compose needs
docker compose up --build -d
curl http://localhost:8001/api/health           # "platform" block shows postgres/minio/redis
```

Compose brings up **postgres + minio + redis + app** with all env wired (see `docker-compose.yml`).
That's the entire stack — the agnostic equivalent of DynamoDB + S3 + realtime, self-hosted.

**Cloud-specific bits (the ONLY differences):**
- **Azure:** VM = *Azure VM* (`Standard_D2s_v5`/`D4s_v5`); open port 8001 (or front with *Azure
  Application Gateway*). Optional managed: *Azure Database for PostgreSQL*, *Azure Cache for Redis*,
  *Blob via S3 gateway* — just change the env values.
- **Google:** VM = *Compute Engine* (`e2-standard-2`/`-4`); firewall rule for 8001. Optional managed:
  *Cloud SQL*, *Memorystore*, *GCS* (S3 interoperability mode → `S3_ENDPOINT_URL=https://storage.googleapis.com`).
- **AWS EC2:** `t3.large`; security-group rule for 8001. Optional managed: *RDS*, *ElastiCache*, *S3*
  (leave `S3_ENDPOINT_URL` blank and use the instance IAM role — no static keys).

To use a **managed** backend instead of the in-Compose one, delete that service from Compose and
point its env keys at the managed endpoint. No code change.

---

## 2. Scale-out path — Kubernetes (AKS / GKE / EKS)

Same image, env moved into a `ConfigMap` + `Secret`. Full steps in **`deploy/k8s/README.md`**. In
short:

```bash
docker build -t REGISTRY/kioskqa:latest . && docker push REGISTRY/kioskqa:latest
# set image + endpoints in deploy/k8s/configmap.yaml; fill secret.yaml
kubectl apply -f secret.yaml
kubectl apply -k deploy/k8s/          # namespace + config + api(HPA) + worker + ingress
kubectl -n kioskqa get pods
```

- **API tier** auto-scales on CPU (HPA, 2→10). **Worker tier** scales independently.
- Point `DB_URL`/`S3_*`/`REDIS_URL` at **managed** services in production (see the table in
  `deploy/k8s/README.md`).
- Use K8s only when you actually need horizontal scale / independent tiers / per-tenant quotas —
  otherwise stay on Compose (see the Docker-vs-K8s section of `CLOUD_AGNOSTIC_DECISION.md`).

---

## 3. Multi-tenant vs. dedicated

- **Pooled SaaS (many customers, our cloud):** set `MULTI_TENANT_ENABLED=true`. Clients send
  `X-Tenant-Id` (or a JWT claim); the app isolates every key/row by tenant.
- **Dedicated (customer's own cloud):** keep `MULTI_TENANT_ENABLED=false`. Hand the customer this
  repo + `docker-compose.yml` (or `deploy/k8s/`); they run it in their account. One implicit
  `default` tenant; the cloud account is the isolation boundary.

Same image both ways — see `CLOUD_AGNOSTIC_DECISION.md` §2.

---

## 4. Frontend + POS (studio, VPS/RPS)

The operator studio (`../kiosk-test-studio`, branch for AWS work) and the POS apps
(`../Kiosk_App/robotics-kiosk-pos`) are static SPAs. Cloud-agnostic hosting: build them and serve
the bundle from **NGINX in a container** (or any static/CDN host — Cloudflare Pages, an object-store
static site). Set the studio's API base (`VITE_API_BASE_URL`) to the deployed backend URL. (These
repos are versioned on their own branches; this backend guide does not modify them.)

---

## 5. Code walkthrough — what changed vs. `mvp-vision-agent`

All changes are **additive and config-gated**; with default env the app is byte-identical to the MVP
(the test suite proves it). Files touched:

### `vision_agent/config.py` — the single source of truth
A new **"CLOUD-AGNOSTIC DEPLOYMENT"** section adds:
- `persistence_backend` (`sqlite|postgres`) — readable alias alongside the existing `db_url`.
- `s3_endpoint_url`, `s3_region`, `s3_access_key_id`, `s3_secret_access_key`, `s3_use_path_style` —
  make the S3 backend talk to **any** S3-compatible store; all inert when blank (real-AWS default).
- `event_bus_backend` (`memory|redis`) + `redis_url`.
- `multi_tenant_enabled` + `default_tenant_id`.
- `deployment_mode`, `service_role`.
- `storage_backend` widened to `local|s3|minio|gcs|azure`.

Nothing reads `os.environ` directly — every knob is a `Settings` field, per repo convention.

### `vision_agent/storage/aws.py` + `__init__.py` — S3 → any S3-compatible store
`S3Storage.__init__` now accepts `endpoint_url`, `region`, static keys, and `path_style`, building a
boto3 client for **MinIO / GCS / Azure-via-S3** as well as real AWS S3. `get_storage()` routes
`s3|minio|gcs|azure` to this one class (endpoint decides the target) and keeps `local` as default.
**Unchanged when `STORAGE_BACKEND=local`.**

### `ports/` — new capability seams (ports & adapters)
- `ports/event_bus.py` — `EventBus` interface with `InMemoryEventBus` (MVP behaviour, default) and
  `RedisEventBus` (pub/sub across replicas). `get_event_bus()` builds the singleton from config;
  `redis` is imported **lazily** so it's not a hard dependency until selected. This is the agnostic
  analogue of "API-GW WebSocket + DynamoDB Streams → Lambda push."
- `ports/tenancy.py` — `current_tenant()`, `set_current_tenant()`, `tenant_key()`, and a FastAPI
  `tenant_dependency`. Single-tenant mode makes `current_tenant()` a constant (`default`), so call
  sites can prefix keys/rows unconditionally in both deployment models.

### `api/main.py` — observability
`/api/health` now returns a `platform` block (active vision/persistence/object-store/event-bus/
tenancy/deployment backends) so an operator can confirm what a deployment resolved to. No other
route changed.

### Packaging & infra (new files)
- `pyproject.toml` — new `[cloud]` extra: `psycopg[binary]`, `redis`, `boto3` (all optional).
- `Dockerfile`, `.dockerignore` — one portable image (uvicorn without `--reload`, per CLAUDE.md).
- `docker-compose.yml` — full agnostic stack (postgres + minio + redis + app) on any Docker host.
- `deploy/k8s/` — `namespace`, `configmap`, `secret.example`, `api-deployment` (+ Service + HPA),
  `worker-deployment`, `ingress`, `kustomization`, `README`. Same env as Compose.
- `tests/test_cloud_agnostic.py` — asserts defaults == MVP, plus tenancy + event-bus behaviour.

### What did **not** change
The five LangGraph agents, the robot backends, the 3-tier planner, the coordinate/tap pipeline, the
Auto-Repair pipeline, and every existing API route are untouched. Postgres works through the existing
SQLAlchemy layer (`db_url`), so no ORM rewrite was needed (the `store/` DynamoDB rewrite on
`aws-based` is unnecessary for Postgres).

### Phase 1a additions — tenant-scoped storage + Postgres confirmation
- **Object store + filesystem blob roots are tenant-scoped**, gated on `MULTI_TENANT_ENABLED` so
  single-tenant stays byte-identical: `ports/tenancy.py` (`scoped`, `scoped_dir`), `ports/paths.py`
  (`app_map_path`/`results_dir`/`screens_dir`/`test_plans_dir`), a `_TenantScopedStorage` wrapper in
  `vision_agent/storage/`, and wiring across `api/main.py` (incl. all `app_map_path` refs, the run
  thread, and the explorer subprocess via `APP_MAP_PATH`), `test_runner/plan_cache.py`, and
  `finalize_tests.py`.
- **Postgres confirmed:** `init_db()` logs the active engine and warns on a `PERSISTENCE_BACKEND`
  vs `DB_URL` mismatch; models are PG-safe as-is.
### Phase 1b/1c additions — row isolation, composite uniques, JWT, supervisor, Redis WS
- **Row-level `tenant_id`** on every table + auto filter/stamp session events (gated), idempotent
  migration; **composite per-tenant uniques** `(tenant_id, key)` with composite FKs.
- **JWT tenant resolution** (`TENANT_JWT_*`), **supervisor/CLI** tenant binding (`TENANT_ID`), and a
  **Redis-backed WebSocket** so realtime works across replicas (`EVENT_BUS_BACKEND=redis`).

#### Migrating an EXISTING pooled Postgres DB to composite uniques
Fresh DBs get composite uniques from `create_all`. For a DB already created with the old
single-column uniques, run once (Postgres; adjust the auto-generated old constraint names via
`\d <table>`):
```sql
-- example for test_runs.run_id → (tenant_id, run_id)
ALTER TABLE test_runs DROP CONSTRAINT test_runs_run_id_key;
ALTER TABLE test_runs ADD CONSTRAINT uq_test_runs_tenant_run UNIQUE (tenant_id, run_id);
-- repeat for kiosk_configs(kiosk_id), test_cases(test_id), device_configs(alias);
-- then recreate the dependent FKs (app_maps, test_results, defects) as composite (tenant_id, run_id/kiosk_id).
```
Single-tenant SQLite dev keeps its inline global unique — no action needed.

- **Still staged** (config-gated): a queue-driven `SERVICE_ROLE=worker` consumer, Temporal, OTel —
  see the "Next" list in `CLOUD_AGNOSTIC_DECISION.md`.
