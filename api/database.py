"""SQLAlchemy database connection. Defaults to SQLite; set DB_URL env var for PostgreSQL."""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from vision_agent.config import settings

engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False} if settings.db_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from api import models  # noqa: F401  – ensure models are registered
    _log_active_engine()
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()
    _register_tenant_scope()


def _log_active_engine():
    """Surface which relational store is ACTUALLY in use and catch a config mismatch.

    persistence_backend is a readable label; db_url is what actually drives SQLAlchemy. If they
    disagree (e.g. PERSISTENCE_BACKEND=postgres but DB_URL still points at sqlite), the app would
    silently write to the wrong store — so we warn loudly instead of failing silently.
    """
    dialect = engine.url.get_backend_name()  # 'sqlite' | 'postgresql' | ...
    print(f"  [DB] persistence_backend={settings.persistence_backend}  engine={dialect}  url={engine.url.render_as_string(hide_password=True)}")
    declared = settings.persistence_backend
    is_pg = dialect.startswith("postgres")
    if declared == "postgres" and not is_pg:
        print("  [DB] WARNING: PERSISTENCE_BACKEND=postgres but DB_URL is not a PostgreSQL URL — "
              "set DB_URL=postgresql+psycopg://user:pass@host:5432/db")
    elif declared == "sqlite" and is_pg:
        print("  [DB] NOTE: DB_URL is PostgreSQL but PERSISTENCE_BACKEND=sqlite — update the label for clarity.")


def _run_lightweight_migrations():
    """Add columns introduced after a table was first created — SQLite's create_all does
    not alter existing tables. Idempotent: only adds a column when it is missing."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    wanted = {
        "device_configs": {
            "kiosk_id":      "VARCHAR(50)",   # abbreviation → Kiosk-ID link (multi-device)
            "position_name": "VARCHAR(80)",   # AGV map position name for /base/goto target (decoupled from kiosk_id)
        },
    }
    for table, cols in wanted.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for col, decl in cols.items():
            if col not in existing:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))
                    print(f"  [DB] migrated: added {table}.{col}")
                except Exception as exc:  # pragma: no cover
                    print(f"  [DB] migration warning ({table}.{col}): {exc}")

    # Row-level multi-tenancy: add tenant_id (default 'default') to every business table so an
    # existing single-tenant DB migrates cleanly (all current rows become the 'default' tenant).
    # server_default backfills existing rows; the index speeds the tenant filter. Idempotent.
    _tenant_tables = ["kiosk_configs", "app_maps", "test_cases", "test_runs",
                      "test_results", "defects", "device_configs", "robot_events"]
    for table in _tenant_tables:
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        if "tenant_id" in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'"))
                try:
                    conn.execute(text(
                        f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)"))
                except Exception:
                    pass
            print(f"  [DB] migrated: added {table}.tenant_id")
        except Exception as exc:  # pragma: no cover
            print(f"  [DB] migration warning ({table}.tenant_id): {exc}")


_tenant_events_registered = False


def _register_tenant_scope():
    """Register global session events that isolate rows by tenant — but ONLY when
    MULTI_TENANT_ENABLED is on. Registered once per process; the handlers themselves check the flag
    at query time, so single-tenant runs are unaffected (no filter, no stamp).

    Reads  (SELECT/UPDATE/DELETE): with_loader_criteria adds `WHERE tenant_id = current_tenant()`
           to every ORM statement touching a TenantMixin entity — leak-proof without editing the
           ~40 query sites, and it also guards ORM bulk update/delete.
    Writes (INSERT): before_flush stamps tenant_id = current_tenant() on new rows that don't set it.
    """
    global _tenant_events_registered
    if _tenant_events_registered:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session, with_loader_criteria
    from api.models import TenantMixin
    from ports.tenancy import current_tenant

    @event.listens_for(Session, "do_orm_execute")
    def _tenant_read_filter(state):
        if not settings.multi_tenant_enabled:
            return
        if not (state.is_select or state.is_update or state.is_delete):
            return
        # Resolve the tenant OUTSIDE the lambda and close over the literal value — the lambda SQL
        # system extracts it as a bound value and must not invoke functions itself.
        tid = current_tenant()
        state.statement = state.statement.options(
            with_loader_criteria(
                TenantMixin,
                lambda cls: cls.tenant_id == tid,
                include_aliases=True,
            )
        )

    @event.listens_for(Session, "before_flush")
    def _tenant_write_stamp(session, flush_context, instances):
        if not settings.multi_tenant_enabled:
            return
        tid = current_tenant()
        for obj in session.new:
            if isinstance(obj, TenantMixin) and not getattr(obj, "tenant_id", None):
                obj.tenant_id = tid

    _tenant_events_registered = True
