"""SQLAlchemy models — all persistent data for the management system."""
import json
from datetime import datetime
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from api.database import Base


class KioskConfig(Base):
    __tablename__ = "kiosk_configs"
    id           = Column(Integer, primary_key=True)
    kiosk_id     = Column(String(50), unique=True, nullable=False)
    name         = Column(String(100))
    url          = Column(String(500))
    robot_id     = Column(String(50))
    screen_w_m   = Column(Float, default=0.4)
    screen_h_m   = Column(Float, default=0.3)
    tag_id       = Column(Integer)
    position_x   = Column(Float, default=0.0)
    position_y   = Column(Float, default=0.0)
    position_th  = Column(Float, default=0.0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppMapRecord(Base):
    __tablename__ = "app_maps"
    id           = Column(Integer, primary_key=True)
    kiosk_id     = Column(String(50), ForeignKey("kiosk_configs.kiosk_id"))
    explored_at  = Column(DateTime)
    screen_count = Column(Integer, default=0)
    element_count= Column(Integer, default=0)
    data         = Column(JSON)
    created_at   = Column(DateTime, default=datetime.utcnow)


class TestCase(Base):
    __tablename__ = "test_cases"
    id                  = Column(Integer, primary_key=True)
    kiosk_id            = Column(String(50))
    test_id             = Column(String(50), unique=True, nullable=False)
    summary             = Column(Text)
    description         = Column(Text)
    preconditions       = Column(Text)
    steps_raw           = Column(Text)
    expected_results_raw= Column(Text)
    tags                = Column(String(500))
    priority            = Column(String(10), default="P3")
    created_at          = Column(DateTime, default=datetime.utcnow)


class TestRun(Base):
    __tablename__ = "test_runs"
    id           = Column(Integer, primary_key=True)
    run_id       = Column(String(50), unique=True, nullable=False)
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
    results      = relationship("TestResult", back_populates="run", cascade="all, delete-orphan")


class TestResult(Base):
    __tablename__ = "test_results"
    id            = Column(Integer, primary_key=True)
    run_id        = Column(String(50), ForeignKey("test_runs.run_id"))
    test_id       = Column(String(50))
    summary       = Column(Text)
    outcome       = Column(String(20))
    step_results  = Column(JSON)
    vision_summary= Column(Text)
    started_at    = Column(DateTime)
    completed_at  = Column(DateTime)
    run           = relationship("TestRun", back_populates="results")


class Defect(Base):
    __tablename__ = "defects"
    id                 = Column(Integer, primary_key=True)
    run_id             = Column(String(50), ForeignKey("test_runs.run_id"))
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


class DeviceConfig(Base):
    """One entry per physical device the robot visits (TVM, MPOS, RSV, etc.)."""
    __tablename__ = "device_configs"
    id          = Column(Integer, primary_key=True)
    alias         = Column(String(50), unique=True, nullable=False)  # e.g. "TVM"
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


class RobotEvent(Base):
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
