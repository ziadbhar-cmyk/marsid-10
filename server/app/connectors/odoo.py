"""Odoo connector over the standard XML-RPC external API (works on Odoo 12 → 18+).

Needs: url, db, username (login email) and password or API key.
Reads only. Each dataset keeps its own watermark (write_date) in Mapping.
"""
import xmlrpc.client
from datetime import datetime

from ..db import Mapping, log, now
from ..ingest import upsert

BATCH = 2000


class Odoo:
    def __init__(self, url, db, username, password, timeout=60):
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        transport = _TimeoutTransport(timeout) if self.url.startswith("http:") else _TimeoutSafeTransport(timeout)
        self.common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", transport=transport, allow_none=True)
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", transport=transport, allow_none=True)
        self.uid = None
        self._fields = {}

    def login(self):
        version = self.common.version()
        self.uid = self.common.authenticate(self.db, self.username, self.password, {})
        if not self.uid:
            raise PermissionError("فشل تسجيل الدخول: تأكد من اسم المستخدم وكلمة المرور / مفتاح API واسم قاعدة البيانات")
        return version

    def call(self, model, method, *args, **kw):
        return self.models.execute_kw(self.db, self.uid, self.password, model, method, list(args), kw)

    def has_model(self, model):
        try:
            return bool(self.call("ir.model", "search_count", [("model", "=", model)]))
        except Exception:
            return False

    def fields(self, model):
        if model not in self._fields:
            self._fields[model] = set(self.call(model, "fields_get", attributes=["type"]).keys())
        return self._fields[model]

    def pick(self, model, wanted):
        have = self.fields(model)
        return [f for f in wanted if f in have]

    def read_all(self, model, domain, fields, order="id"):
        offset = 0
        while True:
            rows = self.call(model, "search_read", domain, fields=fields, offset=offset,
                             limit=BATCH, order=order)
            if not rows:
                break
            yield rows
            if len(rows) < BATCH:
                break
            offset += BATCH


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        c = super().make_connection(host)
        c.timeout = self._timeout
        return c


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        c = super().make_connection(host)
        c.timeout = self._timeout
        return c


def m2o(v):
    return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else ""


def client_for(project):
    c = project.conn
    return Odoo(c.get("url", ""), c.get("db", ""), c.get("username", ""), c.get("password", ""))


def test_connection(conn):
    o = Odoo(conn["url"], conn["db"], conn["username"], conn["password"], timeout=30)
    version = o.login()
    companies = [c["name"] for c in o.call("res.company", "search_read", [], fields=["name"], limit=10)]
    mods = [m for m in ("sale", "point_of_sale", "purchase", "stock", "account", "mrp")
            if o.call("ir.module.module", "search_count", [("name", "=", m), ("state", "=", "installed")])]
    return {"version": version.get("server_version", "?"), "companies": companies, "modules": mods}


# ---------------------------------------------------------------- datasets

def _since(mp):
    return mp.watermark_value or "1970-01-01 00:00:00"


def _wm(rows, current):
    best = current or ""
    for r in rows:
        w = r.get("write_date") or ""
        if w > best:
            best = w
    return best


def pull_sales(o, mp):
    out = []
    wm = mp.watermark_value
    if o.has_model("sale.order"):
        f = o.pick("sale.order", ["name", "date_order", "partner_id", "amount_total", "team_id",
                                  "warehouse_id", "margin", "state", "write_date"])
        dom = [("state", "in", ["sale", "done"]), ("write_date", ">", _since(mp))]
        for rows in o.read_all("sale.order", dom, f, order="write_date"):
            for r in rows:
                margin = r.get("margin")
                out.append({"uid": f"so-{r['id']}", "date": (r.get("date_order") or "")[:10],
                            "doc_no": r["name"], "customer": m2o(r.get("partner_id")),
                            "branch": m2o(r.get("warehouse_id")) or m2o(r.get("team_id")),
                            "channel": "مبيعات", "amount": r.get("amount_total") or 0,
                            "cost": (r.get("amount_total") or 0) - margin if isinstance(margin, (int, float)) else None})
            wm = _wm(rows, wm)
    if o.has_model("pos.order"):
        f = o.pick("pos.order", ["name", "date_order", "partner_id", "amount_total", "config_id",
                                 "margin", "state", "write_date"])
        dom = [("state", "in", ["paid", "done", "invoiced"]), ("write_date", ">", _since(mp))]
        for rows in o.read_all("pos.order", dom, f, order="write_date"):
            for r in rows:
                margin = r.get("margin")
                out.append({"uid": f"pos-{r['id']}", "date": (r.get("date_order") or "")[:10],
                            "doc_no": r["name"], "customer": m2o(r.get("partner_id")) or "نقدي",
                            "branch": m2o(r.get("config_id")), "channel": "نقطة بيع",
                            "amount": r.get("amount_total") or 0,
                            "cost": (r.get("amount_total") or 0) - margin if isinstance(margin, (int, float)) else None})
            wm = _wm(rows, wm)
    return out, wm, False


def pull_purchases(o, mp):
    out, wm = [], mp.watermark_value
    if not o.has_model("purchase.order"):
        return out, wm, False
    f = o.pick("purchase.order", ["name", "date_order", "date_approve", "partner_id", "amount_total",
                                  "picking_type_id", "write_date"])
    dom = [("state", "in", ["purchase", "done"]), ("write_date", ">", _since(mp))]
    for rows in o.read_all("purchase.order", dom, f, order="write_date"):
        for r in rows:
            out.append({"uid": f"po-{r['id']}", "date": (r.get("date_approve") or r.get("date_order") or "")[:10],
                        "doc_no": r["name"], "supplier": m2o(r.get("partner_id")),
                        "branch": m2o(r.get("picking_type_id")), "amount": r.get("amount_total") or 0})
        wm = _wm(rows, wm)
    return out, wm, False


def pull_stock(o, mp):
    out = []
    if not o.has_model("stock.quant"):
        return out, mp.watermark_value, True
    f = o.pick("stock.quant", ["product_id", "location_id", "warehouse_id", "quantity", "value"])
    dom = [("location_id.usage", "=", "internal")]
    for rows in o.read_all("stock.quant", dom, f):
        for r in rows:
            out.append({"uid": f"q-{r['id']}", "item": m2o(r.get("product_id")),
                        "warehouse": m2o(r.get("warehouse_id")) or m2o(r.get("location_id")),
                        "qty": r.get("quantity") or 0, "value": r.get("value") or 0})
    return out, now().strftime("%Y-%m-%d %H:%M:%S"), True


def pull_balances(o, mp):
    out = []
    f = o.pick("res.partner", ["name", "credit", "debit"])
    if "credit" not in f:
        return out, mp.watermark_value, True
    try:
        rows_iter = o.read_all("res.partner", ["|", ("credit", "!=", 0), ("debit", "!=", 0)], f)
        for rows in rows_iter:
            for r in rows:
                if r.get("credit"):
                    out.append({"party_type": "عميل", "party": r["name"], "balance": r["credit"]})
                if r.get("debit"):
                    out.append({"party_type": "مورد", "party": r["name"], "balance": r["debit"]})
    except xmlrpc.client.Fault:
        pass
    return out, now().strftime("%Y-%m-%d %H:%M:%S"), True


def pull_cash(o, mp):
    out, wm = [], mp.watermark_value
    model = "account.bank.statement.line"
    if not o.has_model(model):
        return out, wm, False
    f = o.pick(model, ["date", "journal_id", "amount", "payment_ref", "name", "move_id", "write_date"])
    for rows in o.read_all(model, [("write_date", ">", _since(mp))], f, order="write_date"):
        for r in rows:
            amt = r.get("amount") or 0
            out.append({"uid": f"bsl-{r['id']}", "date": r.get("date"), "doc_no": m2o(r.get("move_id")),
                        "account": m2o(r.get("journal_id")),
                        "description": r.get("payment_ref") or r.get("name") or "",
                        "amount_in": amt if amt > 0 else 0, "amount_out": -amt if amt < 0 else 0})
        wm = _wm(rows, wm)
    return out, wm, False


def pull_expenses(o, mp):
    out, wm = [], mp.watermark_value
    model = "account.move.line"
    fields = o.fields(model)
    if "account_type" in o.fields("account.account"):
        dom = [("account_id.account_type", "in", ["expense", "expense_direct_cost"])]
    elif "user_type_id" in o.fields("account.account"):
        dom = [("account_id.user_type_id.type", "=", "other"), ("account_id.internal_group", "=", "expense")]
    else:
        return out, wm, False
    if "parent_state" in fields:
        dom.append(("parent_state", "=", "posted"))
    dom.append(("write_date", ">", _since(mp)))
    f = o.pick(model, ["date", "move_name", "account_id", "name", "debit", "credit", "write_date"])
    try:
        for rows in o.read_all(model, dom, f, order="write_date"):
            for r in rows:
                out.append({"uid": f"aml-{r['id']}", "date": r.get("date"), "doc_no": r.get("move_name") or "",
                            "category": m2o(r.get("account_id")), "description": r.get("name") or "",
                            "amount": (r.get("debit") or 0) - (r.get("credit") or 0)})
            wm = _wm(rows, wm)
    except xmlrpc.client.Fault:
        pass
    return out, wm, False


def pull_production(o, mp):
    out, wm = [], mp.watermark_value
    if not o.has_model("mrp.production"):
        return out, wm, False
    f = o.pick("mrp.production", ["name", "date_finished", "date_planned_start", "date_start", "product_id",
                                  "qty_produced", "product_qty", "write_date"])
    for rows in o.read_all("mrp.production", [("state", "=", "done"), ("write_date", ">", _since(mp))], f,
                           order="write_date"):
        for r in rows:
            d = r.get("date_finished") or r.get("date_start") or r.get("date_planned_start") or ""
            out.append({"uid": f"mo-{r['id']}", "date": d[:10], "order_no": r["name"],
                        "product": m2o(r.get("product_id")),
                        "qty": r.get("qty_produced") or r.get("product_qty") or 0})
        wm = _wm(rows, wm)
    return out, wm, False


PULLERS = {
    "sales": pull_sales, "purchases": pull_purchases, "stock": pull_stock, "balances": pull_balances,
    "cash": pull_cash, "expenses": pull_expenses, "production": pull_production,
}


def ensure_mappings(db, project):
    have = {m.dataset for m in project.mappings}
    for ds in PULLERS:
        if ds not in have:
            db.add(Mapping(project_id=project.id, dataset=ds, mode="builtin", sql=""))
    db.commit()
    db.refresh(project)


def sync_project(db, project, full=False):
    ensure_mappings(db, project)
    o = client_for(project)
    o.login()
    total = 0
    errors = []
    for mp in project.mappings:
        if not mp.enabled or mp.dataset not in PULLERS:
            continue
        if full:
            mp.watermark_value = ""
        try:
            rows, wm, replace = PULLERS[mp.dataset](o, mp)
            n = upsert(db, project.id, mp.dataset, rows, replace=replace) if (rows or replace) else 0
            mp.watermark_value = wm or mp.watermark_value
            mp.last_rows, mp.last_run, mp.last_error = n, now(), ""
            total += n
            if n:
                log(db, project.id, mp.dataset, n, True, "Odoo")
        except Exception as e:  # keep going with other datasets
            mp.last_error = str(e)[:1000]
            mp.last_run = now()
            errors.append(f"{mp.dataset}: {e}")
        db.commit()
    project.last_sync_at = now()
    project.status = "connected" if not errors else "error"
    project.last_error = "\n".join(errors)[:2000]
    db.commit()
    return total, errors
