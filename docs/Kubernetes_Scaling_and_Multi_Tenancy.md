# Auto-Scaling & Multi-Tenancy on Kubernetes

*How the Kiosk-QA Platform isolates customers and scales on Google Kubernetes Engine (GKE)*

> Companion to `Kubernetes_Scaling_and_Multi_Tenancy.docx` (customer-facing) and the diagram
> `gke_topologies_diagram.png`. Version 1.0 — Haibotix Solutions Engineering.

## 1. Purpose

Is the platform future-ready for Kubernetes auto-scaling and multi-tenancy with isolation between
customers and their apps — and how much is our responsibility vs. the cloud provider's (e.g. GKE)?

Short answer: **shared responsibility, and both halves are already built** into the cloud-agnostic
edition. GKE supplies the scaling and network-isolation machinery; the application supplies the
statelessness and the customer-data isolation that the platform — not the cloud — must enforce.
No rewrite is required; everything below is present and config-gated today.

## 2. Two independent axes of separation

| Axis | Separates | Mechanism | When needed |
|---|---|---|---|
| **`app_id`** | Different apps *within* one customer | App knowledge-graph + every test plan scoped per `app_id` | Always — even one customer with many apps |
| **`tenant_id`** | One customer org from another | Every DB row, storage object and live channel stamped + filtered by `tenant_id` | Only when customers share infrastructure (pooled SaaS) |

- **Example 1** (1 customer, 10 apps) → only `app_id` separation → runs single-tenant, identical to today.
- **Example 2** (10 customers, 5 apps each) → additionally enables `tenant_id` isolation.

## 3. Division of responsibility — GKE vs. the platform

| Concern | GKE handles | Platform must provide (status) |
|---|---|---|
| Pod auto-scaling | Runs the HPA; schedules pods | HPA manifest + stateless pods — **DONE** |
| Node auto-scaling | Cluster Autoscaler adds/removes VMs | Correct CPU/mem requests — present; tune per load |
| Independent tier scaling | Scales each Deployment separately | Separate API/worker Deployments + queue — **DONE** |
| Real-time across replicas | Load-balances connections | Cross-replica fan-out (Redis pub/sub WS) — **DONE**; no sticky sessions |
| Network isolation | NetworkPolicy, per-namespace RBAC/quota | Choose topology (pooled vs. namespace-per-customer) — **DONE** |
| **Customer-data isolation** | **Nothing — GKE never sees your rows** | `tenant_id` on every row + enforced filter + tenant-prefixed blobs + tenant-scoped channels — **DONE** |

**Decisive line:** GKE cannot isolate customer data for you. Two customers sharing one database are
isolated only because the application stamps and filters `tenant_id` on every read and write.

## 4. What the platform already provides

**Built for horizontal scale:** stateless services (all durable state in Postgres/object-store/Redis);
separate API + worker tiers connected by a job queue (browser-heavy execution scales independently);
cross-replica realtime via a shared bus (no sticky sessions).

**Built for tenant isolation (config-gated, off by default):** row-level (`tenant_id` column + central
filter/stamp so no query can leak); blob-level (per-tenant object prefix); realtime + memory
namespaced per tenant; identity resolved per request from a signed token claim (header fallback).

Single-tenant runs are byte-identical to the original local product — no regression.

## 5. Two deployment topologies

See `gke_topologies_diagram.png`.

- **Pooled multi-tenant SaaS** — all customers in one GKE cluster in our account; isolation is
  *logical* (`tenant_id`). Cheapest to operate; best cost-per-app at scale (shared control plane, LB
  and managed datastores amortised across tenants).
- **Dedicated / self-hosted** — own namespace, or own cluster in the customer's own cloud account;
  isolation is the *account/namespace boundary* (NetworkPolicy + ResourceQuota). Same image + manifests
  run unchanged in the customer's account, so their data never leaves it. `tenant_id` then separates
  their internal sub-teams.

## 6. The two worked examples

- **One customer, ten apps** — no multi-tenancy. Apps separated by `app_id`; concurrent testing is
  absorbed by the worker HPA + queue. Nothing new required.
- **Ten customers, five apps each** — enable tenant isolation. Pooled: 50 apps share one cluster,
  kept apart by `tenant_id`. Dedicated: each customer runs the identical image in its own
  namespace/account. Either way the *platform* guarantees no cross-customer reads.

## 7. How auto-scaling behaves

- **HPA** adds/removes API + worker pods on load (CPU or queue depth) — bursts absorbed, released after.
- **Cluster Autoscaler** adds/removes VM nodes under the pods — grows only while work is queued.
- **Cost shape:** steady-state execution is cache-served with no AI calls, so scaling affects compute
  hours far more than AI cost.

## 8. Honest gaps & hardening roadmap

Read/write isolation is complete. Before the *pooled* mode is fully production-grade (single-tenant is
fully covered):

| Item | Status / plan |
|---|---|
| Per-tenant resource quotas (fairness) | Dedicated model: GKE ResourceQuota covers it. Pooled: add per-tenant concurrency/queue-depth cap. |
| Composite unique keys | Enforced on Postgres (pooled target); SQLite doesn't enforce cross-tenant key reuse. |
| Parallel-orchestrator at scale | Supervisor rebinds tenant per worker; exercise under multi-tenant load. |
| Node right-sizing | Set requests/limits per profile so Cluster Autoscaler bin-packs efficiently. |

## 9. Summary

Future-ready and cloud-compatible. Stateless pods, externalised state, HPA + node autoscaling,
independent API/worker tiers, cross-replica realtime, and code-enforced tenant isolation are all in
place. GKE supplies the scaling/network machinery; the platform supplies statelessness + data
isolation. Example 1 works today with no flags; Example 2 works by enabling multi-tenancy plus the
small hardening list above (chiefly per-tenant quotas for the pooled model).
