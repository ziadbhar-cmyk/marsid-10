import threading
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import TIMEZONE
from .connectors import odoo
from .db import Project, SessionLocal, get_setting, log, set_setting
from .notify import build_summary, send_telegram

_locks = {}
_global = threading.Lock()


def _lock(pid):
    with _global:
        return _locks.setdefault(pid, threading.Lock())


def sync_one(pid, full=False):
    lk = _lock(pid)
    if not lk.acquire(blocking=False):
        return "running"
    db = SessionLocal()
    try:
        p = db.get(Project, pid)
        if not p or p.connector != "odoo":
            return "skip"
        total, errors = odoo.sync_project(db, p, full=full)
        return f"ok {total}" + (f" errors {len(errors)}" if errors else "")
    except Exception as e:
        try:
            p = db.get(Project, pid)
            p.status, p.last_error = "error", str(e)[:2000]
            db.commit()
            log(db, pid, "", 0, False, str(e))
        except Exception:
            traceback.print_exc()
        return f"error {e}"
    finally:
        db.close()
        lk.release()


def sync_one_bg(pid, full=False):
    threading.Thread(target=sync_one, args=(pid, full), daemon=True).start()


def sync_all_pull():
    db = SessionLocal()
    try:
        ids = [p.id for p in db.query(Project).filter(Project.connector == "odoo").all()]
    finally:
        db.close()
    return {pid: sync_one(pid) for pid in ids}


def daily_report_if_due():
    """Called every few minutes; sends the Telegram summary once a day at the set hour."""
    db = SessionLocal()
    try:
        hour = int(get_setting(db, "report_hour", "23") or 23)
        now = datetime.now(ZoneInfo(TIMEZONE))
        key = now.date().isoformat()
        if now.hour >= hour and get_setting(db, "last_report_day") != key:
            ok, _ = send_telegram(db, build_summary(db))
            if ok:
                set_setting(db, "last_report_day", key)
    finally:
        db.close()


def tick():
    res = sync_all_pull()
    daily_report_if_due()
    return res
