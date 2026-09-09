"""
Tenancy port — clear isolation for single- and multi-tenant deployments, one codebase.

The same build serves both go-to-market models (see docs/CLOUD_AGNOSTIC_DECISION.md):
  - pooled SaaS  : many customers share one deployment, separated logically by tenant_id
  - dedicated    : one customer per deployment (in their own cloud); tenant_id then separates
                   that customer's own sub-teams / brands, or is simply the default (single)

current_tenant() returns the active tenant id. In single-tenant mode it is ALWAYS
default_tenant_id, so callers may prefix keys/rows unconditionally with zero behaviour change —
the MVP is exactly "single tenant == 'default'". tenant_key() namespaces an object-store or
cache key identically in both models, giving physical-looking key isolation for free.

Wiring (incremental, non-breaking):
  - object-store keys : wrap with tenant_key(key) at the storage seam
  - DB rows           : add a tenant_id column (default 'default') and filter by current_tenant()
  - API requests      : add `Depends(tenant_dependency)` so each request binds its tenant
"""
from __future__ import annotations

import contextvars

from vision_agent.config import settings

# contextvars is async- and thread-friendly: a value set at the start of a request is visible
# to everything run within that request's context, and never leaks across requests.
_tenant_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)


def current_tenant() -> str:
    """The active tenant id. Single-tenant mode → always default_tenant_id."""
    if not settings.multi_tenant_enabled:
        return settings.default_tenant_id
    return _tenant_var.get() or settings.default_tenant_id


def set_current_tenant(tenant_id: str | None) -> contextvars.Token:
    """Bind the active tenant for the current context. Returns a token for reset_tenant()."""
    return _tenant_var.set((tenant_id or settings.default_tenant_id).strip())


def reset_tenant(token: contextvars.Token) -> None:
    """Restore the tenant binding to what it was before the matching set_current_tenant()."""
    _tenant_var.reset(token)


def tenant_key(key: str, tenant_id: str | None = None) -> str:
    """Namespace an object-store / cache key by tenant → "tenants/<id>/<key>".

    ALWAYS prefixes (even single-tenant → tenants/default/), so call sites can be made
    tenant-aware once and behave correctly in both deployment models. For wrapping EXISTING
    call sites where single-tenant must stay byte-identical to the MVP, use scoped() instead.
    """
    tid = tenant_id or current_tenant()
    return f"tenants/{tid}/{key.lstrip('/')}"


def scoped(key: str, tenant_id: str | None = None) -> str:
    """Tenant-namespace an object-store key — but ONLY in multi-tenant mode.

    In single-tenant mode this is the identity function, so wrapping an existing storage key
    with scoped() leaves the pure-local MVP byte-identical (no path/key changes, no regression).
    In multi-tenant mode it prefixes "tenants/<id>/", isolating every tenant's blobs.
    """
    if not settings.multi_tenant_enabled:
        return key
    return tenant_key(key, tenant_id)


def scoped_dir(base, tenant_id: str | None = None):
    """Return a per-tenant sub-directory of `base` in multi-tenant mode; `base` unchanged otherwise.

    Used to isolate the tenant-owned filesystem roots (results, screenshots, test plans, app_map).
    Single-tenant → returns `base` as-is, so existing local layouts are untouched.
    """
    from pathlib import Path

    base = Path(base)
    if not settings.multi_tenant_enabled:
        return base
    tid = tenant_id or current_tenant()
    return base / "tenants" / tid


def _tenant_from_jwt(token: str) -> str | None:
    """Decode a JWT and return the configured tenant claim, or None on any problem.

    PyJWT is imported lazily so it is not a hard dependency until JWT resolution is enabled. Any
    decode/verify failure returns None (→ the caller falls back to the header) rather than raising,
    so a malformed token never 500s the request; a WRONG-but-valid token simply resolves its own
    tenant, which is the desired isolation behaviour.
    """
    try:
        import jwt  # PyJWT
    except ImportError:
        return None
    try:
        aud = settings.tenant_jwt_audience or None
        payload = jwt.decode(
            token,
            settings.tenant_jwt_secret,
            algorithms=[a.strip() for a in settings.tenant_jwt_algorithms.split(",") if a.strip()],
            audience=aud,
            options={"verify_aud": bool(aud)},
        )
        val = payload.get(settings.tenant_jwt_claim)
        return str(val).strip() if val else None
    except Exception:
        return None


def resolve_tenant_from_headers(headers: dict) -> str | None:
    """Resolve the request's tenant from headers (keys lower-cased): a signed JWT bearer token when
    tenant_jwt_enabled, else the X-Tenant-Id header. The header is also the fallback when JWT is on
    but no valid token is present. Returns None → the default tenant is used."""
    if settings.tenant_jwt_enabled:
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tid = _tenant_from_jwt(auth.split(" ", 1)[1].strip())
            if tid:
                return tid
    return headers.get("x-tenant-id")


def tenant_dependency(x_tenant_id: str | None = None) -> str:
    """FastAPI dependency: resolve the request's tenant and bind it for the call.

    Reads the X-Tenant-Id header (a JWT claim can be substituted later). In single-tenant mode
    the header is ignored and the default tenant is used, so existing clients keep working.
    Use as:  def route(..., tenant: str = Depends(tenant_dependency)): ...
    (Import Header in api/main.py and default the param to Header(None, alias="X-Tenant-Id").)
    """
    if not settings.multi_tenant_enabled:
        return settings.default_tenant_id
    set_current_tenant(x_tenant_id)
    return current_tenant()
