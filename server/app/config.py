import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{DATA_DIR / 'marsad.db'}"
# Render/Neon give postgres:// ; SQLAlchemy wants postgresql+psycopg://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please-very-secret")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
TIMEZONE = os.getenv("TIMEZONE", "Africa/Tripoli")
SYNC_MINUTES = int(os.getenv("SYNC_MINUTES", "30"))
CRON_KEY = os.getenv("CRON_KEY", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # e.g. https://marsad.onrender.com
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "1") == "1"
APP_NAME = "مرصد الأعمال"
AGENT_IDLE_POLL = int(os.getenv("AGENT_IDLE_POLL", "120"))  # seconds between agent check-ins when nobody is using the UI
