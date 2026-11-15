"""MarsadAgent — وكيل مزامنة مرصد الأعمال

يشتغل على الجهاز الذي فيه قاعدة بيانات المنظومة المحلية (أبو غريس، العلامة، منظومة المصنع...).
- يقرأ فقط (يرفض أي أمر غير SELECT).
- يتصل هو بالسيرفر عبر HTTPS؛ لا يفتح أي منفذ على الجهاز.
- يأخذ استعلامات الربط من السيرفر، وينفذ طلبات التجربة واكتشاف الجداول عن بُعد.
- إذا انقطع الإنترنت يحفظ البيانات في مجلد outbox ويرسلها لاحقاً.

التشغيل:
  python agent.py --test        تجربة الاتصال بالقاعدة وبالسيرفر
  python agent.py               تشغيل مستمر
  python agent.py --run-for 290 تشغيل لمدة محددة بالثواني (يستخدمه جدول مهام ويندوز كل 5 دقائق)
"""
import argparse
import datetime as dt
import decimal
import json
import logging
import logging.handlers
import os
import platform
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0.0"
HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
OUTBOX = HERE / "outbox"
CHUNK = 2000
POLL_SECONDS = 20
LOCK_PORT = 47653

log = logging.getLogger("marsad")


def setup_logging(verbose=False):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(HERE / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------- values

def clean(v):
    if v is None:
        return None
    if isinstance(v, (dt.datetime,)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return None if len(b) > 64 else b.hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, str):
        return v.strip()
    return v


# ----------------------------------------------------------------- database

class DB:
    def __init__(self, cfg):
        self.cfg = cfg
        self.driver = cfg.get("driver", "sqlserver").lower()
        self.conn = None

    def connect(self):
        if self.conn:
            return self.conn
        c, d = self.cfg, self.driver
        if d in ("sqlserver", "access", "odbc"):
            import pyodbc
            self.conn = pyodbc.connect(c["connection_string"], timeout=30, autocommit=True)
        elif d == "mysql":
            import pymysql
            self.conn = pymysql.connect(host=c.get("host", "127.0.0.1"), port=int(c.get("port", 3306)),
                                        user=c.get("user"), password=c.get("password"),
                                        database=c.get("database"), charset=c.get("charset", "utf8mb4"),
                                        connect_timeout=30, autocommit=True)
        elif d == "postgres":
            import pg8000.dbapi
            self.conn = pg8000.dbapi.connect(host=c.get("host", "127.0.0.1"), port=int(c.get("port", 5432)),
                                             user=c.get("user"), password=c.get("password"),
                                             database=c.get("database"))
            self.conn.autocommit = True
        elif d == "firebird":
            import fdb
            self.conn = fdb.connect(dsn=c["dsn"], user=c.get("user", "SYSDBA"), password=c.get("password", ""),
                                    charset=c.get("charset", "UTF8"))
        elif d == "sqlite":
            import sqlite3
            self.conn = sqlite3.connect(f"file:{c['path']}?mode=ro", uri=True, check_same_thread=False)
        else:
            raise ValueError(f"نوع قاعدة غير معروف: {d}")
        return self.conn

    def close(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def query(self, sql, limit=None):
        guard(sql)
        cur = self.connect().cursor()
        try:
            cur.execute(sql)
            cols = [c[0] for c in cur.description] if cur.description else []
            if limit:
                rows = cur.fetchmany(limit)
                yield cols, [[clean(v) for v in r] for r in rows]
                return
            while True:
                rows = cur.fetchmany(CHUNK)
                if not rows:
                    break
                yield cols, [[clean(v) for v in r] for r in rows]
        finally:
            try:
                cur.close()
            except Exception:
                pass
            if self.driver == "firebird":
                try:
                    self.conn.commit()
                except Exception:
                    pass

    def quote(self, name):
        if self.driver in ("sqlserver", "access", "odbc"):
            return ".".join(f"[{p}]" for p in name.split("."))
        if self.driver == "mysql":
            return ".".join(f"`{p}`" for p in name.split("."))
        return ".".join(f'"{p}"' for p in name.split("."))

    def sample_sql(self, table, n):
        q = self.quote(table)
        if self.driver in ("sqlserver", "access", "odbc"):
            return f"SELECT TOP {int(n)} * FROM {q}"
        if self.driver == "firebird":
            return f"SELECT FIRST {int(n)} * FROM {q}"
        return f"SELECT * FROM {q} LIMIT {int(n)}"

    def literal(self, v):
        """Watermark value as SQL literal."""
        if v in (None, ""):
            v = "1900-01-01 00:00:00"
        s = str(v)
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return s
        s = s.replace("'", "''")
        if self.driver == "access" and re.match(r"\d{4}-\d{2}-\d{2}", s):
            return f"#{s}#"
        return f"'{s}'"

    # ---- schema discovery
    def discover(self, sample_rows=3, max_tables=400):
        conn = self.connect()
        d = self.driver
        tables = {}
        if d == "sqlserver":
            sql = """SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
                     FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t
                       ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
                     WHERE t.TABLE_TYPE IN ('BASE TABLE','VIEW') ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION"""
            for _c, rows in self.query(sql):
                for sch, tn, cn, ty in rows:
                    name = tn if sch == "dbo" else f"{sch}.{tn}"
                    tables.setdefault(name, []).append({"name": cn, "type": ty})
            counts = {}
            try:
                for _c, rows in self.query("""SELECT s.name, t.name, SUM(p.rows) FROM sys.tables t
                        JOIN sys.schemas s ON s.schema_id=t.schema_id JOIN sys.partitions p ON p.object_id=t.object_id
                        AND p.index_id IN (0,1) GROUP BY s.name, t.name"""):
                    for sch, tn, n in rows:
                        counts[tn if sch == "dbo" else f"{sch}.{tn}"] = int(n or 0)
            except Exception:
                pass
        elif d in ("mysql", "postgres"):
            where = "c.table_schema = DATABASE()" if d == "mysql" else \
                "c.table_schema NOT IN ('pg_catalog','information_schema')"
            sql = f"""SELECT c.table_schema, c.table_name, c.column_name, c.data_type FROM information_schema.columns c
                      WHERE {where} ORDER BY c.table_schema, c.table_name, c.ordinal_position"""
            for _c, rows in self.query(sql):
                for sch, tn, cn, ty in rows:
                    name = tn if d == "mysql" or sch == "public" else f"{sch}.{tn}"
                    tables.setdefault(name, []).append({"name": cn, "type": ty})
            counts = {}
            if d == "mysql":
                try:
                    for _c, rows in self.query("SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema = DATABASE()"):
                        counts = {tn: int(n or 0) for tn, n in rows}
                except Exception:
                    pass
        elif d == "firebird":
            sql = """SELECT TRIM(r.RDB$RELATION_NAME), TRIM(f.RDB$FIELD_NAME), f.RDB$FIELD_POSITION
                     FROM RDB$RELATION_FIELDS f JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME=f.RDB$RELATION_NAME
                     WHERE COALESCE(r.RDB$SYSTEM_FLAG,0)=0 ORDER BY 1,3"""
            for _c, rows in self.query(sql):
                for tn, cn, _pos in rows:
                    tables.setdefault(tn, []).append({"name": cn, "type": ""})
            counts = {}
        elif d == "sqlite":
            names = [r[0] for _c, rows in self.query("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'") for r in rows]
            cur = conn.cursor()
            counts = {}
            for tn in names:
                cur.execute(f'PRAGMA table_info("{tn}")')
                tables[tn] = [{"name": r[1], "type": r[2]} for r in cur.fetchall()]
                try:
                    cur.execute(f'SELECT COUNT(*) FROM "{tn}"')
                    counts[tn] = cur.fetchone()[0]
                except Exception:
                    pass
        else:  # access / generic odbc
            cur = conn.cursor()
            names = [r.table_name for r in cur.tables() if (r.table_type or "").upper() in ("TABLE", "VIEW")]
            counts = {}
            for tn in names:
                try:
                    tables[tn] = [{"name": c.column_name, "type": c.type_name} for c in cur.columns(table=tn)]
                except Exception:
                    tables[tn] = []
        out = []
        for i, (name, cols) in enumerate(sorted(tables.items(), key=lambda kv: -counts.get(kv[0], 0))):
            t = {"name": name, "columns": cols, "rows": counts.get(name)}
            if sample_rows and i < max_tables:
                try:
                    for c, rows in self.query(self.sample_sql(name, sample_rows), limit=sample_rows):
                        t["sample"] = [dict(zip(c, [str(v)[:80] if v is not None else None for v in r])) for r in rows]
                except Exception as e:
                    t["sample_error"] = str(e)[:200]
            out.append(t)
        return out


def guard(sql):
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S).strip().rstrip(";").strip()
    first = s.split(None, 1)[0].lower() if s else ""
    if first not in ("select", "with"):
        raise PermissionError("مسموح فقط باستعلامات SELECT للقراءة")
    banned = r"\b(insert|update|delete|drop|alter|truncate|create|exec|execute|merge|grant|revoke)\b"
    # allow the words inside string literals
    no_str = re.sub(r"'([^']|'')*'", "''", s)
    if ";" in no_str or re.search(banned, no_str, re.I):
        raise PermissionError("الاستعلام يحتوي على أوامر غير مسموحة (مسموح القراءة فقط)")


# ----------------------------------------------------------------- server API

class Server:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def post(self, path, data, timeout=120):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Agent-Token": self.token,
                                              "User-Agent": f"MarsadAgent/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {e.code}: {msg}") from None


# ----------------------------------------------------------------- agent

class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.server = Server(cfg["server_url"], cfg["token"])
        self.db = DB(cfg["database"])
        self.state = load_json(STATE_PATH, {"last_sync": 0})
        self.remote = {}
        OUTBOX.mkdir(exist_ok=True)

    def hello(self):
        db_ok, db_err = True, ""
        try:
            self.db.connect()
        except Exception as e:
            db_ok, db_err = False, str(e)[:300]
            self.db.close()
        info = {"hostname": socket.gethostname(), "os": platform.platform(), "version": VERSION,
                "driver": self.db.driver, "db_ok": db_ok, "db_error": db_err,
                "outbox": len(list(OUTBOX.glob("*.json")))}
        self.remote = self.server.post("/api/agent/hello", info, timeout=60)
        return db_ok, db_err

    # remote tasks from the web UI
    def run_task(self, t):
        kind, payload = t["kind"], t.get("payload") or {}
        log.info("task %s %s", t["id"], kind)
        try:
            if kind == "discover":
                self.send_schema()
                data = {"ok": True}
            elif kind == "test_query":
                limit = min(int(payload.get("limit", 20)), 200)
                sql = payload.get("sql") or self.db.sample_sql(payload["table"], limit)
                sql = self.fill(sql, "")
                cols, rows = [], []
                for cols, rows in self.db.query(sql, limit=limit):
                    pass
                data = {"columns": cols, "rows": rows}
            elif kind in ("sync_now", "full_resync"):
                self.sync(only=payload.get("mapping_id"), full=(kind == "full_resync"))
                data = {"ok": True}
            else:
                raise ValueError("unknown task")
            self.server.post("/api/agent/task_result", {"id": t["id"], "ok": True, "data": data})
        except Exception as e:
            log.warning("task failed: %s", e)
            self.db.close() if not isinstance(e, PermissionError) else None
            self.server.post("/api/agent/task_result", {"id": t["id"], "ok": False, "error": str(e)[:1500]})

    def fill(self, sql, wm):
        """{since} -> date/text literal (default 1900-01-01); {since_num} -> number (default 0)."""
        num = wm if wm not in (None, "") and re.fullmatch(r"-?\d+(\.\d+)?", str(wm)) else "0"
        return sql.replace("{since_num}", num).replace("{since}", self.db.literal(wm))

    def send_schema(self):
        log.info("discovering tables...")
        tables = self.db.discover(sample_rows=int(self.cfg.get("sample_rows", 3)))
        self.server.post("/api/agent/schema", {"tables": tables}, timeout=300)
        log.info("sent %d tables", len(tables))

    # push (with offline outbox)
    def push(self, payload):
        try:
            self.flush_outbox()
            self.server.post("/api/agent/push", payload, timeout=300)
            return True
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            name = OUTBOX / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_{payload['mapping_id']}.json"
            save_json(name, payload)
            log.warning("server unreachable (%s) — saved to outbox %s", e, name.name)
            return False

    def flush_outbox(self):
        for f in sorted(OUTBOX.glob("*.json")):
            payload = load_json(f, None)
            if payload:
                self.server.post("/api/agent/push", payload, timeout=300)
            f.unlink()
            log.info("outbox sent %s", f.name)

    def sync(self, only=None, full=False):
        for m in self.remote.get("mappings", []):
            if only and int(only) != m["id"]:
                continue
            ds, mode = m["dataset"], m["mode"]
            wm = "" if full else m.get("watermark_value", "")
            sql = self.fill(m["sql"], wm)
            replace = mode == "full" or full
            t0 = time.time()
            total, first, buf_cols = 0, True, None
            try:
                chunks = list(self._chunks(sql))
                if not chunks:
                    chunks = [([], [])]
                for i, (cols, rows) in enumerate(chunks):
                    dict_rows = [dict(zip([c.lower() for c in cols], r)) for r in rows]
                    total += len(dict_rows)
                    last = i == len(chunks) - 1
                    self.push({"mapping_id": m["id"], "rows": dict_rows, "replace": replace and first,
                               "last_chunk": last, "total_rows": total})
                    first = False
                log.info("%s: %d rows in %.1fs", ds, total, time.time() - t0)
            except Exception as e:
                log.error("%s failed: %s", ds, e)
                self.db.close()
                try:
                    self.server.post("/api/agent/error", {"mapping_id": m["id"], "dataset": ds, "error": str(e)[:1500]})
                except Exception:
                    pass
        self.state["last_sync"] = time.time()
        save_json(STATE_PATH, self.state)

    def _chunks(self, sql):
        # read everything first, then push: a slow network never holds the DB cursor open
        for cols, rows in self.db.query(sql):
            yield cols, rows

    def cycle(self):
        db_ok, err = self.hello()
        if not db_ok:
            log.error("database connection failed: %s", err)
        if db_ok and self.remote.get("need_schema"):
            try:
                self.send_schema()
            except Exception as e:
                log.error("schema failed: %s", e)
                self.db.close()
        for t in self.remote.get("tasks", []):
            self.run_task(t)
        interval = int(self.remote.get("interval_minutes", 30)) * 60
        if db_ok and time.time() - self.state.get("last_sync", 0) >= interval:
            self.sync()
        elif db_ok:
            try:
                self.flush_outbox()
            except Exception:
                pass

    def run(self, run_for=None):
        end = time.time() + run_for if run_for else None
        while True:
            try:
                self.cycle()
            except Exception as e:
                log.error("cycle error: %s", e)
                self.db.close()
            wait = int(self.remote.get("poll_seconds", POLL_SECONDS))
            if end and time.time() + wait > end:
                break
            time.sleep(wait)
        self.db.close()


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--run-for", type=int, default=0)
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    setup_logging(a.v)
    cfg = load_json(a.config, None)
    if not cfg:
        print("config.json غير موجود")
        sys.exit(2)
    agent = Agent(cfg)
    if a.test:
        print(f"MarsadAgent {VERSION}")
        try:
            agent.db.connect()
            print("✔ الاتصال بقاعدة البيانات ناجح")
        except Exception as e:
            print("✘ فشل الاتصال بقاعدة البيانات:", e)
        try:
            ok, err = agent.hello()
            print("✔ الاتصال بالسيرفر ناجح — المشروع:", agent.remote.get("project"))
        except Exception as e:
            print("✘ فشل الاتصال بالسيرفر:", e)
        return
    lock = single_instance()
    if not lock:
        log.info("agent already running")
        return
    log.info("MarsadAgent %s started (driver=%s)", VERSION, agent.db.driver)
    agent.run(a.run_for or None)


if __name__ == "__main__":
    main()
