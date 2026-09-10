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

## 6. Provision & deploy on GCP (worked runbook)

The exact sequence used to stand up the backend **and** the Kiosk POS on a fresh Google Compute
Engine VM (2026-09-10). Same steps apply to Azure/EC2 — only the VM SKU name and firewall CLI differ.

### 6.1 VM sizing (Azure / GCP equivalents)
| Scenario | vCPU / RAM | Azure | GCP |
|---|---|---|---|
| **Demo/pilot — all-in-one** (app + Postgres + MinIO + Redis on one VM) | 4 / 16 | `Standard_D4s_v5` | `e2-standard-4` |
| **Lean single-tenant** (datastores managed off-box) | 2 / 8 | `Standard_D2s_v5` | `e2-standard-2` |
| **K8s scale-out** | 2/8 **per node**, 2+ nodes | `Standard_D2s_v5` pool | `e2-standard-2` pool |

**Claude needs no GPU** (remote API). RAM is the binding constraint (Chromium ~1 GB + torch embed
model ~2 GB + datastores). A GPU VM is only needed for the *optional* local-LLM Auto-Repair backup.
Keep VM + Cloud SQL/Memorystore/bucket in the **same region** (`us-central1` is the default pick).

### 6.2 Create the VM (GCP console wizard choices)
`e2-standard-4` · region `us-central1`, zone `-a` · **Provisioning: Standard** · time limit **OFF** ·
graceful shutdown **ON** · termination action **Stop** · Boot disk: **Ubuntu 22.04 LTS, x86/64**,
**Balanced PD**, **50 GB** (80 if co-hosting Postgres) · Firewall: **Allow HTTP + HTTPS** · default
service account · Ops Agent optional. (Arch = **x86/64** because `e2` is an x86 machine; an Arm image
would only go on a `t2a`/`c4a`.)

### 6.3 Install Docker + git (Ubuntu 22.04)
```bash
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
sudo apt-get install -y git
sudo usermod -aG docker $USER && newgrp docker
git config --global credential.helper store   # cache the GitHub PAT after first use
```
Both repos are private → clone with a **read-only fine-grained PAT** (paste when prompted for password).

### 6.4 Deploy the POS (app under test) on :80 — the EXPANDED build
```bash
EXTERNAL_IP=$(curl -s ifconfig.me)
git clone -b expanded-cloud-agnostic https://github.com/srik-g/robotics-kiosk-pos.git
cd robotics-kiosk-pos
[ -f card-service/cards.json ] || echo '{}' > card-service/cards.json   # bind-mount seed
PUBLIC_BASE_URL=http://$EXTERNAL_IP docker compose up -d --build         # nginx :80 + card-service
cd ~
```
`expanded-cloud-agnostic` = `expanded-version` features + this packaging. `PUBLIC_BASE_URL` is baked
into the SPA at build time — change the IP ⇒ `docker compose build pos` again.

### 6.5 Deploy the QA backend on :8001
```bash
git clone -b cloud-agnostic-agent https://github.com/HydAIbotix/robotic-vision-agent-claude.git
cd robotic-vision-agent-claude
printf 'ANTHROPIC_API_KEY=%s\n' '<your-real-key>' > .env    # gitignored; compose reads ${ANTHROPIC_API_KEY}
docker compose up -d --build                                 # ~4 min first build (Chromium + torch)
cd ~
```
Compose wires Postgres/MinIO/Redis/worker + sets `ROBOT_BACKEND=playwright` and
`PLAYWRIGHT_HEADLESS=true`. The only hand-created file is `.env`.

### 6.6 Verify
```bash
docker compose -f ~/robotic-vision-agent-claude/docker-compose.yml ps
curl -s http://localhost:8001/api/health        # platform: anthropic/postgres/minio/redis
curl -sI http://localhost | head -1             # POS → HTTP/1.1 200 OK
# Chromium launches in-container (headless):
docker compose exec app python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(); print('Chromium OK:', b.version); b.close(); p.stop()"
docker compose exec app python -c "from vision_agent.config import settings; print('headless =', settings.playwright_headless)"
```

### 6.7 (Optional) expose the API, locked to your IP — run in Cloud Shell
```bash
gcloud compute firewall-rules create allow-kioskqa-api \
  --allow=tcp:8001 --target-tags=http-server --source-ranges=$(curl -s ifconfig.me)/32
```
Never open 8001 to `0.0.0.0/0`. Datastore ports (5432/6379/9000/9001) stay VM-internal by default — correct.

### 6.8 Run an exploration against the POS
```bash
VM_IP=$(hostname -I | awk '{print $1}')                       # internal IP, reachable from the app container
docker compose exec app curl -sI "http://$VM_IP" | head -1    # sanity: container → POS = 200
curl -s -X POST http://localhost:8001/api/explore -H 'Content-Type: application/json' \
  -d "{\"kiosk_id\":\"K-RPS\",\"kiosk_url\":\"http://$VM_IP/?screenLayout=standard&flowMode=full\"}"
curl -s http://localhost:8001/api/explore/<explore_id>        # poll: running → done
curl -s http://localhost:8001/api/app-map | head -c 800
```
`POST /api/explore` upserts the kiosk URL, pre-checks reachability, then spawns the explorer (paid
Claude calls). The **expanded** POS may use different `screenLayout`/`flowMode` — adjust for the
intended RPS/VPS view. `kiosk_id` is the join key for later test cases + runs.

### 6.9 Deploy fixes baked into the branches (why a fresh clone Just Works now)
1. `python-multipart` in `pyproject.toml` (`/api/test-cases/upload` crashed the API import otherwise).
2. `ROBOT_BACKEND=playwright` on app + worker in compose.
3. `PLAYWRIGHT_HEADLESS=true` in compose (+ the `playwright_headless` config flag) — headless VM has
   no X server.
4. POS `card-service/Dockerfile` seeds `cards.json` (it's gitignored, absent from a clone).

### 6.10 Resume checklist (pick up here)
- Backend: `cd ~/robotic-vision-agent-claude && git pull origin cloud-agnostic-agent && docker compose up -d --build`.
- POS: `cd ~/robotics-kiosk-pos && git pull && docker compose up -d --build` (on `expanded-cloud-agnostic`).
- Re-run §6.8 exploration; confirm it completes **headless** (the earlier failure was only the X-server bug).
- Then test execution: `POST /api/test-cases/upload` (Excel) → `POST /api/runs`.
- Operator UI (`kiosk-test-studio`) not deployed — use the API directly, or point a Studio at `http://<EXTERNAL_IP>:8001`.
