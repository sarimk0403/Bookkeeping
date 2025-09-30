import os
import csv
from io import BytesIO
from datetime import datetime, timezone, timedelta

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    flash, session, send_file, Response, abort
)
from jinja2 import DictLoader

from pymongo import MongoClient, ASCENDING, DESCENDING
from bson.objectid import ObjectId
import gridfs

from dotenv import load_dotenv
load_dotenv()


try:
    from werkzeug.utils import secure_filename
except Exception:
    def secure_filename(x): return x

# ------------------------------
# App config / Auth
# ------------------------------
APP_TITLE = "Bookkeeping"
ALLOWED_EXT = {"png", "jpg", "jpeg", "pdf"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")

# ------------------------------
# MongoDB / GridFS (Atlas)
# ------------------------------
MONGODB_URI = os.environ.get("MONGODB_URI")  # mongodb+srv://... (Atlas Drivers > Python)
MONGODB_DB = os.environ.get("MONGODB_DB", "bookkeeping")
if not MONGODB_URI:
    raise RuntimeError("Set MONGODB_URI in environment")

mongo_client = MongoClient(MONGODB_URI)
mdb = mongo_client[MONGODB_DB]
expenses_col = mdb["expenses"]
fs = gridfs.GridFS(mdb)

# Helpful indexes (idempotent)
expenses_col.create_index([("date", DESCENDING)])
expenses_col.create_index([("category", ASCENDING)])
expenses_col.create_index([("amount", DESCENDING)])
expenses_col.create_index([("vendor", ASCENDING)])

# ------------------------------
# Helpers
# ------------------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def _oid(id_str):
    try:
        return ObjectId(id_str)
    except Exception:
        abort(404)

def _parse_date(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

def _now_utc():
    return datetime.now(timezone.utc)

def _serialize_expense(doc):
    """Convert Mongo doc to template-friendly dict and format date."""
    d = dict(doc)
    d["id"] = str(d.pop("_id"))
    if d.get("receipt_id"):
        d["receipt_id"] = str(d["receipt_id"])
    # Ensure keys expected by templates exist
    d["vendor"] = d.get("vendor", "")
    d["category"] = d.get("category", "")
    d["notes"] = d.get("notes", "")
    # Format date for <input type="date"> and table display
    if isinstance(d.get("date"), datetime):
        d["date"] = d["date"].strftime("%Y-%m-%d")
    return d

def _save_receipt(file_storage):
    """Save uploaded file to GridFS; return ObjectId or None."""
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    if not allowed_file(file_storage.filename):
        return None
    filename = secure_filename(file_storage.filename)
    content = file_storage.read()
    return fs.put(
        content,
        filename=filename,
        content_type=file_storage.mimetype or "application/octet-stream",
        uploadDate=_now_utc(),
    )

def _delete_receipt(receipt_id):
    if not receipt_id:
        return
    try:
        fs.delete(receipt_id)
    except Exception:
        pass

def require_login(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

# ------------------------------
# Auth routes
# ------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == APP_USERNAME and p == APP_PASSWORD:
            session["authed"] = True
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Invalid credentials.", "error")
        return redirect(url_for("login"))
    return render_template_string(LOGIN_TEMPLATE, title=APP_TITLE, css=BASE_CSS)

@app.route("/logout")
@require_login
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------------------
# Dashboard
# ------------------------------
@app.route("/")
@app.route("/dashboard")
@require_login
def index():
    # Total spend
    total_doc = list(expenses_col.aggregate([
        {"$group": {"_id": None, "sum": {"$sum": "$amount"}}}
    ]))
    total = total_doc[0]["sum"] if total_doc else 0.0

    # This month spend
    now = _now_utc()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    tm_doc = list(expenses_col.aggregate([
        {"$match": {"date": {"$gte": month_start, "$lt": next_month}}},
        {"$group": {"_id": None, "sum": {"$sum": "$amount"}}}
    ]))
    this_month = tm_doc[0]["sum"] if tm_doc else 0.0

    # Top 5 categories by spend
    top_cats = list(expenses_col.aggregate([
        {"$group": {"_id": "$category", "sum": {"$sum": "$amount"}}},
        {"$sort": {"sum": -1}},
        {"$limit": 5}
    ]))
    top_categories = [{"category": (x["_id"] or "Uncategorized"), "sum": x["sum"]} for x in top_cats]

    # Recent 10
    recent = expenses_col.find({}).sort("date", DESCENDING).limit(10)
    recent_rows = [_serialize_expense(d) for d in recent]

    return render_template_string(
        DASHBOARD_TEMPLATE,
        title=APP_TITLE,
        css=BASE_CSS,
        total=total,
        this_month=this_month,
        top_categories=top_categories,
        recent=recent_rows
    )

# ------------------------------
# Expenses list + filters
# ------------------------------
@app.route("/expenses")
@require_login
def expenses():
    start = request.args.get("start") or ""
    end = request.args.get("end") or ""
    category = request.args.get("category") or ""
    search = request.args.get("search") or ""

    q = {}
    if category:
        q["category"] = category

    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    if start_dt or end_dt:
        q["date"] = {}
        if start_dt:
            q["date"]["$gte"] = start_dt
        if end_dt:
            q["date"]["$lt"] = (end_dt + timedelta(days=1))  # inclusive to end-of-day

    if search:
        q["$or"] = [
            {"vendor": {"$regex": search, "$options": "i"}},
            {"notes": {"$regex": search, "$options": "i"}},
        ]

    cursor = expenses_col.find(q).sort("date", DESCENDING)
    rows = [_serialize_expense(d) for d in cursor]

    # filtered total
    ftotal_doc = list(expenses_col.aggregate([
        {"$match": q},
        {"$group": {"_id": None, "sum": {"$sum": "$amount"}}}
    ]))
    filtered_total = ftotal_doc[0]["sum"] if ftotal_doc else 0.0

    # category list for dropdown
    cats = expenses_col.distinct("category")
    cats = sorted([c for c in cats if c], key=lambda x: x.lower())

    filters = {"start": start, "end": end, "category": category, "search": search}

    return render_template_string(
        INDEX_TEMPLATE,
        title=APP_TITLE,
        css=BASE_CSS,
        rows=rows,
        total=filtered_total,
        categories=cats,
        filters=filters
    )

# ------------------------------
# Add expense
# ------------------------------
@app.route("/add", methods=["GET", "POST"])
@require_login
def add():
    if request.method == "POST":
        date_str = request.form.get("date")
        vendor = (request.form.get("vendor") or "").strip()
        category = (request.form.get("category") or "").strip()
        amount = (request.form.get("amount") or "").strip()
        notes = (request.form.get("notes") or "").strip()

        try:
            amount_val = float(amount)
        except ValueError:
            flash("Amount must be a number.", "error")
            return redirect(url_for("add"))

        date_dt = _parse_date(date_str) or _now_utc()

        # Receipt to GridFS
        receipt_file = request.files.get("receipt")
        receipt_oid = _save_receipt(receipt_file)

        doc = {
            "date": date_dt,
            "vendor": vendor,
            "category": category,
            "amount": amount_val,
            "notes": notes,
            "created_at": _now_utc(),
            "updated_at": _now_utc(),
        }
        if receipt_oid:
            doc["receipt_id"] = receipt_oid

        expenses_col.insert_one(doc)
        flash("Expense added.", "success")
        return redirect(url_for("expenses"))

    return render_template_string(ADD_TEMPLATE, title=APP_TITLE, css=BASE_CSS)

# ------------------------------
# Edit expense
# ------------------------------
@app.route("/edit/<expense_id>", methods=["GET", "POST"])
@require_login
def edit(expense_id):
    oid = _oid(expense_id)
    doc = expenses_col.find_one({"_id": oid})
    if not doc:
        abort(404)

    if request.method == "POST":
        date_str = request.form.get("date") or None
        vendor = (request.form.get("vendor") or doc.get("vendor") or "").strip()
        category = (request.form.get("category") or doc.get("category") or "").strip()
        amount = (request.form.get("amount") or doc.get("amount") or "").strip()
        notes = (request.form.get("notes") or doc.get("notes") or "").strip()

        try:
            amount_val = float(amount)
        except ValueError:
            flash("Amount must be a number.", "error")
            return redirect(url_for("edit", expense_id=expense_id))

        date_dt = _parse_date(date_str) if date_str else (doc.get("date") or _now_utc())

        set_fields = {
            "date": date_dt,
            "vendor": vendor,
            "category": category,
            "amount": amount_val,
            "notes": notes,
            "updated_at": _now_utc(),
        }

        # Optional receipt replacement
        receipt_file = request.files.get("receipt")
        if receipt_file and receipt_file.filename:
            new_oid = _save_receipt(receipt_file)
            if doc.get("receipt_id"):
                _delete_receipt(doc["receipt_id"])
            set_fields["receipt_id"] = new_oid

        expenses_col.update_one({"_id": oid}, {"$set": set_fields})
        flash("Expense updated.", "success")
        return redirect(url_for("expenses"))

    return render_template_string(EDIT_TEMPLATE, title=APP_TITLE, css=BASE_CSS, row=_serialize_expense(doc))

# ------------------------------
# Delete expense
# ------------------------------
@app.route("/delete/<expense_id>", methods=["POST"])
@require_login
def delete(expense_id):
    oid = _oid(expense_id)
    doc = expenses_col.find_one({"_id": oid})
    if not doc:
        abort(404)

    if doc.get("receipt_id"):
        _delete_receipt(doc["receipt_id"])

    expenses_col.delete_one({"_id": oid})
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses"))

# ------------------------------
# Stream receipt from GridFS
# ------------------------------
@app.route("/receipts/<id>")
@require_login
def receipts(id):
    oid = _oid(id)
    try:
        gfile = fs.get(oid)
    except Exception:
        abort(404)
    data = gfile.read()
    return send_file(
        BytesIO(data),
        mimetype=(getattr(gfile, "content_type", None) or "application/octet-stream"),
        download_name=(getattr(gfile, "filename", None) or f"receipt_{id}"),
        as_attachment=False,
        max_age=0,
        conditional=False
    )

# ------------------------------
# CSV export
# ------------------------------
@app.route("/export.csv")
@require_login
def export_csv():
    # Same filters as /expenses
    start = request.args.get("start") or ""
    end = request.args.get("end") or ""
    category = request.args.get("category") or ""
    search = request.args.get("search") or ""

    q = {}
    if category:
        q["category"] = category

    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    if start_dt or end_dt:
        q["date"] = {}
        if start_dt:
            q["date"]["$gte"] = start_dt
        if end_dt:
            q["date"]["$lt"] = (end_dt + timedelta(days=1))

    if search:
        q["$or"] = [
            {"vendor": {"$regex": search, "$options": "i"}},
            {"notes": {"$regex": search, "$options": "i"}},
        ]

    cursor = expenses_col.find(q).sort("date", DESCENDING)

    def generate():
        yield "id,date,vendor,category,amount,notes,receipt_id,created_at\n"
        for d in cursor:
            created = d.get("created_at")
            created_s = created.isoformat() if isinstance(created, datetime) else (created or "")
            date_s = d.get("date").strftime("%Y-%m-%d") if isinstance(d.get("date"), datetime) else (d.get("date") or "")
            row = [
                str(d["_id"]),
                date_s,
                d.get("vendor", "") or "",
                d.get("category", "") or "",
                f'{d.get("amount", 0):.2f}',
                (d.get("notes", "").replace("\n", " ").replace(",", " ")),
                (str(d.get("receipt_id")) if d.get("receipt_id") else ""),
                created_s,
            ]
            yield ",".join(row) + "\n"

    headers = {
        "Content-Disposition": "attachment; filename=expenses_export.csv",
        "Content-Type": "text/csv; charset=utf-8",
    }
    return Response(generate(), headers=headers)

# ------------------------------
# Inline CSS / Templates
# ------------------------------
BASE_CSS = """
:root { --bg:#0b1020; --card:#131a2e; --muted:#9fb0d3; --accent:#6ea8fe; }
*{ box-sizing:border-box; }
body{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; background:var(--bg); color:#e6eefc; }
.container{ max-width:1100px; margin:40px auto; padding:0 16px; }
a{ color:var(--accent); text-decoration:none; }
nav{ display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }
.header-title{ font-weight:800; font-size:20px; letter-spacing:0.3px; }
.card{ background:var(--card); border:1px solid #1e2743; border-radius:14px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,0.25); }
.btn{ display:inline-block; padding:10px 14px; border-radius:10px; border:1px solid #304377; background:#203057; color:#e6eefc; cursor:pointer; }
.btn.primary{ background:var(--accent); color:#0b1020; border:0; }
.btn.danger{ background:#e55353; border:0; }
input,select,textarea{ width:100%; padding:10px 12px; background:#0f1530; color:#e6eefc; border:1px solid #2c3e70; border-radius:10px; }
label{ color:#bcd0f0; font-size:14px; }
.grid{ display:grid; gap:12px; }
.grid.cols-4{ grid-template-columns: repeat(4, 1fr); }
.grid.cols-2{ grid-template-columns: 1fr 1fr; }
.kpis{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:12px; margin-bottom:12px; }
.kpi{ background:#10183a; border:1px solid #22376f; border-radius:14px; padding:14px; }
.kpi .label{ color:#9fb0d3; font-size:13px; }
.kpi .value{ font-size:24px; font-weight:800; margin-top:6px; }
.table{ width:100%; border-collapse:collapse; }
.table th, table td{ border-bottom:1px solid #25345e; padding:10px; text-align:left; vertical-align:top; }
.table th{ color:#a8b9df; font-weight:600; }
.badge{ background:#17244a; border:1px solid #2a3e76; padding:4px 8px; border-radius:999px; font-size:12px; }
.flash{ margin:8px 0; padding:10px 12px; border-radius:8px; background:#132141; border:1px solid #234; }
.flash.error{ background:#3a1620; border-color:#6a1f31; }
.section-title{ color:#9fb0d3; font-size:13px; margin:4px 0 6px; text-transform:uppercase; letter-spacing:.08em; }
.footer{ color:#8aa0cf; font-size:12px; margin-top:16px; text-align:center; opacity:.85; }
.table-wrap{ overflow-x:auto; }
@media (max-width: 720px){
  .grid.cols-4{ grid-template-columns: 1fr 1fr; }
  .kpis{ grid-template-columns: 1fr; }
}
"""

BASE_TMPL = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{{ title }}</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="container">
    <nav>
      <div class="header-title">{{ title }}</div>
      <div>
        {% if session.get('authed') %}
        <a href="{{ url_for('index') }}" class="btn">Dashboard</a>
        <a href="{{ url_for('expenses') }}" class="btn">Expenses</a>
        <a href="{{ url_for('add') }}" class="btn primary">+ Add Expense</a>
        <a href="{{ url_for('logout') }}" class="btn">Logout</a>
        {% endif %}
      </div>
    </nav>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}
          <div class="flash {{cat}}">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="card">
      {% block content %}{% endblock %}
    </div>

    <div class="footer">Lightweight internal app. Max upload 25MB. Allowed: png, jpg, jpeg, pdf.</div>
  </div>
</body>
</html>
"""

# Provide base.html from memory so no templates/ folder is required
app.jinja_loader = DictLoader({"base.html": BASE_TMPL})

LOGIN_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{{ title }} – Login</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="container">
    <div class="card" style="max-width:420px; margin:60px auto;">
      <div class="header-title" style="margin-bottom:12px;">{{ title }} – Login</div>
      <form method="post" class="grid">
        <div>
            <label>Username</label>
            <input type="text" name="username" placeholder="Enter username" required/>
        </div>
        <div>
          <label>Password</label>
          <input type="password" name="password" placeholder="Enter shared password" required/>
        </div>
        <div>
          <button class="btn primary" type="submit">Login</button>
        </div>
      </form>
      <div class="footer">Set APP_USERNAME / APP_PASSWORD env vars.</div>
    </div>
  </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
{% extends 'base.html' %}
{% block content %}
  <div class="kpis">
    <div class="kpi">
      <div class="label">Total Spend</div>
      <div class="value">${{ '%.2f'|format(total or 0) }}</div>
    </div>
    <div class="kpi">
      <div class="label">This Month</div>
      <div class="value">${{ '%.2f'|format(this_month or 0) }}</div>
    </div>
    <div class="kpi">
      <div class="label">Top Category</div>
      <div class="value">
        {% if top_categories and top_categories[0] %}
          {{ top_categories[0]['category'] or '—' }} (${{ '%.2f'|format(top_categories[0]['sum']) }})
        {% else %}—{% endif %}
      </div>
    </div>
  </div>

  <div class="section-title">Top 5 Categories</div>
  <div style="display:grid; gap:8px; grid-template-columns:repeat(5,minmax(0,1fr));">
    {% for c in top_categories %}
      <div class="card" style="padding:10px;">
        <div style="color:#9fb0d3; font-size:12px;">{{ c['category'] or 'Uncategorized' }}</div>
        <div style="font-weight:700; margin-top:4px;">${{ '%.2f'|format(c['sum']) }}</div>
      </div>
    {% else %}
      <div style="grid-column:1/-1; color:#9fb0d3;">No data.</div>
    {% endfor %}
  </div>

  <div class="section-title" style="margin-top:14px;">Recent Expenses</div>
  <div class="table-wrap">
    <table class="table">
      <thead>
        <tr>
          <th>Date</th><th>Vendor</th><th>Category</th><th>Amount</th><th>Receipt</th><th>Notes</th>
        </tr>
      </thead>
      <tbody>
        {% for r in recent %}
        <tr>
          <td>{{ r['date'] }}</td>
          <td>{{ r['vendor'] or '-' }}</td>
          <td><span class="badge">{{ r['category'] or 'Uncategorized' }}</span></td>
          <td>${{ '%.2f'|format(r['amount']) }}</td>
          <td>
            {% if r['receipt_id'] %}
              <a href="{{ url_for('receipts', id=r['receipt_id']) }}" target="_blank">View</a>
            {% else %}-{% endif %}
          </td>
          <td>{{ r['notes'] or '' }}</td>
        </tr>
        {% else %}
        <tr><td colspan="6">No recent expenses.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
{% endblock %}
"""

INDEX_TEMPLATE = """
{% extends 'base.html' %}
{% block content %}
<form method="get" class="grid cols-4" style="margin-bottom:12px;">
  <div>
    <label>Start date</label>
    <input type="date" name="start" value="{{ filters.start }}"/>
  </div>
  <div>
    <label>End date</label>
    <input type="date" name="end" value="{{ filters.end }}"/>
  </div>
  <div>
    <label>Category</label>
    <select name="category">
      <option value="">All</option>
      {% for c in categories %}
      <option value="{{c}}" {% if filters.category==c %}selected{% endif %}>{{c}}</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label>Search</label>
    <input type="text" name="search" placeholder="Vendor or notes…" value="{{ filters.search }}"/>
  </div>
  <div>
    <button class="btn" style="margin-top:28px;">Apply</button>
    <a class="btn" style="margin-top:28px;" href="{{ url_for('expenses') }}">Reset</a>
  </div>
  <div></div><div></div>
  <div style="display:flex; justify-content:flex-end; align-items:end;">
    <a class="btn" href="{{ url_for('export_csv', **filters) }}">Export CSV</a>
  </div>
</form>

<div class="section-title">Filtered Total</div>
<div style="margin-bottom:10px; font-size:18px; font-weight:700;">
  ${{ '%.2f'|format(total or 0) }}
</div>

<div class="table-wrap">
  <table class="table">
  <thead>
      <tr>
      <th>Date</th>
      <th>Vendor</th>
      <th>Category</th>
      <th>Amount</th>
      <th>Receipt</th>
      <th>Notes</th>
      <th></th>
      </tr>
  </thead>
  <tbody>
      {% for r in rows %}
      <tr>
      <td>{{ r['date'] }}</td>
      <td>{{ r['vendor'] or '-' }}</td>
      <td><span class="badge">{{ r['category'] or 'Uncategorized' }}</span></td>
      <td>${{ '%.2f'|format(r['amount']) }}</td>
      <td>
          {% if r['receipt_id'] %}
          <a href="{{ url_for('receipts', id=r['receipt_id']) }}" target="_blank">View</a>
          {% else %}-{% endif %}
      </td>
      <td>{{ r['notes'] or '' }}</td>
      <td>
          <div style="display:flex; gap:6px; flex-wrap: wrap;">
              <a class="btn" href="{{ url_for('edit', expense_id=r['id']) }}">Edit</a>
              <form method="post" action="{{ url_for('delete', expense_id=r['id']) }}" onsubmit="return confirm('Delete this expense?');">
                <button class="btn danger">Delete</button>
              </form>
          </div>
      </td>
      </tr>
      {% else %}
      <tr><td colspan="7">No expenses found.</td></tr>
      {% endfor %}
  </tbody>
  </table>
</div>
{% endblock %}
"""

ADD_TEMPLATE = """
{% extends 'base.html' %}
{% block content %}
<h3>Add Expense</h3>
<form method="post" enctype="multipart/form-data" class="grid cols-2">
  <div>
    <label>Date</label>
    <input type="date" name="date" value="{{ (caller_date or '') }}" required/>
  </div>
  <div>
    <label>Amount (USD)</label>
    <input type="number" step="0.01" name="amount" required/>
  </div>
  <div>
    <label>Vendor</label>
    <input type="text" name="vendor" placeholder="Optional"/>
  </div>
  <div>
    <label>Category</label>
    <input type="text" name="category" placeholder="e.g., Meals, Travel, Tools"/>
  </div>
  <div class="grid cols-2">
    <div>
      <label>Receipt (png/jpg/jpeg/pdf)</label>
      <input type="file" name="receipt" accept="image/*,application/pdf"/>
    </div>
  </div>
  <div style="grid-column:1/-1;">
    <label>Notes</label>
    <textarea name="notes" rows="3" placeholder="Optional notes"></textarea>
  </div>
  <div style="grid-column:1/-1; display:flex; gap:8px; justify-content:flex-end;">
    <a class="btn" href="{{ url_for('expenses') }}">Cancel</a>
    <button class="btn primary" type="submit">Save</button>
  </div>
</form>
{% endblock %}
"""

EDIT_TEMPLATE = """
{% extends 'base.html' %}
{% block content %}
<h3>Edit Expense</h3>
<form method="post" enctype="multipart/form-data" class="grid cols-2">
  <div>
    <label>Date</label>
    <input type="date" name="date" value="{{ row['date'] }}" required/>
  </div>
  <div>
    <label>Amount (USD)</label>
    <input type="number" step="0.01" name="amount" value="{{ row['amount'] }}" required/>
  </div>
  <div>
    <label>Vendor</label>
    <input type="text" name="vendor" value="{{ row['vendor'] or '' }}"/>
  </div>
  <div>
    <label>Category</label>
    <input type="text" name="category" value="{{ row['category'] or '' }}"/>
  </div>
  <div class="grid cols-2">
    <div>
      <label>Receipt (upload new to replace)</label>
      <input type="file" name="receipt" accept="image/*,application/pdf"/>
      {% if row['receipt_id'] %}
        <div style="margin-top:6px;">
          <a href="{{ url_for('receipts', id=row['receipt_id']) }}" target="_blank">Current receipt</a>
        </div>
      {% endif %}
    </div>
  </div>
  <div style="grid-column:1/-1;">
    <label>Notes</label>
    <textarea name="notes" rows="3">{{ row['notes'] or '' }}</textarea>
  </div>
  <div style="grid-column:1/-1; display:flex; gap:8px; justify-content:flex-end;">
    <a class="btn" href="{{ url_for('expenses') }}">Cancel</a>
    <button class="btn primary" type="submit">Save Changes</button>
  </div>
</form>
{% endblock %}
"""

# ------------------------------
# Main
# ------------------------------
if __name__ == "__main__":
    # For local dev: flask run or python app.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
