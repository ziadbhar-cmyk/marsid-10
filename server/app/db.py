import json
import secrets
from datetime import datetime

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True,
                       pool_recycle=280)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def now():
    return datetime.utcnow()


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    activity = Column(String(40), default="other")
    system_name = Column(String(200), default="")
    owner = Column(String(200), default="")
    city = Column(String(120), default="")
    branches = Column(String(400), default="")
    currency = Column(String(10), default="د.ل")
    notes = Column(Text, default="")
    color = Column(String(20), default="#0F6E56")
    connector = Column(String(20), default="")          # odoo | agent | excel
    conn_json = Column(Text, default="{}")              # connection settings
    agent_token = Column(String(80), default=lambda: secrets.token_urlsafe(32))
    agent_last_seen = Column(DateTime, nullable=True)
    agent_info = Column(Text, default="{}")
    schema_json = Column(Text, default="")             # tables discovered by agent
    status = Column(String(20), default="new")         # new | connected | error
    last_sync_at = Column(DateTime, nullable=True)
    last_error = Column(Text, default="")
    created_at = Column(DateTime, default=now)

    mappings = relationship("Mapping", cascade="all, delete-orphan", back_populates="project")

    @property
    def conn(self):
        try:
            return json.loads(self.conn_json or "{}")
        except Exception:
            return {}

    @property
    def info(self):
        try:
            return json.loads(self.agent_info or "{}")
        except Exception:
            return {}


class Mapping(Base):
    """One dataset pulled from one project (SQL for agents, builtin for Odoo)."""
    __tablename__ = "mappings"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset = Column(String(30), nullable=False)
    sql = Column(Text, default="")
    mode = Column(String(20), default="incremental")   # incremental | full
    watermark_field = Column(String(60), default="_wm")
    watermark_value = Column(String(60), default="")
    enabled = Column(Boolean, default=True)
    last_rows = Column(Integer, default=0)
    last_run = Column(DateTime, nullable=True)
    last_error = Column(Text, default="")
    project = relationship("Project", back_populates="mappings")


class Fact(Base):
    __tablename__ = "facts"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, index=True, nullable=False)
    dataset = Column(String(30), nullable=False)
    uid = Column(String(64), nullable=False)
    date = Column(Date, nullable=True)
    doc = Column(String(120), default="")
    branch = Column(String(200), default="")
    channel = Column(String(120), default="")
    party = Column(String(250), default="")
    item = Column(String(300), default="")
    qty = Column(Float, default=0)
    amount = Column(Float, default=0)
    amount2 = Column(Float, nullable=True)
    synced_at = Column(DateTime, default=now)

    __table_args__ = (
        Index("ix_fact_uid", "project_id", "dataset", "uid"),
        Index("ix_fact_date", "project_id", "dataset", "date"),
    )


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, index=True)
    kind = Column(String(30))           # discover | test_query | sync_now
    payload = Column(Text, default="{}")
    status = Column(String(20), default="pending")   # pending | sent | done | error
    result = Column(Text, default="")
    created_at = Column(DateTime, default=now)
    finished_at = Column(DateTime, nullable=True)


class SyncLog(Base):
    __tablename__ = "sync_logs"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, index=True)
    at = Column(DateTime, default=now)
    dataset = Column(String(30), default="")
    rows = Column(Integer, default=0)
    ok = Column(Boolean, default=True)
    message = Column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(Text, default="")


def init_db():
    Base.metadata.create_all(engine)


def get_setting(db, key, default=""):
    s = db.get(Setting, key)
    return s.value if s else default


def set_setting(db, key, value):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def log(db, project_id, dataset, rows, ok=True, message=""):
    db.add(SyncLog(project_id=project_id, dataset=dataset, rows=rows, ok=ok, message=message[:2000]))
    db.commit()
