import csv
import io
import json
import os
import re
import secrets
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import config as C
from .connectors import odoo
from .datasets import ACTIVITIES, CONNECTORS, DATASETS, DB_ENGINES, SQL_TEMPLATES
from .db import (AgentTask, Fact, Mapping, Project, SessionLocal, SyncLog, get_setting, init_db,
                 log, now, set_setting)
from .excel_import import build_rows, guess_mapping, read_table
from .ingest import upsert
from .jobs import daily_report_if_due, sync_one_bg, tick
from .metrics import project_dashboard, overview, today
from .notify import build_summary, send_telegram

app = FastAPI(title=C.APP_NAME, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=C.SECRET_KEY, max_age=60 * 60 * 24 * 30,
                   https_only=False, same_site="lax")
app.mount("/static", StaticFiles(directory=C.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=C.BASE_DIR / "templates")

AGENT_DIR = Path(os.getenv("AGENT_DIR", C.BASE_DIR.parent.parent / "agent"))
PALETTE = ["#0F6E56", "#534AB7", "#993C1D", "#185FA5", "#993556", "#854F0B", "#3B6D11", "#5F5E5A"]


# ------------------------------------------------------------------ helpers

def money(v, digits=0):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}"
    except Exception:
        return str(v)


def local(dt):
    if not dt:
        return "—"
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(C.TIMEZONE)).strftime("%Y-%m-%d %H:%M")


def ago(dt):
    if not dt:
        return "لم يتم بعد"
    s = (datetime.utcnow() - dt).total_seconds()
    if s < 90:
        return "الآن"
    if s < 3600:
        return f"قبل {int(s // 60)} دقيقة"
    if s < 86400:
        return f"قبل {int(s // 3600)} ساعة"
    return f"قبل {int(s // 86400)} يوم"


templates.env.filters.update(money=money, local=local, ago=ago)
templates.env.globals.update(DS_ORDER={k: list(v["fields"]) for k, v in DATASETS.items()}, APP_NAME=C.APP_NAME, DATASETS=DATASETS, ACTIVITIES=ACTIVITIES,
                             CONNECTORS=CONNECTORS, DB_ENGINES=DB_ENGINES)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_redirect(request, exc):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


LAST_UI = {"t": 0.0}


def user(request: Request):
    if not request.session.get("user"):
        raise LoginRequired()
    LAST_UI["t"] = time.time()
    return request.session["user"]


def render(request, name, **ctx):
    db = ctx.pop("_db", None)
    if db is not None and "nav_projects" not in ctx:
        ctx["nav_projects"] = db.query(Project).order_by(Project.id).all()
    return templates.TemplateResponse(request, name, ctx)


def project_or_404(db, pid):
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "المشروع غير موجود")
    return p


def server_url(request):
    return (C.PUBLIC_URL or str(request.base_url)).rstrip("/")


def agent_online(p):
    return bool(p.agent_last_seen and (datetime.utcnow() - p.agent_last_seen) < timedelta(minutes=12))


templates.env.globals.update(agent_online=agent_online)


@app.on_event("startup")
def _startup():
    init_db()
    if C.ENABLE_SCHEDULER:
        from apscheduler.schedulers.background import BackgroundScheduler
        sch = BackgroundScheduler(timezone=C.TIMEZONE)
        sch.add_job(tick, "interval", minutes=C.SYNC_MINUTES, id="tick", max_instances=1, coalesce=True)
        sch.add_job(daily_report_if_due, "interval", minutes=10, id="report", max_instances=1)
        sch.start()


# ------------------------------------------------------------------ auth

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=next, error="")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if secrets.compare_digest(username, C.ADMIN_USER) and secrets.compare_digest(password, C.ADMIN_PASSWORD):
        request.session["user"] = username
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    return render(request, "login.html", next=next, error="اسم المستخدم أو كلمة المرور غير صحيحة")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cron/tick")
def cron_tick(key: str = ""):
    if not C.CRON_KEY or not secrets.compare_digest(key, C.CRON_KEY):
        raise HTTPException(403)
    return {"result": {str(k): v for k, v in tick().items()}}


# ------------------------------------------------------------------ khazina (treasury & manufacturing system)
# Standalone page with its own login (Firebase); served without Marsad login.

KHAZINA_FILE = C.BASE_DIR / "khazina" / "index.html"


@app.get("/khazina", response_class=HTMLResponse, include_in_schema=False)
@app.get("/khazina/", response_class=HTMLResponse, include_in_schema=False)
def khazina():
    return HTMLResponse(KHAZINA_FILE.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ home

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db=Depends(get_db), u=Depends(user)):
    projects = db.query(Project).order_by(Project.id).all()
    if not projects:
        return RedirectResponse("/projects/new", status_code=303)
    ov = overview(db, projects)
    logs = db.query(SyncLog).order_by(SyncLog.id.desc()).limit(12).all()
    names = {p.id: p.name for p in projects}
    return render(request, "home.html", _db=db, ov=ov, logs=logs, names=names)


# ------------------------------------------------------------------ project wizard

@app.get("/projects/new", response_class=HTMLResponse)
def new_project(request: Request, db=Depends(get_db), u=Depends(user)):
    return render(request, "project_form.html", _db=db, p=None, step=1)


def _project_from_form(p, form):
    for f in ("name", "activity", "system_name", "owner", "city", "branches", "currency", "notes", "color"):
        if f in form:
            setattr(p, f, (form.get(f) or "").strip())


@app.post("/projects/new")
async def create_project(request: Request, db=Depends(get_db), u=Depends(user)):
    form = await request.form()
    if not (form.get("name") or "").strip():
        return render(request, "project_form.html", _db=db, p=None, step=1, error="اكتب اسم المشروع")
    p = Project()
    _project_from_form(p, form)
    if not p.color:
        p.color = PALETTE[db.query(Project).count() % len(PALETTE)]
    db.add(p)
    db.commit()
    return RedirectResponse(f"/projects/{p.id}/connect", status_code=303)


@app.get("/projects/{pid}/edit", response_class=HTMLResponse)
def edit_project(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    return render(request, "project_form.html", _db=db, p=project_or_404(db, pid), step=1)


@app.post("/projects/{pid}/edit")
async def save_project(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    _project_from_form(p, await request.form())
    db.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/delete")
def delete_project(pid: int, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    db.query(Fact).filter(Fact.project_id == pid).delete()
    db.query(AgentTask).filter(AgentTask.project_id == pid).delete()
    db.query(SyncLog).filter(SyncLog.project_id == pid).delete()
    db.delete(p)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/projects/{pid}/connect", response_class=HTMLResponse)
def connect_page(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    return render(request, "connect.html", _db=db, p=p, step=2, c=p.conn)


@app.post("/projects/{pid}/connect/odoo/test")
def odoo_test(pid: int, url: str = Form(...), db_name: str = Form(...), username: str = Form(...),
              password: str = Form(...), u=Depends(user)):
    try:
        info = odoo.test_connection({"url": url.strip(), "db": db_name.strip(),
                                     "username": username.strip(), "password": password})
        return {"ok": True, **info}
    except Exception as e:
        msg = str(e)
        if "Name or service not known" in msg or "getaddrinfo" in msg:
            msg = "لا يمكن الوصول للرابط. تأكد من رابط أودو"
        return {"ok": False, "error": msg[:500]}


@app.post("/projects/{pid}/connect/odoo")
def odoo_save(pid: int, url: str = Form(...), db_name: str = Form(...), username: str = Form(...),
              password: str = Form(...), db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    p.connector = "odoo"
    p.conn_json = json.dumps({"url": url.strip().rstrip("/"), "db": db_name.strip(),
                              "username": username.strip(), "password": password})
    p.status = "connected"
    db.commit()
    odoo.ensure_mappings(db, p)
    sync_one_bg(pid, full=True)
    return RedirectResponse(f"/projects/{pid}?welcome=1", status_code=303)


@app.post("/projects/{pid}/connect/agent")
def agent_save(pid: int, engine: str = Form("sqlserver"), interval: int = Form(30),
               db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    p.connector = "agent"
    c = p.conn
    c.update({"engine": engine, "interval": max(5, int(interval))})
    p.conn_json = json.dumps(c)
    db.commit()
    return RedirectResponse(f"/projects/{pid}/agent", status_code=303)


@app.post("/projects/{pid}/connect/excel")
def excel_save(pid: int, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    p.connector = "excel"
    p.status = "connected"
    db.commit()
    return RedirectResponse(f"/projects/{pid}/import", status_code=303)


# ------------------------------------------------------------------ agent setup (web side)

@app.get("/projects/{pid}/agent", response_class=HTMLResponse)
def agent_page(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    tables = json.loads(p.schema_json) if p.schema_json else []
    return render(request, "agent.html", _db=db, p=p, step=2, server=server_url(request),
                  tables_count=len(tables))


@app.post("/projects/{pid}/agent/rotate")
def agent_rotate(pid: int, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    p.agent_token = secrets.token_urlsafe(32)
    db.commit()
    return RedirectResponse(f"/projects/{pid}/agent", status_code=303)


DRIVER_CONN = {
    "sqlserver": {"driver": "sqlserver",
                  "connection_string": "DRIVER={SQL Server};SERVER=.\\SQLEXPRESS;DATABASE=اسم_القاعدة;UID=marsad_read;PWD=كلمة_المرور"},
    "access": {"driver": "access",
               "connection_string": "DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=C:\\المسار\\data.mdb;"},
    "mysql": {"driver": "mysql", "host": "127.0.0.1", "port": 3306, "database": "", "user": "marsad_read", "password": ""},
    "postgres": {"driver": "postgres", "host": "127.0.0.1", "port": 5432, "database": "", "user": "marsad_read", "password": ""},
    "firebird": {"driver": "firebird", "dsn": "127.0.0.1:C:\\المسار\\DATA.FDB", "user": "SYSDBA", "password": "masterkey"},
    "sqlite": {"driver": "sqlite", "path": "C:\\المسار\\data.db"},
    "odbc": {"driver": "odbc", "connection_string": "DSN=اسم_المصدر;UID=;PWD="},
}


@app.get("/projects/{pid}/agent.zip")
def agent_zip(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    engine = p.conn.get("engine", "sqlserver")
    cfg = {"server_url": server_url(request), "token": p.agent_token, "project": p.name,
           "database": DRIVER_CONN.get(engine, DRIVER_CONN["sqlserver"])}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in AGENT_DIR.iterdir():
            if f.is_file() and f.name != "config.json" and not f.name.startswith("."):
                z.write(f, f"MarsadAgent/{f.name}")
        z.writestr("MarsadAgent/config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"marsad_agent_{pid}")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'})


@app.get("/projects/{pid}/agent/status")
def agent_status(pid: int, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    tables = json.loads(p.schema_json) if p.schema_json else []
    return {"online": agent_online(p), "last_seen": ago(p.agent_last_seen), "info": p.info,
            "tables": len(tables), "last_sync": ago(p.last_sync_at), "error": p.last_error}


@app.post("/projects/{pid}/agent/task")
async def agent_task_create(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    project_or_404(db, pid)
    body = await request.json()
    kind = body.get("kind")
    if kind not in ("discover", "test_query", "sync_now", "full_resync"):
        raise HTTPException(400)
    t = AgentTask(project_id=pid, kind=kind, payload=json.dumps(body.get("payload") or {}, ensure_ascii=False))
    db.add(t)
    db.commit()
    return {"id": t.id}


@app.get("/projects/{pid}/agent/task/{tid}")
def agent_task_get(pid: int, tid: int, db=Depends(get_db), u=Depends(user)):
    t = db.get(AgentTask, tid)
    if not t or t.project_id != pid:
        raise HTTPException(404)
    return {"status": t.status, "result": json.loads(t.result) if t.result else None}


@app.get("/projects/{pid}/explorer", response_class=HTMLResponse)
def explorer(pid: int, request: Request, q: str = "", db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    tables = json.loads(p.schema_json) if p.schema_json else []
    if q:
        ql = q.lower()
        tables = [t for t in tables if ql in t["name"].lower() or any(ql in c["name"].lower() for c in t.get("columns", []))]
    tables.sort(key=lambda t: -(t.get("rows") or 0))
    return render(request, "explorer.html", _db=db, p=p, tables=tables, q=q)


# ------------------------------------------------------------------ mappings (agent)

@app.get("/projects/{pid}/mappings", response_class=HTMLResponse)
def mappings_page(pid: int, request: Request, edit: int = 0, dataset: str = "", db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    cur = db.get(Mapping, edit) if edit else None
    ds = cur.dataset if cur else (dataset or "sales")
    sql = cur.sql if cur else SQL_TEMPLATES.get(ds, "")
    return render(request, "mappings.html", _db=db, p=p, cur=cur, ds=ds, sql=sql, templates_sql=SQL_TEMPLATES)


@app.post("/projects/{pid}/mappings")
def mappings_save(pid: int, dataset: str = Form(...), sql: str = Form(""), mode: str = Form("incremental"),
                  watermark_field: str = Form("_wm"), mid: int = Form(0), db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    if dataset not in DATASETS:
        raise HTTPException(400)
    if DATASETS[dataset]["kind"] == "snapshot":
        mode = "full"
    m = db.get(Mapping, mid) if mid else None
    if not m:
        m = Mapping(project_id=pid)
        db.add(m)
    if m.sql != sql or m.mode != mode:
        m.watermark_value = ""
    m.dataset, m.sql, m.mode, m.watermark_field = dataset, sql, mode, (watermark_field or "_wm").strip()
    m.enabled = True
    db.commit()
    db.add(AgentTask(project_id=pid, kind="sync_now", payload=json.dumps({"mapping_id": m.id})))
    db.commit()
    return RedirectResponse(f"/projects/{pid}/mappings", status_code=303)


@app.post("/projects/{pid}/mappings/{mid}/{action}")
def mapping_action(pid: int, mid: int, action: str, db=Depends(get_db), u=Depends(user)):
    m = db.get(Mapping, mid)
    if not m or m.project_id != pid:
        raise HTTPException(404)
    if action == "delete":
        db.delete(m)
    elif action == "toggle":
        m.enabled = not m.enabled
    elif action == "reset":
        m.watermark_value = ""
        db.query(Fact).filter(Fact.project_id == pid, Fact.dataset == m.dataset).delete()
    db.commit()
    back = f"/projects/{pid}/mappings" if m.project.connector == "agent" else f"/projects/{pid}"
    return RedirectResponse(back, status_code=303)


# ------------------------------------------------------------------ excel import

@app.get("/projects/{pid}/import", response_class=HTMLResponse)
def import_page(pid: int, request: Request, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    return render(request, "import.html", _db=db, p=p, step=2 if p.status == "new" else 0)


@app.post("/projects/{pid}/import/upload")
async def import_upload(pid: int, dataset: str = Form("sales"), file: UploadFile = File(...),
                        db=Depends(get_db), u=Depends(user)):
    project_or_404(db, pid)
    ext = Path(file.filename or "x.xlsx").suffix.lower()
    if ext not in (".xlsx", ".xlsm", ".csv", ".txt"):
        raise HTTPException(400, "الملف يجب أن يكون xlsx أو csv (احفظ ملفات xls القديمة بصيغة xlsx)")
    fid = f"{pid}_{uuid.uuid4().hex[:10]}{ext}"
    (C.UPLOAD_DIR / fid).write_bytes(await file.read())
    return RedirectResponse(f"/projects/{pid}/import/{fid}?dataset={dataset}", status_code=303)


@app.get("/projects/{pid}/import/{fid}", response_class=HTMLResponse)
def import_map(pid: int, fid: str, request: Request, dataset: str = "sales", sheet: str = "",
               db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    path = C.UPLOAD_DIR / Path(fid).name
    if not path.exists():
        raise HTTPException(404)
    headers, rows, sheets = read_table(path, sheet or None, max_rows=8)
    guess = guess_mapping(dataset, headers)
    return render(request, "import_map.html", _db=db, p=p, fid=fid, dataset=dataset, headers=headers,
                  rows=rows, sheets=sheets, sheet=sheet, guess=guess)


@app.post("/projects/{pid}/import/{fid}")
async def import_do(pid: int, fid: str, request: Request, db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    form = await request.form()
    dataset = form.get("dataset")
    sheet = form.get("sheet") or None
    path = C.UPLOAD_DIR / Path(fid).name
    headers, rows, _ = read_table(path, sheet)
    mapping, consts = {}, {}
    for field in DATASETS[dataset]["fields"]:
        v = form.get(f"col_{field}")
        if v not in (None, ""):
            mapping[field] = int(v)
        c = (form.get(f"const_{field}") or "").strip()
        if c:
            consts[field] = c
    data = build_rows(dataset, headers, rows, mapping, consts)
    prefix = (form.get("batch") or path.stem)
    for d in data:
        if not any(d.get(k) for k in DATASETS[dataset]["key"] if k != "line"):
            d["uid"] = f"{prefix}-{d['line']}"
    replace = form.get("replace") == "1" or DATASETS[dataset]["kind"] == "snapshot"
    n = upsert(db, pid, dataset, data, replace=replace)
    p.last_sync_at, p.status = now(), "connected"
    if p.connector != "agent" and p.connector != "odoo":
        p.connector = "excel"
    db.commit()
    log(db, pid, dataset, n, True, f"استيراد ملف {fid}")
    try:
        path.unlink()
    except Exception:
        pass
    return RedirectResponse(f"/projects/{pid}?imported={n}", status_code=303)


# ------------------------------------------------------------------ dashboards & data

def _d(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


@app.get("/projects/{pid}", response_class=HTMLResponse)
def dashboard(pid: int, request: Request, start: str = "", end: str = "", db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    if not p.connector:
        return RedirectResponse(f"/projects/{pid}/connect", status_code=303)
    d = project_dashboard(db, pid, _d(start), _d(end))
    logs = db.query(SyncLog).filter(SyncLog.project_id == pid).order_by(SyncLog.id.desc()).limit(10).all()
    return render(request, "dashboard.html", _db=db, p=p, d=d, logs=logs,
                  welcome=request.query_params.get("welcome"), imported=request.query_params.get("imported"))


@app.post("/projects/{pid}/sync")
def sync_now(pid: int, full: int = Form(0), db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    if p.connector == "odoo":
        sync_one_bg(pid, full=bool(full))
    elif p.connector == "agent":
        db.add(AgentTask(project_id=pid, kind="full_resync" if full else "sync_now", payload="{}"))
        db.commit()
    return RedirectResponse(f"/projects/{pid}?synced=1", status_code=303)


def _data_query(db, pid, dataset, start, end, q):
    qry = db.query(Fact).filter(Fact.project_id == pid, Fact.dataset == dataset)
    if start:
        qry = qry.filter(Fact.date >= start)
    if end:
        qry = qry.filter(Fact.date <= end)
    if q:
        like = f"%{q}%"
        qry = qry.filter((Fact.party.like(like)) | (Fact.item.like(like)) | (Fact.doc.like(like)) | (Fact.branch.like(like)))
    return qry.order_by(Fact.date.desc().nullslast(), Fact.id.desc())


@app.get("/projects/{pid}/data", response_class=HTMLResponse)
def data_page(pid: int, request: Request, dataset: str = "sales", start: str = "", end: str = "", q: str = "",
              db=Depends(get_db), u=Depends(user)):
    p = project_or_404(db, pid)
    qry = _data_query(db, pid, dataset, _d(start), _d(end), q)
    total = qry.count()
    rows = qry.limit(300).all()
    return render(request, "data.html", _db=db, p=p, dataset=dataset, rows=rows, total=total,
                  start=start, end=end, q=q)


@app.get("/projects/{pid}/data.csv")
def data_csv(pid: int, dataset: str = "sales", start: str = "", end: str = "", q: str = "",
             db=Depends(get_db), u=Depends(user)):
    project_or_404(db, pid)
    spec = DATASETS[dataset]["fields"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([lbl for _f, (_c, lbl, _r) in spec.items()])
    for r in _data_query(db, pid, dataset, _d(start), _d(end), q).yield_per(2000):
        w.writerow([getattr(r, col) if getattr(r, col) is not None else "" for _f, (col, _l, _r) in spec.items()])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{dataset}_{pid}.csv"'})


# ------------------------------------------------------------------ settings

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", db=Depends(get_db), u=Depends(user)):
    s = {k: get_setting(db, k) for k in ("telegram_token", "telegram_chat", "report_hour")}
    return render(request, "settings.html", _db=db, s=s, msg=msg, preview=build_summary(db))


@app.post("/settings")
def settings_save(telegram_token: str = Form(""), telegram_chat: str = Form(""), report_hour: str = Form("23"),
                  db=Depends(get_db), u=Depends(user)):
    set_setting(db, "telegram_token", telegram_token.strip())
    set_setting(db, "telegram_chat", telegram_chat.strip())
    set_setting(db, "report_hour", report_hour.strip() or "23")
    return RedirectResponse("/settings?msg=تم الحفظ", status_code=303)


@app.post("/settings/test")
def settings_test(db=Depends(get_db), u=Depends(user)):
    ok, msg = send_telegram(db, build_summary(db))
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


# ------------------------------------------------------------------ agent API (called by MarsadAgent)

def agent_project(db, token):
    if not token:
        raise HTTPException(401, "missing token")
    p = db.query(Project).filter(Project.agent_token == token).first()
    if not p:
        raise HTTPException(401, "bad token")
    return p


@app.post("/api/agent/hello")
async def api_hello(request: Request, x_agent_token: str = Header(""), db=Depends(get_db)):
    p = agent_project(db, x_agent_token)
    body = await request.json()
    p.agent_last_seen = now()
    p.agent_info = json.dumps(body, ensure_ascii=False)[:4000]
    if body.get("db_ok") and p.status == "new":
        p.status = "connected"
    db.commit()
    tasks = (db.query(AgentTask).filter(AgentTask.project_id == p.id, AgentTask.status == "pending")
             .order_by(AgentTask.id).limit(10).all())
    out_tasks = []
    for t in tasks:
        t.status = "sent"
        out_tasks.append({"id": t.id, "kind": t.kind, "payload": json.loads(t.payload or "{}")})
    db.commit()
    maps = [{"id": m.id, "dataset": m.dataset, "sql": m.sql, "mode": m.mode,
             "watermark_field": m.watermark_field, "watermark_value": m.watermark_value}
            for m in p.mappings if m.enabled and m.sql]
    active = time.time() - LAST_UI["t"] < 600 or bool(out_tasks)
    return {"project": p.name, "interval_minutes": int(p.conn.get("interval", 30)),
            "poll_seconds": 15 if active else C.AGENT_IDLE_POLL,
            "need_schema": not p.schema_json, "mappings": maps, "tasks": out_tasks}


@app.post("/api/agent/schema")
async def api_schema(request: Request, x_agent_token: str = Header(""), db=Depends(get_db)):
    p = agent_project(db, x_agent_token)
    body = await request.json()
    p.schema_json = json.dumps(body.get("tables", []), ensure_ascii=False, default=str)
    p.agent_last_seen = now()
    db.commit()
    return {"ok": True}


@app.post("/api/agent/task_result")
async def api_task_result(request: Request, x_agent_token: str = Header(""), db=Depends(get_db)):
    p = agent_project(db, x_agent_token)
    body = await request.json()
    t = db.get(AgentTask, int(body.get("id", 0)))
    if not t or t.project_id != p.id:
        raise HTTPException(404)
    t.status = "done" if body.get("ok") else "error"
    t.result = json.dumps({"ok": body.get("ok"), "error": body.get("error"), "data": body.get("data")},
                          ensure_ascii=False, default=str)[:2_000_000]
    t.finished_at = now()
    db.commit()
    return {"ok": True}


def _max_wm(vals):
    try:
        return str(max(vals, key=lambda v: float(v)))
    except ValueError:
        return max(vals)


@app.post("/api/agent/push")
async def api_push(request: Request, x_agent_token: str = Header(""), db=Depends(get_db)):
    p = agent_project(db, x_agent_token)
    body = await request.json()
    m = db.get(Mapping, int(body.get("mapping_id", 0)))
    if not m or m.project_id != p.id:
        raise HTTPException(404, "mapping not found")
    rows = body.get("rows") or []
    replace = bool(body.get("replace"))
    n = upsert(db, p.id, m.dataset, rows, replace=replace)
    wf = (m.watermark_field or "").lower()
    if m.mode == "incremental" and wf:
        vals = [str(r.get(wf) or r.get(m.watermark_field) or "") for r in rows]
        vals = [v for v in vals if v and v != "None"]
        if vals:
            m.watermark_value = _max_wm(vals + ([m.watermark_value] if m.watermark_value else []))[:60]
    if body.get("last_chunk", True):
        m.last_rows = int(body.get("total_rows", n))
        m.last_run = now()
        m.last_error = ""
        log(db, p.id, m.dataset, m.last_rows, True, "وكيل المزامنة")
    p.last_sync_at, p.agent_last_seen, p.status, p.last_error = now(), now(), "connected", ""
    db.commit()
    return {"ok": True, "rows": n, "watermark": m.watermark_value}


@app.post("/api/agent/error")
async def api_error(request: Request, x_agent_token: str = Header(""), db=Depends(get_db)):
    p = agent_project(db, x_agent_token)
    body = await request.json()
    msg = str(body.get("error", ""))[:2000]
    mid = body.get("mapping_id")
    if mid:
        m = db.get(Mapping, int(mid))
        if m and m.project_id == p.id:
            m.last_error, m.last_run = msg, now()
    p.last_error = msg
    p.agent_last_seen = now()
    db.commit()
    log(db, p.id, body.get("dataset", ""), 0, False, msg)
    return {"ok": True}
