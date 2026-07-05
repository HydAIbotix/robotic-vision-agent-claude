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
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()


def _run_lightweight_migrations():
    """Add columns introduced after a table was first created — SQLite's create_all does
    not alter existing tables. Idempotent: only adds a column when it is missing."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    wanted = {
        "device_configs": {"kiosk_id": "VARCHAR(50)"},  # abbreviation → Kiosk-ID link (multi-device)
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
