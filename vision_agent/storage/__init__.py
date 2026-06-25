from vision_agent.config import settings


def get_storage():
    """Return the configured storage backend. Swap STORAGE_BACKEND in .env — no code change."""
    if settings.storage_backend == "s3":
        from vision_agent.storage.aws import S3Storage
        return S3Storage(settings.s3_bucket, settings.s3_prefix)
    from vision_agent.storage.local import LocalStorage
    return LocalStorage(settings.screenshots_dir)
