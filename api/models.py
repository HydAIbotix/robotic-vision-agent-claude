"""SQLAlchemy models — all persistent data for the management system."""
import json
from datetime import datetime
from sqlalchemy import (Column, DateTime, Float, Integer, String, Text, JSON,
                        ForeignKey, ForeignKeyConstraint, UniqueConstraint)
from sqlalchemy.orm import relationship, declared_attr
from api.database import Base


class TenantMixin:
    """Row-level multi-tenancy. Every business table carries a tenant_id so a POOLED deployment
    isolates each customer's rows. Reads are auto-filtered and inserts auto-stamped by the session
    events in api/database.py — but ONLY when MULTI_TENANT_ENABLED is on, so a single-tenant
    deployment behaves exactly like the MVP (every row is "default", nothing is filtered).

    declared_attr gives each mapped class its own Column + index. server_default fills existing rows
    on migration and any insert that bypasses the ORM default. Natural keys (kiosk_id, test_id,
    run_id, alias) are unique PER TENANT via composite UniqueConstraints below, and the FKs that
    reference them are composite (tenant_id, <key>) so two tenants can reuse the same id. On a fresh
    DB (SQLite or Postgres) create_all applies these; single-tenant behaves like a global unique
    (tenant_id is constant). Migrating an EXISTING pooled Postgres DB to composite uniques is a
    documented manual step (see docs/CLOUD_AGNOSTIC_DEPLOY.md)."""

    @declared_attr
    def tenant_id(cls):
        return Column(String(64), nullable=False, default="default",
                      server_default="default", index=True)


class KioskConfig(TenantMixin, Base):
    __tablename__ = "kiosk_configs"
    __table_args__ = (UniqueConstraint("tenant_id", "kiosk_id", name="uq_kiosk_configs_tenant_kiosk"),)
    id           = Column(Integer, primary_key=True)
    kiosk_id     = Column(String(50), nullable=False)   # unique PER TENANT (see __table_args__)
    name         = Column(String(100))
    url          = Column(String(500))
    robot_id     = Column(String(50), default="R-01")   # default so /explore-created rows aren't NULL
    screen_w_m   = Column(Float, default=0.4)
    screen_h_m   = Column(Float, default=0.3)
    tag_id       = Column(Integer, default=1)            # default so /explore-created rows aren't NULL
    position_x   = Column(Float, default=0.0)
    position_y   = Column(Float, default=0.0)
    position_th  = Column(Float, default=0.0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppMapRecord(TenantMixin, Base):
    __tablename__ = "app_maps"
    # Composite FK so it references kiosk_configs' per-tenant unique key.
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "kiosk_id"],
                                           ["kiosk_configs.tenant_id", "kiosk_configs.kiosk_id"]),)
    id           = Column(Integer, primary_key=True)
    kiosk_id     = Column(String(50))
    explored_at  = Column(DateTime)
    screen_count = Column(Integer, default=0)
    element_count= Column(Integer, default=0)
    data         = Column(JSON)
    created_at   = Column(DateTime, default=datetime.utcnow)


class TestCase(TenantMixin, Base):
    __tablename__ = "test_cases"
    __table_args__ = (UniqueConstraint("tenant_id", "test_id", name="uq_test_cases_tenant_test"),)
    id                  = Column(Integer, primary_key=True)
    kiosk_id            = Column(String(50))
    test_id             = Column(String(50), nullable=False)   # unique PER TENANT
    summary             = Column(Text)
    description         = Column(Text)
    preconditions       = Column(Text)
    steps_raw           = Column(Text)
    expected_results_raw= Column(Text)
    tags                = Column(String(500))
    priority            = Column(String(10), default="P3")
    created_at          = Column(DateTime, default=datetime.utcnow)


class TestRun(TenantMixin, Base):
    __tablename__ = "test_runs"
    __table_args__ = (UniqueConstraint("tenant_id", "run_id", name="uq_test_runs_tenant_run"),)
    id           = Column(Integer, primary_key=True)
    run_id       = Column(String(50), nullable=False)   # unique PER TENANT
    kiosk_id     = Column(String(50))
    robot_id     = Column(String(50))
    excel_path   = Column(String(500))
    filter_tc    = Column(String(100))
    mode         = Column(String(20), default="playwright")  # playwright | real | demo
    status       = Column(String(20), default="pending")     # pending|running|completed|failed
    total        = Column(Integer, default=0)
    passed       = Column(Integer, default=0)
    failed       = Column(Integer, default=0)
    error        = Column(Text)
    started_at   = Column(DateTime)
    completed_at = Column(DateTime)
    created_at   = Column(DateTime, default=datetime.utcnow)
    results      = relationship(
        "TestResult", back_populates="run", cascade="all, delete-orphan",
        primaryjoin="and_(TestRun.tenant_id==TestResult.tenant_id, TestRun.run_id==TestResult.run_id)",
        foreign_keys="[TestResult.tenant_id, TestResult.run_id]",
    )


class TestResult(TenantMixin, Base):
    __tablename__ = "test_results"
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "run_id"],
                                           ["test_runs.tenant_id", "test_runs.run_id"]),)
    id            = Column(Integer, primary_key=True)
    run_id        = Column(String(50))
    test_id       = Column(String(50))
    summary       = Column(Text)
    outcome       = Column(String(20))
    step_results  = Column(JSON)
    vision_summary= Column(Text)
    started_at    = Column(DateTime)
    completed_at  = Column(DateTime)
    # Composite FK spans (tenant_id, run_id); give the relationship an explicit join so SQLAlchemy
    # never has to guess, and overlaps on the shared tenant_id column are declared harmless.
    run           = relationship(
        "TestRun", back_populates="results",
        primaryjoin="and_(TestResult.tenant_id==TestRun.tenant_id, TestResult.run_id==TestRun.run_id)",
        foreign_keys="[TestResult.tenant_id, TestResult.run_id]",
    )


class Defect(TenantMixin, Base):
    __tablename__ = "defects"
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "run_id"],
                                           ["test_runs.tenant_id", "test_runs.run_id"]),)
    id                 = Column(Integer, primary_key=True)
    run_id             = Column(String(50))
    test_id            = Column(String(50))
    title              = Column(Text)
    description        = Column(Text)
    steps_to_reproduce = Column(Text)
    root_cause         = Column(Text)
    probable_fix       = Column(Text)
    severity           = Column(String(20))   # critical | high | medium | low
    priority           = Column(String(10))   # P1 | P2 | P3 | P4
    jira_key           = Column(String(50))   # e.g. KIOSK-123
    jira_url           = Column(String(500))
    status             = Column(String(20), default="open")
    evidence_json      = Column(JSON)         # list of screenshot paths
    created_at         = Column(DateTime, default=datetime.utcnow)


class DeviceConfig(TenantMixin, Base):
    """One entry per physical device the robot visits (TVM, MPOS, RSV, etc.)."""
    __tablename__ = "device_configs"
    __table_args__ = (UniqueConstraint("tenant_id", "alias", name="uq_device_configs_tenant_alias"),)
    id          = Column(Integer, primary_key=True)
    alias         = Column(String(50), nullable=False)  # e.g. "TVM" — unique PER TENANT
    kiosk_id      = Column(String(50))                               # linked Kiosk-ID (e.g. "KIOSK-ID-1") — the JOIN KEY
    # AGV map position name sent as the /base/goto `target` when driving to this device (e.g.
    # "kiosk-2-Aug-14-G37"). DECOUPLED from kiosk_id so the robotics team can rename AGV map
    # positions without disturbing the join key. Blank → falls back to kiosk_id (historical behaviour).
    position_name = Column(String(80))
    description   = Column(String(200))                             # e.g. "Ticket Vending Machine"
    pos_x       = Column(Float, default=0.0)   # metres from robot home
    pos_y       = Column(Float, default=0.0)
    pos_theta   = Column(Float, default=0.0)   # heading in degrees
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RobotEvent(TenantMixin, Base):
    __tablename__ = "robot_events"
    id           = Column(Integer, primary_key=True)
    run_id       = Column(String(50))
    robot_id     = Column(String(50))
    cmd_id       = Column(String(50))
    event_type   = Column(String(50))
    endpoint     = Column(String(100))
    request_at   = Column(Float)
    response_at  = Column(Float)
    latency_ms   = Column(Float)
    http_status  = Column(Integer)
    created_at   = Column(DateTime, default=datetime.utcnow)
