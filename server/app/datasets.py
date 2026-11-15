"""Unified data model.

Every dataset from every system is stored in one table (facts) using generic
columns. This keeps the dashboards identical whatever the source system is.
Each dataset declares which of its fields go to which generic column.
"""

# generic columns in Fact: doc, date, branch, channel, party, item, qty, amount, amount2

DATASETS = {
    "sales": {
        "label": "المبيعات",
        "kind": "flow",
        "fields": {
            "date": ("date", "التاريخ", True),
            "doc_no": ("doc", "رقم الفاتورة", True),
            "branch": ("branch", "الفرع / المخزن", False),
            "channel": ("channel", "القناة (محل، صفحة، توصيل)", False),
            "customer": ("party", "العميل", False),
            "item": ("item", "الصنف", False),
            "qty": ("qty", "الكمية", False),
            "amount": ("amount", "قيمة البيع", True),
            "cost": ("amount2", "التكلفة", False),
        },
        "key": ["doc_no", "item", "line"],
    },
    "purchases": {
        "label": "المشتريات",
        "kind": "flow",
        "fields": {
            "date": ("date", "التاريخ", True),
            "doc_no": ("doc", "رقم الفاتورة", True),
            "branch": ("branch", "الفرع / المخزن", False),
            "supplier": ("party", "المورد", False),
            "item": ("item", "الصنف", False),
            "qty": ("qty", "الكمية", False),
            "amount": ("amount", "القيمة", True),
        },
        "key": ["doc_no", "item", "line"],
    },
    "expenses": {
        "label": "المصروفات",
        "kind": "flow",
        "fields": {
            "date": ("date", "التاريخ", True),
            "doc_no": ("doc", "رقم المستند", False),
            "branch": ("branch", "الفرع", False),
            "category": ("party", "بند المصروف", True),
            "description": ("item", "البيان", False),
            "amount": ("amount", "القيمة", True),
        },
        "key": ["doc_no", "category", "description", "line"],
    },
    "cash": {
        "label": "حركة الخزينة والمصارف",
        "kind": "flow",
        "fields": {
            "date": ("date", "التاريخ", True),
            "doc_no": ("doc", "رقم المستند", False),
            "account": ("branch", "الخزينة / المصرف", True),
            "description": ("item", "البيان", False),
            "amount_in": ("amount", "مقبوضات", False),
            "amount_out": ("amount2", "مدفوعات", False),
        },
        "key": ["doc_no", "account", "line"],
    },
    "production": {
        "label": "الإنتاج",
        "kind": "flow",
        "fields": {
            "date": ("date", "التاريخ", True),
            "order_no": ("doc", "رقم أمر الإنتاج", True),
            "branch": ("branch", "خط الإنتاج / المصنع", False),
            "product": ("item", "المنتج", True),
            "qty": ("qty", "الكمية المنتجة", False),
            "cost": ("amount", "تكلفة الإنتاج", False),
        },
        "key": ["order_no", "product", "line"],
    },
    "stock": {
        "label": "المخزون (لقطة حالية)",
        "kind": "snapshot",
        "fields": {
            "item": ("item", "الصنف", True),
            "warehouse": ("branch", "المخزن", False),
            "qty": ("qty", "الكمية", True),
            "value": ("amount", "القيمة بالتكلفة", False),
        },
        "key": ["item", "warehouse"],
    },
    "balances": {
        "label": "أرصدة العملاء والموردين (لقطة حالية)",
        "kind": "snapshot",
        "fields": {
            "party_type": ("channel", "النوع: عميل / مورد / موظف", True),
            "party": ("party", "الاسم", True),
            "balance": ("amount", "الرصيد", True),
        },
        "key": ["party_type", "party"],
    },
}

CUSTOMER_WORDS = {"عميل", "عملاء", "زبون", "customer", "c", "receivable"}
SUPPLIER_WORDS = {"مورد", "موردين", "supplier", "vendor", "s", "payable"}

ACTIVITIES = {
    "clothing": "تجارة ملابس",
    "factory": "تصنيع",
    "furniture": "أثاث ومنزلية",
    "wholesale": "تجارة جملة",
    "pharmacy": "صيدلية",
    "other": "نشاط آخر",
}

CONNECTORS = {
    "odoo": "أودو Odoo (ربط مباشر عبر API)",
    "agent": "منظومة محلية (وكيل مزامنة على جهاز السيرفر)",
    "excel": "ملفات Excel / CSV (رفع يدوي أو تصدير)",
}

DB_ENGINES = {
    "sqlserver": "Microsoft SQL Server",
    "access": "Microsoft Access (mdb/accdb)",
    "mysql": "MySQL / MariaDB",
    "postgres": "PostgreSQL",
    "firebird": "Firebird / Interbase",
    "sqlite": "SQLite",
    "odbc": "أي مصدر ODBC آخر (DSN)",
}

# SQL templates shown to the user when adding a mapping for an agent project.
SQL_TEMPLATES = {
    "sales": """-- اكتب الاستعلام بحيث تكون أسماء الأعمدة بالإنجليزي كما هي تحت (AS ...)
-- {since} يستبدله الوكيل تلقائياً بآخر تاريخ تمت مزامنته
SELECT
    d.LineID        AS line,
    h.InvoiceDate   AS date,
    h.InvoiceNo     AS doc_no,
    h.StoreName     AS branch,
    'محل'           AS channel,
    h.CustomerName  AS customer,
    d.ItemName      AS item,
    d.Qty           AS qty,
    d.Total         AS amount,
    d.Qty * d.Cost  AS cost,
    h.ModifiedAt    AS _wm
FROM SalesInvoices h
JOIN SalesInvoiceLines d ON d.InvoiceID = h.ID
WHERE h.ModifiedAt >= {since}""",
    "purchases": """SELECT
    d.LineID AS line, h.InvoiceDate AS date, h.InvoiceNo AS doc_no,
    h.SupplierName AS supplier, d.ItemName AS item, d.Qty AS qty, d.Total AS amount,
    h.ModifiedAt AS _wm
FROM PurchaseInvoices h JOIN PurchaseInvoiceLines d ON d.InvoiceID = h.ID
WHERE h.ModifiedAt >= {since}""",
    "expenses": """SELECT
    e.ID AS line, e.ExpDate AS date, e.VoucherNo AS doc_no,
    e.CategoryName AS category, e.Notes AS description, e.Amount AS amount,
    e.ExpDate AS _wm
FROM Expenses e
WHERE e.ExpDate >= {since}""",
    "cash": """SELECT
    t.ID AS line, t.TransDate AS date, t.VoucherNo AS doc_no, t.BoxName AS account,
    t.Notes AS description,
    CASE WHEN t.Amount > 0 THEN t.Amount ELSE 0 END AS amount_in,
    CASE WHEN t.Amount < 0 THEN -t.Amount ELSE 0 END AS amount_out,
    t.TransDate AS _wm
FROM CashTransactions t
WHERE t.TransDate >= {since}""",
    "production": """SELECT
    p.ID AS line, p.OrderDate AS date, p.OrderNo AS order_no, p.ProductName AS product,
    p.ProducedQty AS qty, p.TotalCost AS cost, p.OrderDate AS _wm
FROM ProductionOrders p
WHERE p.OrderDate >= {since}""",
    "stock": """-- لقطة كاملة: تُستبدل كل مرة (بدون {since})
SELECT i.ItemName AS item, s.StoreName AS warehouse, s.Qty AS qty, s.Qty * i.Cost AS value
FROM ItemStock s JOIN Items i ON i.ID = s.ItemID
WHERE s.Qty <> 0""",
    "balances": """-- لقطة كاملة: تُستبدل كل مرة
SELECT 'عميل' AS party_type, c.Name AS party, c.Balance AS balance FROM Customers c WHERE c.Balance <> 0
UNION ALL
SELECT 'مورد', s.Name, s.Balance FROM Suppliers s WHERE s.Balance <> 0""",
}
