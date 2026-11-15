"""Turn rows (dicts using the unified field names) into Fact records."""
import hashlib
from datetime import date, datetime

from sqlalchemy import delete

from .datasets import DATASETS
from .db import Fact, now

DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y",
                "%d/%m/%Y %H:%M:%S", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y"]


def parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):  # Excel serial number
        try:
            from datetime import timedelta
            return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
        except Exception:
            return None
    s = str(v).strip()
    s = s.replace("Z", "")
    if len(s) > 19 and s[:4].isdigit():   # 2026-11-15 10:20:30.123 -> trim fraction
        s = s[:19]
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def parse_num(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("٬", "").replace("٫", ".")
    arabic = "٠١٢٣٤٥٦٧٨٩"
    s = "".join(str(arabic.index(c)) if c in arabic else c for c in s)
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
    except ValueError:
        return 0.0
    return -n if neg else n


def _txt(v, n=250):
    if v is None:
        return ""
    return str(v).strip()[:n]


def make_uid(dataset, row):
    if row.get("uid") not in (None, ""):
        raw = str(row["uid"])
    else:
        keys = DATASETS[dataset]["key"]
        raw = "|".join(_txt(row.get(k)) for k in keys)
        if not raw.strip("|"):
            raw = "|".join(f"{k}={row[k]}" for k in sorted(row))
    return hashlib.sha1(f"{dataset}|{raw}".encode()).hexdigest()


def row_to_fact(project_id, dataset, row):
    spec = DATASETS[dataset]["fields"]
    lower = {str(k).lower(): v for k, v in row.items()}
    vals = {}
    for field, (col, _label, _req) in spec.items():
        v = lower.get(field)
        if col == "date":
            vals[col] = parse_date(v)
        elif col in ("qty", "amount"):
            vals[col] = parse_num(v)
        elif col == "amount2":
            vals[col] = None if v in (None, "") else parse_num(v)
        else:
            vals[col] = _txt(v, 300 if col == "item" else 250)
    if "doc" in vals:
        vals["doc"] = vals["doc"][:120]
    return Fact(project_id=project_id, dataset=dataset, uid=make_uid(dataset, lower),
                synced_at=now(), **vals)


def upsert(db, project_id, dataset, rows, replace=False):
    """Insert rows; same uid overwrites. replace=True wipes the dataset first."""
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset}")
    if replace:
        db.execute(delete(Fact).where(Fact.project_id == project_id, Fact.dataset == dataset))
    facts = {}
    for r in rows:
        f = row_to_fact(project_id, dataset, r)
        facts[f.uid] = f          # last one wins inside the batch
    if not replace and facts:
        uids = list(facts)
        for i in range(0, len(uids), 500):
            db.execute(delete(Fact).where(Fact.project_id == project_id, Fact.dataset == dataset,
                                          Fact.uid.in_(uids[i:i + 500])))
    db.add_all(facts.values())
    db.commit()
    return len(facts)
