"""
Tests for the cloud-agnostic seams (branch: cloud-agnostic-agent).

Two goals:
  1. NO REGRESSION — with default config, every seam resolves to the exact MVP behaviour
     (local filesystem storage, in-memory bus, single 'default' tenant, sqlite).
  2. The adapters switch purely by config, with no code change at the call site.
"""
import importlib

from vision_agent.config import settings


# ── Defaults reproduce the pure-local MVP ──────────────────────────────────────
def test_defaults_are_local_mvp():
    assert settings.storage_backend == "local"
    assert settings.persistence_backend == "sqlite"
    assert settings.event_bus_backend == "memory"
    assert settings.multi_tenant_enabled is False
    assert settings.default_tenant_id == "default"
    # Claude stays the model regardless of cloud.
    assert settings.vision_backend in ("anthropic", "bedrock")


def test_get_storage_defaults_to_local():
    from vision_agent.storage import get_storage
    from vision_agent.storage.local import LocalStorage

    assert isinstance(get_storage(), LocalStorage)


# ── Tenancy ────────────────────────────────────────────────────────────────────
def test_tenancy_single_tenant_default():
    from ports import tenancy

    # single-tenant mode → always the default tenant, header ignored
    assert tenancy.current_tenant() == "default"
    assert tenancy.tenant_key("app_map.json") == "tenants/default/app_map.json"
    assert tenancy.tenant_dependency("acme") == "default"  # ignored when multi_tenant off


def test_tenancy_multi_tenant(monkeypatch):
    from ports import tenancy

    monkeypatch.setattr(settings, "multi_tenant_enabled", True)
    token = tenancy.set_current_tenant("acme")
    try:
        assert tenancy.current_tenant() == "acme"
        assert tenancy.tenant_key("results/run-1.json") == "tenants/acme/results/run-1.json"
        # explicit override wins
        assert tenancy.tenant_key("x", tenant_id="globex") == "tenants/globex/x"
    finally:
        tenancy.reset_tenant(token)


# ── Event bus ────────────────────────────────────────────────────────────────────
def test_in_memory_event_bus_publish_subscribe():
    from ports import event_bus

    event_bus.reset_event_bus()
    bus = event_bus.get_event_bus()
    assert isinstance(bus, event_bus.InMemoryEventBus)

    received = []
    unsub = bus.subscribe("run:1", received.append)
    bus.publish("run:1", {"type": "step_result", "n": 1})
    bus.publish("run:2", {"type": "other"})       # different channel — not delivered
    assert received == [{"type": "step_result", "n": 1}]

    unsub()
    bus.publish("run:1", {"type": "after_unsub"})  # no longer delivered
    assert len(received) == 1


# ── Storage-key / path scoping (single-tenant == MVP; multi-tenant isolates) ─────
def test_scoped_is_identity_when_single_tenant():
    from ports import tenancy

    assert settings.multi_tenant_enabled is False
    assert tenancy.scoped("results/run-1.json") == "results/run-1.json"          # unchanged
    assert str(tenancy.scoped_dir("results")) == "results"                        # unchanged


def test_scoped_prefixes_when_multi_tenant(monkeypatch):
    from ports import tenancy

    monkeypatch.setattr(settings, "multi_tenant_enabled", True)
    token = tenancy.set_current_tenant("acme")
    try:
        assert tenancy.scoped("results/run-1.json") == "tenants/acme/results/run-1.json"
        assert str(tenancy.scoped_dir("results")).replace("\\", "/") == "results/tenants/acme"
    finally:
        tenancy.reset_tenant(token)


def test_blob_paths_single_tenant_match_mvp():
    from ports import paths
    from vision_agent.config import settings as s

    # Single-tenant → the exact MVP-derived paths (no regression to existing local layouts).
    assert paths.app_map_path() == str(__import__("pathlib").Path(s.app_map_path))
    assert str(paths.results_dir()) == str(__import__("pathlib").Path(s.results_dir))


def test_blob_paths_multi_tenant_isolated(monkeypatch, tmp_path):
    from pathlib import Path
    from ports import paths, tenancy

    # Point roots at a tmp dir so the accessor's mkdir doesn't create junk in the repo.
    monkeypatch.setattr(settings, "app_map_path", str(tmp_path / "app_map.json"))
    monkeypatch.setattr(settings, "results_dir", str(tmp_path / "results"))
    monkeypatch.setattr(settings, "multi_tenant_enabled", True)
    token = tenancy.set_current_tenant("bankx")
    try:
        amp = Path(paths.app_map_path()).as_posix()
        assert "tenants/bankx/" in amp and amp.endswith(Path(settings.app_map_path).name)
        # app_map + its screenshots stay co-located under the tenant dir
        assert paths.screens_dir().as_posix().endswith("tenants/bankx/screenshots")
        assert paths.results_dir().as_posix().endswith("tenants/bankx")
    finally:
        tenancy.reset_tenant(token)


def test_get_storage_wraps_only_in_multi_tenant(monkeypatch):
    from vision_agent.storage import get_storage, _TenantScopedStorage
    from vision_agent.storage.local import LocalStorage

    assert isinstance(get_storage(), LocalStorage)                 # single-tenant → raw backend
    monkeypatch.setattr(settings, "multi_tenant_enabled", True)
    try:
        wrapped = get_storage()
        assert isinstance(wrapped, _TenantScopedStorage)
        assert isinstance(wrapped.inner, LocalStorage)
    finally:
        monkeypatch.setattr(settings, "multi_tenant_enabled", False)


# ── Row-level tenant isolation (DB) ──────────────────────────────────────────────
def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from api.database import Base, _register_tenant_scope
    from api import models  # noqa: F401 – ensure mapped + TenantMixin present

    _register_tenant_scope()  # idempotent; events are global + flag-gated
    eng = create_engine(f"sqlite:///{tmp_path / 'iso.db'}")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng), models


def test_db_rows_isolated_in_multi_tenant(monkeypatch, tmp_path):
    from ports import tenancy

    SL, models = _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "multi_tenant_enabled", True)

    for tid, rid in [("acme", "r-acme"), ("globex", "r-globex"), ("acme", "r-acme2")]:
        tok = tenancy.set_current_tenant(tid)
        s = SL(); s.add(models.TestRun(run_id=rid, status="done")); s.commit(); s.close()
        tenancy.reset_tenant(tok)

    tok = tenancy.set_current_tenant("acme")
    s = SL()
    assert sorted(r.run_id for r in s.query(models.TestRun).all()) == ["r-acme", "r-acme2"]
    assert s.query(models.TestRun).first().tenant_id == "acme"   # auto-stamped
    s.close(); tenancy.reset_tenant(tok)

    tok = tenancy.set_current_tenant("globex")
    s = SL()
    assert [r.run_id for r in s.query(models.TestRun).all()] == ["r-globex"]
    s.close(); tenancy.reset_tenant(tok)


def test_db_rows_not_filtered_in_single_tenant(tmp_path):
    # Flag off (default) → no filter, no stamp: behaves exactly like the MVP (all rows visible).
    SL, models = _fresh_db(tmp_path)
    assert settings.multi_tenant_enabled is False
    s = SL()
    s.add(models.TestRun(run_id="a", status="done"))
    s.add(models.TestRun(run_id="b", status="done"))
    s.commit()
    assert sorted(r.run_id for r in s.query(models.TestRun).all()) == ["a", "b"]
    assert s.query(models.TestRun).first().tenant_id == "default"   # server_default
    s.close()


def test_composite_unique_allows_cross_tenant_id_reuse(monkeypatch, tmp_path):
    from ports import tenancy

    SL, models = _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "multi_tenant_enabled", True)

    # Same run_id under two tenants must BOTH succeed (per-tenant uniqueness).
    for tid in ("acme", "globex"):
        tok = tenancy.set_current_tenant(tid)
        s = SL(); s.add(models.TestRun(run_id="run-1", status="done")); s.commit(); s.close()
        tenancy.reset_tenant(tok)

    # But a duplicate within ONE tenant must fail.
    tok = tenancy.set_current_tenant("acme")
    s = SL(); s.add(models.TestRun(run_id="run-1", status="dup"))
    raised = False
    try:
        s.commit()
    except Exception:
        raised = True
        s.rollback()
    s.close(); tenancy.reset_tenant(tok)
    assert raised, "same-tenant duplicate run_id must be rejected"


# ── JWT-claim tenant resolution ──────────────────────────────────────────────────
def test_tenant_resolution_header_default():
    from ports import tenancy
    # JWT disabled (default) → header is the source
    assert tenancy.resolve_tenant_from_headers({"x-tenant-id": "acme"}) == "acme"
    assert tenancy.resolve_tenant_from_headers({}) is None


def test_tenant_resolution_from_jwt(monkeypatch):
    import pytest
    jwt = pytest.importorskip("jwt")  # PyJWT is in the [cloud] extra
    from ports import tenancy

    monkeypatch.setattr(settings, "tenant_jwt_enabled", True)
    monkeypatch.setattr(settings, "tenant_jwt_secret", "s3cr3t")
    monkeypatch.setattr(settings, "tenant_jwt_claim", "tenant_id")

    good = jwt.encode({"tenant_id": "acme"}, "s3cr3t", algorithm="HS256")
    assert tenancy.resolve_tenant_from_headers({"authorization": f"Bearer {good}"}) == "acme"

    # A token signed with the wrong secret is rejected → header fallback (not a 500).
    bad = jwt.encode({"tenant_id": "evil"}, "wrong", algorithm="HS256")
    assert tenancy.resolve_tenant_from_headers(
        {"authorization": f"Bearer {bad}", "x-tenant-id": "hdr"}) == "hdr"


# ── Redis-ready WebSocket broadcast (publishes to the event bus) ─────────────────
def test_broadcast_publishes_to_event_bus():
    from ports import event_bus
    import api.main as m

    event_bus.reset_event_bus()
    bus = event_bus.get_event_bus()
    got = []
    unsub = bus.subscribe("ws:run:default:run-x", got.append)   # single-tenant channel
    m._broadcast("run-x", {"event": "hi"})
    unsub()
    assert got == [{"event": "hi"}]


def test_broadcast_channel_is_tenant_namespaced(monkeypatch):
    from ports import event_bus, tenancy
    import api.main as m

    monkeypatch.setattr(settings, "multi_tenant_enabled", True)
    event_bus.reset_event_bus()
    bus = event_bus.get_event_bus()
    got = []
    tok = tenancy.set_current_tenant("acme")
    unsub = bus.subscribe("ws:run:acme:r1", got.append)
    m._broadcast("r1", {"e": 1})
    # a different tenant's subscriber must NOT receive it
    other = []
    unsub_other = bus.subscribe("ws:run:globex:r1", other.append)
    m._broadcast("r1", {"e": 2})
    unsub(); unsub_other(); tenancy.reset_tenant(tok)
    assert got == [{"e": 1}, {"e": 2}] and other == []


# ── Task queue / orchestration / tracing / memory (all default to MVP behaviour) ──
def test_task_queue_inline_runs_handler_in_thread():
    import threading
    from ports import queue

    assert settings.task_queue_backend == "inline"
    done = threading.Event()
    seen = {}

    def handler(payload):
        seen["payload"] = payload
        done.set()

    queue.enqueue_run({"run_id": "r1"}, handler)     # inline → same-process daemon thread
    assert done.wait(2.0) and seen["payload"] == {"run_id": "r1"}


def test_orchestration_inprocess_delegates_to_queue(monkeypatch):
    from ports import orchestration

    assert settings.orchestrator_backend == "inprocess"
    captured = {}
    monkeypatch.setattr("ports.queue.enqueue_run",
                        lambda payload, handler: captured.setdefault("payload", payload))
    orchestration.submit_run({"run_id": "r2"}, lambda p: None)
    assert captured["payload"] == {"run_id": "r2"}


def test_tracing_default_off():
    from ports import tracing
    assert settings.tracing_backend == "none"
    assert tracing.llm_callbacks() == []          # nothing attached to LLM invokes
    with tracing.span("noop"):                     # context manager is a safe no-op
        pass


def test_memory_default_off():
    from ports import memory
    assert settings.memory_backend == "none"
    assert memory.enabled() is False
    assert memory.search("anything") == []
    memory.remember("anything")                    # no-op, must not raise


def test_event_bus_redis_requires_url(monkeypatch):
    from ports import event_bus

    monkeypatch.setattr(settings, "event_bus_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "")
    event_bus.reset_event_bus()
    try:
        raised = False
        try:
            event_bus.get_event_bus()
        except RuntimeError:
            raised = True
        assert raised, "redis backend with empty REDIS_URL must fail fast"
    finally:
        event_bus.reset_event_bus()
