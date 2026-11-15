import csv
import io
from pathlib import Path

from openpyxl import load_workbook

from .datasets import DATASETS

# words that help auto-guess which column is which (Arabic + English)
HINTS = {
    "date": ["تاريخ", "date", "اليوم"],
    "doc_no": ["رقم الفاتورة", "رقم", "فاتورة", "invoice", "doc", "no", "المستند", "سند"],
    "order_no": ["أمر", "امر", "order"],
    "branch": ["فرع", "مخزن", "branch", "store", "المعرض"],
    "warehouse": ["مخزن", "مستودع", "warehouse", "store"],
    "channel": ["قناة", "channel", "مصدر"],
    "customer": ["عميل", "زبون", "customer", "client"],
    "supplier": ["مورد", "supplier", "vendor"],
    "party": ["اسم", "الحساب", "name", "party", "عميل", "مورد"],
    "party_type": ["نوع", "type"],
    "item": ["صنف", "مادة", "منتج", "item", "product", "البيان"],
    "product": ["منتج", "صنف", "product"],
    "qty": ["كمية", "عدد", "qty", "quantity"],
    "amount": ["إجمالي", "اجمالي", "القيمة", "المبلغ", "صافي", "total", "amount", "قيمة"],
    "cost": ["تكلفة", "cost"],
    "value": ["قيمة", "value", "تكلفة"],
    "balance": ["رصيد", "balance"],
    "category": ["بند", "نوع المصروف", "حساب", "category"],
    "description": ["بيان", "ملاحظات", "description", "notes"],
    "account": ["خزينة", "مصرف", "حساب", "account", "box"],
    "amount_in": ["مقبوض", "وارد", "مدين", "in", "debit", "قبض"],
    "amount_out": ["مدفوع", "صادر", "دائن", "out", "credit", "صرف"],
}


def read_table(path: Path, sheet=None, max_rows=None):
    """Return (headers, rows as list of lists, sheet_names)."""
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1256", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|") if text else csv.excel
        data = list(csv.reader(io.StringIO(text), dialect))
        sheets = ["CSV"]
    else:
        wb = load_workbook(path, read_only=True, data_only=True)
        sheets = wb.sheetnames
        ws = wb[sheet] if sheet in sheets else wb[sheets[0]]
        data = []
        for i, r in enumerate(ws.iter_rows(values_only=True)):
            data.append(list(r))
            if max_rows and i > max_rows + 30:
                break
        wb.close()
    # header = first row having >= 2 non-empty cells
    h_idx = 0
    for i, r in enumerate(data[:30]):
        if sum(1 for c in r if c not in (None, "")) >= 2:
            h_idx = i
            break
    headers = [str(c).strip() if c not in (None, "") else f"عمود {j + 1}" for j, c in enumerate(data[h_idx] if data else [])]
    rows = [r for r in data[h_idx + 1:] if any(c not in (None, "") for c in r)]
    if max_rows:
        rows = rows[:max_rows]
    return headers, rows, sheets


def guess_mapping(dataset, headers):
    out = {}
    used = set()
    for field in DATASETS[dataset]["fields"]:
        for hint in HINTS.get(field, [field]):
            for j, h in enumerate(headers):
                if j in used:
                    continue
                if hint.lower() in h.lower():
                    out[field] = j
                    used.add(j)
                    break
            if field in out:
                break
    return out


def build_rows(dataset, headers, rows, mapping, constants=None):
    """mapping: field -> column index (int) ; constants: field -> fixed text"""
    out = []
    constants = constants or {}
    for n, r in enumerate(rows):
        d = {"line": n}
        for field, j in mapping.items():
            if j is None or j == "":
                continue
            j = int(j)
            d[field] = r[j] if j < len(r) else None
        for field, val in constants.items():
            if val:
                d[field] = val
        out.append(d)
    return out
