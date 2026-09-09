"""
Tenant-aware filesystem roots for blob artifacts (branch: cloud-agnostic-agent).

The MVP writes app_map / results / screenshots / test-plans to fixed paths derived from
settings. In a POOLED multi-tenant deployment those artifacts must be isolated per tenant. These
accessors return:
  - single-tenant mode : the EXACT MVP path (byte-identical — no regression)
  - multi-tenant mode  : a per-tenant path under ".../tenants/<tenant_id>/..."

Call sites use these accessors instead of `settings.<x>` directly, so tenant isolation is a
config switch (MULTI_TENANT_ENABLED) with no behaviour change when it is off. The active tenant
comes from ports.tenancy.current_tenant() (bound per request; the explorer subprocess receives
the resolved path via the APP_MAP_PATH env override — see api.main._explore).
"""
from __future__ import annotations

from pathlib import Path

from vision_agent.config import settings
from ports.tenancy import current_tenant, scoped_dir


def app_map_path(tenant_id: str | None = None) -> str:
    """Path to this tenant's app_map JSON. Single-tenant → settings.app_map_path unchanged.

    In multi-tenant mode the parent dir is ensured (idempotent mkdir) so any writer — including
    the explorer subprocess that receives this via APP_MAP_PATH — can write without a pre-step.
    """
    raw = Path(settings.app_map_path)
    if not settings.multi_tenant_enabled:
        return str(raw)
    tid = tenant_id or current_tenant()
    tenant_file = raw.parent / "tenants" / tid / raw.name
    tenant_file.parent.mkdir(parents=True, exist_ok=True)
    return str(tenant_file)


def results_dir(tenant_id: str | None = None) -> Path:
    """Per-tenant results root (holds <run_id>/results.json + logs)."""
    return scoped_dir(settings.results_dir, tenant_id)


def screens_dir(tenant_id: str | None = None) -> Path:
    """Per-tenant screenshots root — the sibling of the app_map file, as in the MVP.

    Derived from app_map_path() so a tenant's map and its screenshots stay co-located
    (single-tenant → <app_map_dir>/screenshots; multi-tenant → <app_map_dir>/tenants/<id>/screenshots).
    """
    return Path(app_map_path(tenant_id)).parent / "screenshots"


def test_plans_dir(tenant_id: str | None = None) -> Path:
    """Per-tenant cached-plan root (<project_root>/test_plans in the MVP)."""
    base = Path(__file__).resolve().parent.parent / "test_plans"
    return scoped_dir(base, tenant_id)
