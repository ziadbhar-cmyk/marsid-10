from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import TIMEZONE
from .datasets import CUSTOMER_WORDS, SUPPLIER_WORDS
from .db import Fact


def today():
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _sum(db, pid, ds, col, start=None, end=None, extra=None):
    q = select(func.coalesce(func.sum(col), 0)).where(Fact.project_id == pid, Fact.dataset == ds)
    if start:
        q = q.where(Fact.date >= start)
    if end:
        q = q.where(Fact.date <= end)
    if extra is not None:
        q = q.where(extra)
    return float(db.execute(q).scalar() or 0)


def _count_docs(db, pid, ds, start, end):
    q = select(func.count(func.distinct(Fact.doc))).where(
        Fact.project_id == pid, Fact.dataset == ds, Fact.date >= start, Fact.date <= end)
    return int(db.execute(q).scalar() or 0)


def _group(db, pid, ds, col, start, end, limit=10, value=Fact.amount):
    q = (select(col, func.sum(value).label("v"))
         .where(Fact.project_id == pid, Fact.dataset == ds, Fact.date >= start, Fact.date <= end)
         .group_by(col).order_by(func.sum(value).desc()).limit(limit))
    return [{"k": k or "غير محدد", "v": round(float(v or 0), 2)} for k, v in db.execute(q)]


def daily_series(db, pid, ds, start, end):
    q = (select(Fact.date, func.sum(Fact.amount))
         .where(Fact.project_id == pid, Fact.dataset == ds, Fact.date >= start, Fact.date <= end)
         .group_by(Fact.date))
    got = {d: float(v or 0) for d, v in db.execute(q)}
    days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    return [d.isoformat() for d in days], [round(got.get(d, 0), 2) for d in days]


def balances(db, pid):
    q = (select(Fact.channel, func.sum(Fact.amount))
         .where(Fact.project_id == pid, Fact.dataset == "balances").group_by(Fact.channel))
    rec = pay = emp = 0.0
    for t, v in db.execute(q):
        t = (t or "").strip().lower()
        if t in CUSTOMER_WORDS:
            rec += float(v or 0)
        elif t in SUPPLIER_WORDS:
            pay += float(v or 0)
        else:
            emp += float(v or 0)
    return rec, pay, emp


def has_data(db, pid, ds):
    q = select(func.count(Fact.id)).where(Fact.project_id == pid, Fact.dataset == ds)
    return int(db.execute(q).scalar() or 0)


def project_kpis(db, pid, ref=None):
    t = ref or today()
    y = t - timedelta(days=1)
    m0 = t.replace(day=1)
    pm_end = m0 - timedelta(days=1)
    pm0 = pm_end.replace(day=1)
    # same period last month (1..day)
    try:
        pm_same = pm0.replace(day=t.day)
    except ValueError:
        pm_same = pm_end
    k = {}
    k["sales_today"] = _sum(db, pid, "sales", Fact.amount, t, t)
    k["sales_yesterday"] = _sum(db, pid, "sales", Fact.amount, y, y)
    k["sales_mtd"] = _sum(db, pid, "sales", Fact.amount, m0, t)
    k["sales_pm_same"] = _sum(db, pid, "sales", Fact.amount, pm0, pm_same)
    k["sales_pm"] = _sum(db, pid, "sales", Fact.amount, pm0, pm_end)
    k["invoices_today"] = _count_docs(db, pid, "sales", t, t)
    k["invoices_mtd"] = _count_docs(db, pid, "sales", m0, t)
    cost_mtd = _sum(db, pid, "sales", Fact.amount2, m0, t)
    sales_with_cost = _sum(db, pid, "sales", Fact.amount, m0, t, Fact.amount2.isnot(None))
    k["gross_profit_mtd"] = sales_with_cost - cost_mtd if sales_with_cost else None
    k["margin_mtd"] = (k["gross_profit_mtd"] / sales_with_cost * 100) if sales_with_cost else None
    k["purchases_mtd"] = _sum(db, pid, "purchases", Fact.amount, m0, t)
    k["expenses_mtd"] = _sum(db, pid, "expenses", Fact.amount, m0, t)
    k["production_mtd"] = _sum(db, pid, "production", Fact.qty, m0, t)
    cin = _sum(db, pid, "cash", Fact.amount, m0, t)
    cout = _sum(db, pid, "cash", Fact.amount2, m0, t)
    k["cash_in_mtd"], k["cash_out_mtd"] = cin, cout
    k["cash_in_today"] = _sum(db, pid, "cash", Fact.amount, t, t)
    k["cash_out_today"] = _sum(db, pid, "cash", Fact.amount2, t, t)
    k["receivables"], k["payables"], k["other_balances"] = balances(db, pid)
    k["stock_value"] = _sum(db, pid, "stock", Fact.amount)
    k["stock_items"] = int(db.execute(select(func.count(Fact.id)).where(
        Fact.project_id == pid, Fact.dataset == "stock", Fact.qty > 0)).scalar() or 0)
    k["avg_invoice_mtd"] = k["sales_mtd"] / k["invoices_mtd"] if k["invoices_mtd"] else 0
    k["growth_vs_pm"] = ((k["sales_mtd"] - k["sales_pm_same"]) / k["sales_pm_same"] * 100) if k["sales_pm_same"] else None
    return k


def project_dashboard(db, pid, start=None, end=None):
    t = today()
    end = end or t
    start = start or (end - timedelta(days=29))
    labels, series = daily_series(db, pid, "sales", start, end)
    _, pur = daily_series(db, pid, "purchases", start, end)
    _, exp = daily_series(db, pid, "expenses", start, end)
    return {
        "kpi": project_kpis(db, pid, t),
        "start": start, "end": end,
        "labels": labels, "sales": series, "purchases": pur, "expenses": exp,
        "by_branch": _group(db, pid, "sales", Fact.branch, start, end),
        "by_channel": _group(db, pid, "sales", Fact.channel, start, end),
        "top_customers": _group(db, pid, "sales", Fact.party, start, end),
        "top_items": _group(db, pid, "sales", Fact.item, start, end),
        "top_expenses": _group(db, pid, "expenses", Fact.party, start, end),
        "top_suppliers": _group(db, pid, "purchases", Fact.party, start, end),
        "production_items": _group(db, pid, "production", Fact.item, start, end, value=Fact.qty),
        "stock_by_wh": _stock_by_wh(db, pid),
        "low_stock": _low_stock(db, pid),
        "top_receivables": _top_bal(db, pid, CUSTOMER_WORDS),
        "top_payables": _top_bal(db, pid, SUPPLIER_WORDS),
        "datasets": {ds: has_data(db, pid, ds) for ds in
                     ("sales", "purchases", "expenses", "cash", "production", "stock", "balances")},
    }


def _stock_by_wh(db, pid):
    q = (select(Fact.branch, func.sum(Fact.amount), func.sum(Fact.qty))
         .where(Fact.project_id == pid, Fact.dataset == "stock").group_by(Fact.branch)
         .order_by(func.sum(Fact.amount).desc()).limit(10))
    return [{"k": k or "غير محدد", "v": round(float(v or 0), 2), "q": float(q_ or 0)} for k, v, q_ in db.execute(q)]


def _low_stock(db, pid, limit=12):
    q = (select(Fact.item, Fact.branch, Fact.qty)
         .where(Fact.project_id == pid, Fact.dataset == "stock", Fact.qty <= 3)
         .order_by(Fact.qty).limit(limit))
    return [{"item": i, "wh": w, "qty": float(qq or 0)} for i, w, qq in db.execute(q)]


def _top_bal(db, pid, words, limit=8):
    q = (select(Fact.party, Fact.channel, Fact.amount)
         .where(Fact.project_id == pid, Fact.dataset == "balances").order_by(Fact.amount.desc()).limit(400))
    out = [{"k": p, "v": float(a or 0)} for p, t, a in db.execute(q) if (t or "").strip().lower() in words]
    return out[:limit]


def overview(db, projects, days=30):
    t = today()
    start = t - timedelta(days=days - 1)
    rows = []
    chart = []
    for p in projects:
        k = project_kpis(db, p.id, t)
        labels, series = daily_series(db, p.id, "sales", start, t)
        chart.append({"name": p.name, "color": p.color, "data": series})
        rows.append({"p": p, "k": k})
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    totals = {
        "sales_today": sum(r["k"]["sales_today"] for r in rows),
        "sales_mtd": sum(r["k"]["sales_mtd"] for r in rows),
        "receivables": sum(r["k"]["receivables"] for r in rows),
        "payables": sum(r["k"]["payables"] for r in rows),
        "stock_value": sum(r["k"]["stock_value"] for r in rows),
        "cash_net_today": sum(r["k"]["cash_in_today"] - r["k"]["cash_out_today"] for r in rows),
    }
    return {"rows": rows, "labels": labels, "chart": chart, "totals": totals, "today": t}
