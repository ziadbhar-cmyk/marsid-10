"""Daily summary to Telegram (free). Uses only the standard library."""
import json
import urllib.parse
import urllib.request

from .db import Project, get_setting
from .metrics import project_kpis, today


def fmt(n):
    return f"{n:,.0f}"


def build_summary(db):
    t = today()
    lines = [f"📊 ملخص يوم {t.isoformat()}", ""]
    tot = 0
    for p in db.query(Project).order_by(Project.id).all():
        k = project_kpis(db, p.id, t)
        tot += k["sales_today"]
        lines.append(f"🏪 {p.name}")
        lines.append(f"  مبيعات اليوم: {fmt(k['sales_today'])} {p.currency} ({k['invoices_today']} فاتورة)")
        lines.append(f"  منذ بداية الشهر: {fmt(k['sales_mtd'])}")
        if k["growth_vs_pm"] is not None:
            lines.append(f"  مقارنة بنفس الفترة من الشهر الماضي: {k['growth_vs_pm']:+.1f}%")
        if k["cash_in_today"] or k["cash_out_today"]:
            lines.append(f"  الخزينة اليوم: +{fmt(k['cash_in_today'])} / -{fmt(k['cash_out_today'])}")
        if k["receivables"]:
            lines.append(f"  ديون العملاء: {fmt(k['receivables'])}")
        if p.last_sync_at is None:
            lines.append("  ⚠️ لم تتم مزامنة بعد")
        lines.append("")
    lines.append(f"الإجمالي اليوم لكل الأنشطة: {fmt(tot)}")
    return "\n".join(lines)


def send_telegram(db, text):
    token = get_setting(db, "telegram_token")
    chat = get_setting(db, "telegram_chat")
    if not token or not chat:
        return False, "لم يتم ضبط بيانات تيليجرام في الإعدادات"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20) as r:
            res = json.loads(r.read().decode())
        return bool(res.get("ok")), "تم الإرسال" if res.get("ok") else str(res)
    except Exception as e:
        return False, str(e)
