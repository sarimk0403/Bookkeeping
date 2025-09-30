# Simple Bookkeeping Web App – with Admin Dashboard
# Stack: Flask (Python), SQLite (built-in), local file storage for receipts/images
# One-file app for quick internal use.
# ------------------------------------------------------------
# Quick start:
#   1) python3 -m venv .venv && source .venv/bin/activate
#   2) pip install flask python-dotenv
#   3) python app.py
#   4) Visit http://127.0.0.1:5000
# ------------------------------------------------------------

import os
import csv
import sqlite3
import secrets
from datetime import datetime, date
from werkzeug.utils import secure_filename
from flask import (
    Flask, request, redirect, url_for, render_template_string, send_from_directory,
    send_file, flash, session
)
from jinja2 import DictLoader

APP_TITLE = "Internal Bookkeeping"
DB_PATH = os.environ.get("DB_PATH", "expenses.db")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "pdf"}
# Optional: simple shared password for internal use
APP_USERNAME = os.environ.get("APP_USERNAME", "Sarimk0403")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "Sarimk@2003")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB uploads


# If you prefer not to create templates/base.html on disk,
# you can uncomment these two lines to provide it in-memory instead:
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


# Uncomment the next line if you are NOT creating templates/base.html on disk
# app.jinja_loader = DictLoader({"base.html": BASE_TMPL})

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
      <div class="footer">Set APP_PASSWORD env var to change the password.</div>
    </div>
  </div>
</body>
</html>
"""


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                vendor TEXT,
                category TEXT,
                amount REAL NOT NULL,
                notes TEXT,
                receipt_filename TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

init_db()

# ------------------------------
# Helpers
# ------------------------------

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def require_login(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


# ------------------------------
# Auth (very simple shared password)
# ------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pw = request.form.get("password", "")
        if user == APP_USERNAME and pw == APP_PASSWORD:
            session["authed"] = True
            session["user"] = user
            flash("Logged in.", "success")
            return redirect(request.args.get("next") or url_for("index"))
        else:
            flash("Incorrect username or password.", "error")
    return render_template_string(LOGIN_TEMPLATE, title=APP_TITLE)


@app.route("/logout")
@require_login
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


# ------------------------------
# Dashboard (now the default route)
# ------------------------------
@app.route("/")
@require_login
def index():
    # Date helpers for current month
    today = date.today()
    month_start = today.replace(day=1).strftime("%Y-%m-%d")

    with db_conn() as conn:
        total_all = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses").fetchone()[0] or 0
        total_month = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date(date) >= date(?)",
            (month_start,),
        ).fetchone()[0] or 0
        count_all = conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        by_cat = conn.execute(
            "SELECT COALESCE(category,'Uncategorized') AS category, SUM(amount) AS total\n"
            "FROM expenses GROUP BY COALESCE(category,'Uncategorized')\n"
            "ORDER BY total DESC LIMIT 6"
        ).fetchall()
        recent = conn.execute(
            "SELECT id,date,vendor,category,amount,receipt_filename FROM expenses\n"
            "ORDER BY date DESC, id DESC LIMIT 6"
        ).fetchall()

    return render_template_string(DASHBOARD_TEMPLATE,
        title=APP_TITLE,
        total_all=total_all,
        total_month=total_month,
        count_all=count_all,
        by_cat=by_cat,
        recent=recent,
    )

@app.route('/service-worker.js')
def sw(): return send_from_directory('static','service-worker.js')



# ------------------------------
# Expenses list (moved from index to /expenses)
# ------------------------------
@app.route("/expenses")
@require_login
def expenses():
    q = []
    params = []

    # Filters
    start = request.args.get("start")
    end = request.args.get("end")
    category = request.args.get("category")
    search = request.args.get("search")

    if start:
        q.append("date(date) >= date(?)")
        params.append(start)
    if end:
        q.append("date(date) <= date(?)")
        params.append(end)
    if category:
        q.append("category = ?")
        params.append(category)
    if search:
        q.append("(vendor LIKE ? OR notes LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where = ("WHERE " + " AND ".join(q)) if q else ""
    order = "ORDER BY date DESC, id DESC"

    with db_conn() as conn:
        rows = conn.execute(f"SELECT * FROM expenses {where} {order}", params).fetchall()
        total = conn.execute(f"SELECT COALESCE(SUM(amount),0) AS t FROM expenses {where}", params).fetchone()[0]
        cats = conn.execute("SELECT DISTINCT category FROM expenses ORDER BY category").fetchall()

    return render_template_string(INDEX_TEMPLATE, title=APP_TITLE, rows=rows, total=total, filters={
        "start": start or "",
        "end": end or "",
        "category": category or "",
        "search": search or "",
    }, categories=[c[0] for c in cats])


@app.route("/add", methods=["GET", "POST"])
@require_login
def add():
    if request.method == "POST":
        date_str = request.form.get("date") or datetime.now().strftime("%Y-%m-%d")
        vendor = request.form.get("vendor", "").strip()
        category = request.form.get("category", "").strip()
        amount = request.form.get("amount", "0").strip()
        notes = request.form.get("notes", "").strip()

        # Validate fields
        try:
            amount_val = float(amount)
        except ValueError:
            flash("Amount must be a number.", "error")
            return redirect(url_for("add"))

        filename = None
        file = request.files.get("receipt")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Invalid file type. Allowed: png, jpg, jpeg, pdf.", "error")
                return redirect(url_for("add"))
            safe = secure_filename(file.filename)
            filename = f"{int(datetime.now().timestamp())}_{safe}"
            file.save(os.path.join(UPLOAD_DIR, filename))

        with db_conn() as conn:
            conn.execute(
                "INSERT INTO expenses (date, vendor, category, amount, notes, receipt_filename) VALUES (?,?,?,?,?,?)",
                (date_str, vendor, category, float(amount_val), notes, filename),
            )
        flash("Expense added.", "success")
        return redirect(url_for("expenses"))

    return render_template_string(ADD_TEMPLATE, title=APP_TITLE, today=datetime.now().strftime("%Y-%m-%d"))



@app.route("/delete/<int:expense_id>", methods=["POST"]) 
@require_login
def delete(expense_id):
    with db_conn() as conn:
        row = conn.execute("SELECT receipt_filename FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if row:
            if row[0]:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, row[0]))
                except FileNotFoundError:
                    pass
            conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
            flash("Deleted.", "success")
        else:
            flash("Not found.", "error")
    return redirect(url_for("expenses"))


@app.route("/receipts/<path:filename>")
@require_login
def receipts(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/edit/<int:expense_id>", methods=["GET", "POST"])
@require_login
def edit(expense_id):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            flash("Expense not found.", "error")
            return redirect(url_for("expenses"))

    if request.method == "POST":
        date_str = request.form.get("date") or row["date"]
        vendor = (request.form.get("vendor") or "").strip()
        category = (request.form.get("category") or "").strip()
        amount = (request.form.get("amount") or "").strip()
        notes = (request.form.get("notes") or "").strip()

        try:
            amount_val = float(amount)
        except ValueError:
            flash("Amount must be a number.", "error")
            return redirect(url_for("edit", expense_id=expense_id))

        filename = row["receipt_filename"]
        file = request.files.get("receipt")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Invalid file type.", "error")
                return redirect(url_for("edit", expense_id=expense_id))
            safe = secure_filename(file.filename)
            filename = f"{int(datetime.now().timestamp())}_{safe}"
            file.save(os.path.join(UPLOAD_DIR, filename))

        with db_conn() as conn:
            conn.execute(
                "UPDATE expenses SET date=?, vendor=?, category=?, amount=?, notes=?, receipt_filename=? WHERE id=?",
                (date_str, vendor, category, amount_val, notes, filename, expense_id),
            )
        flash("Expense updated.", "success")
        return redirect(url_for("expenses"))

    return render_template_string(EDIT_TEMPLATE, title=APP_TITLE, row=row)


@app.route("/export.csv")
@require_login
def export_csv():
    # Apply same filters as expenses list
    q = []
    params = []
    start = request.args.get("start")
    end = request.args.get("end")
    category = request.args.get("category")
    search = request.args.get("search")

    if start:
        q.append("date(date) >= date(?)")
        params.append(start)
    if end:
        q.append("date(date) <= date(?)")
        params.append(end)
    if category:
        q.append("category = ?")
        params.append(category)
    if search:
        q.append("(vendor LIKE ? OR notes LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where = ("WHERE " + " AND ".join(q)) if q else ""

    with db_conn() as conn:
        rows = conn.execute(f"SELECT date,vendor,category,amount,notes,receipt_filename,created_at FROM expenses {where} ORDER BY date DESC", params).fetchall()

    # Create CSV in-memory
    tmp = os.path.join("/tmp", f"export_{int(datetime.now().timestamp())}.csv")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date","vendor","category","amount","notes","receipt","created_at"])
        for r in rows:
            writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5] or "", r[6]])

    return send_file(tmp, as_attachment=True, download_name="expenses_export.csv")


# ------------------------------
# HTML Templates (inline for single-file simplicity)
# ------------------------------
BASE_CSS = """
:root { --bg:#0b1020; --card:#131a2e; --muted:#9fb0d3; --accent:#6ea8fe; }
*{ box-sizing:border-box; }
body{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; background:var(--bg); color:#e6eefc; }
.container{ max-width:1100px; margin:40px auto; padding:0 16px; }
a{ color:var(--accent); text-decoration:none; }
nav{ display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }
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
.table th,.table td{ border-bottom:1px solid #25345e; padding:10px; text-align:left; }
.table th{ color:#a8b9df; font-weight:600; }
.badge{ padding:4px 8px; background:#1e2a52; border-radius:999px; border:1px solid #2f447f; font-size:12px; color:#bcd0f0; }
.flash{ padding:10px 12px; border-radius:10px; margin-bottom:12px; }
.flash.success{ background:#16351e; border:1px solid #2f7f4a; }
.flash.error{ background:#3a1717; border:1px solid #9b2b2b; }
.footer{ margin-top:16px; color:#9fb0d3; font-size:13px; }
.header-title{ font-size:20px; font-weight:700; }
.section-title{ font-weight:700; margin:10px 0; }
.actions{ display:flex; gap:8px; }
/* --- Mobile tweaks --- */
@media (max-width: 768px) {
  .container { padding: 0 12px; }
  .grid { gap: 10px; }
  .grid.cols-4, .grid.cols-2 { grid-template-columns: 1fr; } /* filters & forms stack */
  nav { flex-direction: column; align-items: flex-start; gap: 10px; }
  .btn { width: 100%; min-height: 44px; } /* big, easy tap targets */
  input, select, textarea { font-size: 16px; } /* avoid iOS zoom on focus */
  .card { padding: 12px; }
  .kpis { grid-template-columns: 1fr; } /* dashboard tiles stack */
  .header-title { font-size: 18px; }
  .table th, .table td { padding: 8px; font-size: 14px; }
}

/* Keep navbar visible when scrolling on mobile */
nav { position: sticky; top: 0; z-index: 20; background: var(--bg); padding-bottom: 8px; }

/* Make tables scroll instead of overflowing the screen */
.table-wrap { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.table { min-width: 720px; } /* ensures header/columns keep shape while scrolling */

"""

DASHBOARD_TEMPLATE = """
{% extends 'base.html' %}
{% block content %}
<div class="kpis">
  <div class="kpi"><div class="label">Total Spent (All Time)</div><div class="value">${{ '%.2f'|format(total_all) }}</div></div>
  <div class="kpi"><div class="label">Total Spent (This Month)</div><div class="value">${{ '%.2f'|format(total_month) }}</div></div>
  <div class="kpi"><div class="label"># of Expenses</div><div class="value">{{ count_all }}</div></div>
</div>

<div class="grid cols-2">
  <div>
    <div class="section-title">Top Categories</div>
        <div class="table-wrap">
        <table class="table">
        <thead><tr><th>Category</th><th>Total</th></tr></thead>
        <tbody>
            {% for r in by_cat %}
            <tr><td><span class="badge">{{ r['category'] }}</span></td><td>${{ '%.2f'|format(r['total']) }}</td></tr>
            {% else %}
            <tr><td colspan="2">No data yet.</td></tr>
            {% endfor %}
        </tbody>
        </table>
    </div>
  </div>
  <div>
    <div class="section-title">Recent Expenses</div>
    <div class="table-wrap">
        <table class="table">
        <thead><tr><th>Date</th><th>Vendor</th><th>Category</th><th>Amount</th></tr></thead>
        <tbody>
            {% for r in recent %}
            <tr>
                <td>{{ r['date'] }}</td>
                <td>{{ r['vendor'] or '-' }}</td>
                <td><span class="badge">{{ r['category'] or 'Uncategorized' }}</span></td>
                <td>${{ '%.2f'|format(r['amount']) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="4">No expenses yet.</td></tr>
            {% endfor %}
        </tbody>
        </table>
    </div>
  </div>
</div>

<div class="actions" style="margin-top:12px;">
  <a class="btn primary" href="{{ url_for('add') }}">+ Add Expense</a>
  <a class="btn" href="{{ url_for('expenses') }}">View All Expenses</a>
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
<div style="margin-bottom:10px; font-size:18px; font-weight:700;">${{ '%.2f'|format(total or 0) }}</div>
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
            {% if r['receipt_filename'] %}
            <a href="{{ url_for('receipts', filename=r['receipt_filename']) }}" target="_blank">View</a>
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
      {% if row['receipt_filename'] %}
        <div style="margin-top:6px;">
          <a href="{{ url_for('receipts', filename=row['receipt_filename']) }}" target="_blank">Current receipt</a>
        </div>
      {% endif %}
    </div>
  </div>
  <div style="grid-column:1/-1;">
    <label>Notes</label>
    <textarea name="notes" rows="3">{{ row['notes'] or '' }}</textarea>
  </div>
  <div style="grid-column:1/-1; display:flex; gap:8px;">
    <button class="btn primary" type="submit">Save</button>
    <a class="btn" href="{{ url_for('expenses') }}">Cancel</a>
  </div>
</form>
{% endblock %}
"""


ADD_TEMPLATE = """
{% extends 'base.html' %}
{% block content %}
<form method="post" enctype="multipart/form-data" class="grid cols-2">
  <div>
    <label>Date</label>
    <input type="date" name="date" value="{{ today }}" required/>
  </div>
  <div>
    <label>Amount (USD)</label>
    <input type="number" step="0.01" name="amount" placeholder="0.00" required/>
  </div>
  <div>
    <label>Vendor</label>
    <input type="text" name="vendor" placeholder="Who did we pay?"/>
  </div>
  <div>
    <label>Category</label>
    <input type="text" name="category" placeholder="e.g., Software, Travel, Supplies"/>
  </div>
  <div class="grid cols-2">
    <div>
      <label>Receipt (png/jpg/jpeg/pdf)</label>
      <input type="file" name="receipt" accept="image/*,application/pdf"/>
    </div>
  </div>
  <div style="grid-column:1/-1;">
    <label>Notes</label>
    <textarea name="notes" rows="3" placeholder="Optional details"></textarea>
  </div>
  <div style="grid-column:1/-1; display:flex; gap:8px;">
    <button class="btn primary" type="submit">Save</button>
    <a class="btn" href="{{ url_for('expenses') }}">Cancel</a>
  </div>
</form>
{% endblock %}
"""

# Jinja context for CSS
@app.context_processor
def inject_base():
    return {"css": BASE_CSS}


if __name__ == "__main__":
    print(f"\n{APP_TITLE} running…")
    print("Env vars: DB_PATH, UPLOAD_DIR, APP_PASSWORD, SECRET_KEY")
    app.run(debug=True)