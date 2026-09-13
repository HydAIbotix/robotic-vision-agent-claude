# Cost Model & Indicative Pricing — Google Cloud Platform

*Haibotix Agentic SDLC Platform — single-tenant VM and multi-tenant Kubernetes*

> Companion to `GCP_Cost_Estimate.docx` (customer-facing). Version 1.0 — Haibotix Solutions Engineering.
> All figures are **indicative Google Cloud / Vertex AI list prices (us-central1, Sep 2026)** — validate against live pricing.

## 1. Overview

Claude provides general visual understanding of application screens with no per-application model
training. The platform runs on Google Cloud — Compute Engine or GKE for compute, Cloud SQL + Cloud
Storage for data, Memorystore + Cloud Load Balancing for scale. Caching-first execution keeps ongoing
AI cost low; data stays within the customer's chosen Google Cloud project and Region.

- **Predictable, low run cost** — steady-state execution runs without AI calls; recurring cost scales with the number of apps and tests, not run frequency.
- **Two deployment shapes** — a single always-on VM for one customer; a shared Kubernetes cluster for many.

## 2. How cost is determined

Total = **Software & AI** (one-time discovery + planning; ongoing validation + self-healing by Claude)
+ **Infrastructure** (compute, DB, cache, storage, networking, operations). Because stable tests run
from cache with no AI calls, recurring cost grows with apps/screens/tests — not run frequency.

## 3. The intelligence layer — Claude on Google Cloud

Claude runs via **Vertex AI** (managed, in-Region, private). Indicative rates: **~$5 / M input tokens,
~$25 / M output tokens**. What's sent to the model is minimal:

| Stage | Sent to the AI model | When |
|---|---|---|
| Application discovery | Screenshots + on-screen element text | Once per app (and on major UI change) |
| Test planning | The application model as text (no image) | Once per test, then cached |
| Outcome validation | A single screenshot | Only for unfamiliar screens / failures |
| Self-healing | Failure description + retrieved code snippet | Only when a test fails |

### 3.1 One-time onboarding — App Explorer & initial planning

Driven by application size. Unit rates: discovery **~$0.15–0.25/screen**; initial planning
**~$0.10–0.20/test case** (cached).

| Application profile | Screens | Test cases | One-time onboarding |
|---|---|---|---|
| Compact (e.g. a kiosk POS) | ~20 | ~50 | ~$5–10 |
| Medium business app | ~60 | ~100 | ~$18–35 |
| **Large, complex app** | ~150–250 | ~150–250 | **~$50–110** |
| Very large enterprise app | ~400+ | ~300+ | ~$110–200 |

> A large, complex application is discovered and planned for **~$50–110** as a one-time cost — not the
> few-dollar figure that applies only to a small demo app. Discovery repeats only on a major UI change.

### 3.2 Recurring AI usage — validation & self-healing

Steady state runs from cached plans with local matching and **no AI calls**. Claude is used only for
unfamiliar screens, occasional validation, and one diagnosis per failing test. Indicative monthly AI
usage: **~$25–40/app** (typical), trending to **~$40–80/app** for large complex apps with high test
volume. Run frequency has little effect.

## 4. Infrastructure — Scenario A: single-tenant VM

One customer on one Compute Engine VM (app + isolated browser + PostgreSQL + object store + cache on
one box). Always-on 24×7 (≈730 h/month).

| Google Cloud service | Configuration | Monthly (USD) |
|---|---|---|
| Compute Engine VM | e2-standard-4 (4 vCPU, 16 GB), 730 h | ~$98 |
| Persistent Disk | Balanced, 50 GB | ~$5 |
| External static IP | 1 address, in use, 730 h | ~$4 |
| Network egress | Light — screenshots to model API + console | ~$2–5 |
| Artifact Registry | Container image storage | ~$1 |
| Data stores | PostgreSQL, object storage, cache co-hosted on VM | $0 |
| **Infrastructure subtotal** | | **~$110** |

One VM hosts all applications of a single customer; only AI usage grows with the number of apps.

## 5. Infrastructure — Scenario B: multi-tenant Kubernetes

Many customers on shared GKE infrastructure with managed data services. Estimate = 50 apps
(10 customers × 5), always-on. Customers isolated logically (every record/object/channel stamped with
the owner) — no per-customer infrastructure to duplicate.

| Google Cloud service | Configuration | Monthly (USD) |
|---|---|---|
| GKE cluster management | 1 cluster, $0.10/h | ~$73 |
| Node pool (compute) | 3× e2-standard-4 baseline; auto-scales at peak | ~$294 (peak ~$490–590) |
| Cloud SQL (PostgreSQL) | 2 vCPU / 8 GB, 50 GB SSD, zonal | ~$115 (HA ~$230) |
| Memorystore (Redis) | Basic tier, 1–2 GB (bus + job queue) | ~$36–70 (HA ~2×) |
| Cloud Storage | Screenshots/artifacts/plans + operations | ~$5–10 |
| External HTTP(S) Load Balancer | 1 forwarding rule + data processing | ~$18–25 |
| Cloud Operations | Logging & monitoring beyond free tier | ~$10–20 |
| Egress + Artifact Registry | Internet egress + image storage | ~$10 |
| **Infrastructure subtotal (baseline)** | | **~$560–620** |

> Multi-tenant is materially cheaper than one VM per customer. Ten single-tenant VMs ≈ **$1,100/mo
> compute floor alone** (ten mostly-idle machines) with no central management; one shared cluster serves
> all 50 apps for **~$560–620/mo** because the control plane, LB, DB and worker pool are shared and scale
> to demand. (Google Cloud credits one zonal cluster's fee per billing account; regional is charged. HA
> options roughly double the Cloud SQL + Redis lines — optional for a pilot.)

## 6. Indicative monthly cost

Indicative planning estimates (Google Cloud list prices, us-central1). AI usage assumes ~$25–40/app/mo
(higher for very large apps, lower for compact ones).

| Deployment | Apps | Infra/mo | AI usage/mo | Total/mo | One-time onboarding |
|---|---|---|---|---|---|
| Single VM — 1 compact app | 1 | ~$110 | ~$25–40 | **~$135–150** | ~$5–10 |
| Single VM — 1 large, complex app | 1 | ~$110 | ~$40–80 | **~$150–190** | ~$50–110 |
| Single VM — 1 customer, 10 apps | 10 | ~$110 | ~$250–400 | **~$360–510** | ~$50–300 |
| GKE multi-tenant — 10 customers × 5 | 50 | ~$560–620 | ~$1,250–2,000 | **~$1,810–2,620** | ~$250–1,500 |

At scale the per-app cost falls and holds steady — roughly **$36–52/app/month** for 50 apps — because
shared infrastructure, caching and reusable discovery memory make cost per app sub-linear.

## 7. Pricing levers

- **Application size** — more screens → more one-time discovery cost.
- **Test volume** — more tests → more one-time planning cost (not per-run, thanks to caching).
- **Validation depth** — richer visual validation adds small per-run AI cost; most validation is local/free.
- **Run frequency** — little effect on AI cost in steady state; mainly influences compute hours.
- **Infrastructure levers** — committed-use discounts (1-yr ~25–37%, 3-yr ~46–55%) on VM/nodes/Cloud SQL;
  Spot nodes for retriable test execution (~60–91% off); GKE Autopilot (no idle-node floor); off-hours VM shutdown.

## 8. Optional — self-hosted GPU models for Auto-Repair (Llama 4 + GraphRAG)

For strict data-sovereignty / air-gap, Auto-Repair can run a self-hosted open model (e.g. **Llama 4**)
on a GPU machine in the customer's own project, so no source code leaves the environment. Context
retrieval can use **Microsoft GraphRAG** (a codebase/design-doc knowledge graph for higher-precision,
multi-hop retrieval on complex defects).

**What it changes:**
- **Compute model** — the per-repair Claude call (~$0.05–0.10 each) is replaced by inference on an
  always-on GPU machine — a fixed monthly cost regardless of repair volume.
- **GraphRAG** — adds a one-time + incremental graph-indexing pass over code/docs; indexing runs on the
  same GPU (no external token cost) but consumes GPU hours; re-index incrementally on code changes.
- **Shared** — one GPU machine serves Auto-Repair for all apps/customers (a shared platform service).

**Indicative GPU machine cost (Google Cloud, 24×7):**

| GPU machine | Suitable for | On-demand/mo | 1-yr commit | Spot |
|---|---|---|---|---|
| 1× L4 (24 GB) | Small/quantized models only (marginal for Llama 4) | ~$650 | ~$410 | ~$200–260 |
| 1× A100 (80 GB) | Llama 4 Scout (quantized), lower throughput | ~$3,700 | ~$2,300 | ~$1,100–1,500 |
| 1× H100 (80 GB) | Llama 4 Scout (recommended) | ~$8,000 | ~$5,000 | ~$2,500–3,200 |
| 8× H100 (640 GB) | Llama 4 Maverick (full, largest models) | ~$64,000 | ~$40,000 | ~$20,000+ |

> **This is a data-sovereignty / control choice, not a cost saving.** At typical repair volumes the
> managed Claude path costs a few dollars a month; an always-on GPU machine starts ~$650/mo (L4) and is
> ~$2,500–5,000/mo for a Llama 4-class model on an H100. Choose it when code must never leave the
> environment, when repair volume is very high, or when heavy GraphRAG indexing is required.

Extending self-hosted models to the visual discovery stage is possible (Llama 4 is multimodal) but the
visual-understanding stage is the most demanding and is validated per customer with an accuracy benchmark.

## 9. OpEx vs. CapEx

No traditional hardware CapEx (pure cloud). (Many finance teams book all cloud as OpEx; the CapEx column
captures one-time/committed items commonly capitalised.)

| Category | Nature | Items |
|---|---|---|
| **OpEx** (recurring, usage-based) | Monthly; scales with usage, floors when idle | Compute (VM/GKE nodes, any GPU machine), disk, Cloud SQL, Memorystore, Cloud Storage, LB, egress, Cloud Operations, support, **AI usage (Claude)** |
| **CapEx / one-time / committed** | One-off or committed; capital-like | One-time onboarding & implementation; committed-use commitments (1–3 yr); optional GPU reservation for the self-hosted-model option |

**Scenario A — single tenant, one large complex app (per month):**

| Item | OpEx/mo | CapEx (one-time) |
|---|---|---|
| VM + disk + IP | ~$107 | — |
| Egress + Artifact Registry | ~$3–6 | — |
| AI usage (Claude) | ~$40–80 | — |
| One-time onboarding | — | ~$50–110 |
| Committed-use discount (optional) | reduces infra ~25–55% | commitment |
| **Total** | **~$150–190/mo** | **~$50–110 one-time** |

**Scenario B — multi-tenant, 50 apps / 10 customers (per month):**

| Item | OpEx/mo | CapEx (one-time) |
|---|---|---|
| GKE management + node pool | ~$367 | — |
| Cloud SQL + Memorystore + Cloud Storage | ~$156–195 | — |
| LB + Cloud Operations + egress | ~$38–55 | — |
| AI usage (Claude, 50 apps) | ~$1,250–2,000 | — |
| One-time onboarding (50 apps) | — | ~$250–1,500 |
| Committed-use discount (optional) | reduces infra ~25–55% | commitment |
| **Total** | **~$1,810–2,620/mo** | **~$250–1,500 one-time** |

**Customer reading:** a predictable monthly operating cost (compute + managed services + AI usage), a
one-time onboarding per app that scales with app size, and an optional multi-year commitment trading a
capital-like commitment for 25–55% lower operating cost.

## 10. Notes & assumptions

- Indicative Google Cloud / Vertex AI list prices (us-central1, Sep 2026); validate against live pricing and Region.
- Compute/DB/cache/networking assumed always-on 24×7 (≈730 h/month).
- Workload basis: ~20 screens / ~50 tests for a compact app; larger profiles per §3.1. Business-day runs; standard validation depth.
- Region, HA choices, peak concurrency, data volumes and support tier move the figures; ranges bracket common cases.
- New Google Cloud accounts get a one-time credit (typically $300) + always-free allowances — offsets early pilots (not included).
