from vision_agent.config import settings

# Object stores that speak the S3 API. AWS S3, MinIO, GCS (interop) and Azure (via an S3
# gateway) all use the same S3Storage class; the endpoint decides which one is hit.
_S3_COMPATIBLE = {"s3", "minio", "gcs", "azure"}


class _TenantScopedStorage:
    """Wraps a storage backend so WRITES land under the active tenant's namespace.

    Used only in multi-tenant mode. save()/save_json() keys are prefixed via ports.tenancy.scoped
    (→ tenants/<id>/<key>), isolating each tenant's blobs. load() is intentionally NOT scoped: it
    is called with explicit, already-resolved paths (e.g. a captured screenshot path), not with
    saved-artifact keys, so scoping it would break reads. In single-tenant mode this wrapper is
    never applied (get_storage returns the raw backend), keeping the MVP byte-identical.
    """

    def __init__(self, inner):
        self.inner = inner

    def load(self, source: str) -> bytes:
        return self.inner.load(source)

    def save(self, data: bytes, key: str) -> str:
        from ports.tenancy import scoped
        return self.inner.save(data, scoped(key))

    def save_json(self, data: dict, key: str) -> str:
        from ports.tenancy import scoped
        return self.inner.save_json(data, scoped(key))


def get_storage():
    """Return the configured object-store backend. Swap STORAGE_BACKEND in .env — no code change.

    "local" → filesystem (dev / single box). Any of s3|minio|gcs|azure → the S3-compatible
    backend, pointed at s3_endpoint_url (blank = real AWS). Fully cloud-agnostic. In multi-tenant
    mode the backend is wrapped so writes are isolated per tenant.
    """
    if settings.storage_backend in _S3_COMPATIBLE:
        from vision_agent.storage.aws import S3Storage
        backend = S3Storage(
            settings.s3_bucket,
            settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url or None,
            region=settings.s3_region or None,
            access_key=settings.s3_access_key_id or None,
            secret_key=settings.s3_secret_access_key or None,
            path_style=settings.s3_use_path_style,
        )
    else:
        from vision_agent.storage.local import LocalStorage
        backend = LocalStorage(settings.screenshots_dir)

    if settings.multi_tenant_enabled:
        return _TenantScopedStorage(backend)
    return backend
