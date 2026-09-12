"""
Durable object-store archiving (branch: cloud-agnostic-agent).

Playwright and OpenCV need screenshots + app_map as LOCAL files, so the app's WORKING store is the
local filesystem (fast, tool-compatible, byte-identical to the MVP). This module MIRRORS the
produced artifacts to the S3-compatible object store (MinIO / S3 / GCS / Azure) as the DURABLE
record — the cloud-agnostic equivalent of writing to S3, and how AgentCore uses ephemeral local
disk + S3 on AWS.

It is deliberately:
  - config-gated   : no-op unless settings.archive_to_object_store is set (+ an S3 target configured)
  - boundary-called: after exploration / planning / a run — NEVER inside the per-step vision path
  - failure-isolated: never raises (archiving must not break an activity)

Object keys mirror the working layout (app_map.json, screenshots/…, test_plans/…, results/<run>/…)
so the bucket matches what you see on the host — easy to show in the MinIO console during a demo.
"""
from __future__ import annotations

import os
from pathlib import Path

from vision_agent.config import settings

_S3_COMPATIBLE = {"s3", "minio", "gcs", "azure"}


def enabled() -> bool:
    """Archiving is on only when explicitly enabled AND an S3-compatible target is configured."""
    if not getattr(settings, "archive_to_object_store", False):
        return False
    return (
        settings.storage_backend in _S3_COMPATIBLE
        or bool(settings.s3_endpoint_url)
        or bool(settings.s3_bucket)
    )


def _client():
    from vision_agent.storage.aws import S3Storage
    return S3Storage(
        settings.s3_bucket,
        settings.s3_prefix,
        endpoint_url=settings.s3_endpoint_url or None,
        region=settings.s3_region or None,
        access_key=settings.s3_access_key_id or None,
        secret_key=settings.s3_secret_access_key or None,
        path_style=settings.s3_use_path_style,
    )


def _key_for(path: Path) -> str:
    """Clean S3 key that mirrors the working path (strip the container/data roots)."""
    s = str(path).replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    for pre in ("app/data/", "app/", "data/", "./"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    return s


def archive(local_path: str | os.PathLike, *, key: str | None = None) -> int:
    """Mirror a local file or directory tree to the object store.

    Returns the number of files uploaded (0 if disabled, missing, or on any error). Never raises —
    a failed archive prints a note and leaves the activity untouched.
    """
    if not enabled():
        return 0
    try:
        p = Path(local_path)
        if not p.exists():
            return 0
        st = _client()
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        n = 0
        for f in files:
            k = key if (key and p.is_file()) else _key_for(f)
            st.save(f.read_bytes(), k)
            n += 1
        if n:
            print(f"  [ARCHIVE] {n} file(s) -> object store  ({_key_for(p)})")
        return n
    except Exception as e:  # never break an activity because archiving failed
        print(f"  [ARCHIVE] skipped {local_path}: {e}")
        return 0


def delete_prefix(*prefixes: str) -> int:
    """Delete every object under the given key prefix(es) from the object store.

    Used by /api/reset so a cloud Reset also clears the ARCHIVED copies of whatever it removes
    locally (plans, run results, run screenshots). Pass ONLY the reset-scoped prefixes so exploration
    artifacts (app_map.json, screenshots/exploration_*) are preserved. No-op unless archiving is
    enabled; never raises. Single-tenant key layout (the default); multi-tenant would prefix the
    keys with tenants/<id>/ — a follow-up if pooled multi-tenant archiving is enabled.
    """
    if not enabled():
        return 0
    try:
        st = _client()
        s3, bucket, pfx = st._s3, st._bucket, st._prefix
        deleted = 0
        for prefix in prefixes:
            key_prefix = f"{pfx}/{prefix}" if pfx else prefix
            token = None
            while True:
                kw = {"Bucket": bucket, "Prefix": key_prefix}
                if token:
                    kw["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kw)
                objs = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
                if objs:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                    deleted += len(objs)
                if resp.get("IsTruncated"):
                    token = resp.get("NextContinuationToken")
                else:
                    break
        if deleted:
            print(f"  [ARCHIVE] deleted {deleted} object(s) from the object store ({', '.join(prefixes)})")
        return deleted
    except Exception as e:
        print(f"  [ARCHIVE] delete skipped: {e}")
        return 0
