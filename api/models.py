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
