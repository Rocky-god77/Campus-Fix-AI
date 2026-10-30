from flask import Flask, render_template, request, redirect, url_for, flash
from pathlib import Path
from datetime import datetime
import sqlite3
import re
import os
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "campusfix.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_FILE_SIZE = 5 * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get("CAMPUSFIX_SECRET_KEY", "dev-only-change-this-key")
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

CATEGORIES = [
    "Electrical", "Plumbing", "Classroom Equipment", "Internet",
    "Cleanliness", "Furniture", "Safety", "Other"
]
STATUSES = ["Reported", "Assigned", "In Progress", "Resolved", "Closed"]
PRIORITIES = ["Low", "Medium", "High"]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            location TEXT NOT NULL,
            priority TEXT NOT NULL,
            priority_reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Reported',
            assigned_to TEXT DEFAULT '',
            image_path TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolution_note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS complaint_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
        );
        """)

def classify_complaint(title, description, location):
    text = f"{title} {description}".lower()
    if any(word in text for word in ["fire", "smoke", "shock", "sparking", "exposed wire", "gas leak", "danger", "injury"]):
        return "Safety", "High", "Possible immediate safety risk detected. Staff should review promptly."
    category_rules = [
        ("Electrical", ["fan", "light", "electric", "switch", "socket", "power", "wire", "bulb"]),
        ("Plumbing", ["water leak", "leakage", "tap", "pipe", "toilet", "drain", "flush", "flood"]),
        ("Classroom Equipment", ["projector", "computer", "screen", "speaker", "smart board", "lab equipment"]),
        ("Internet", ["wifi", "wi-fi", "internet", "network", "router", "connection"]),
        ("Cleanliness", ["dirty", "garbage", "trash", "clean", "dust", "smell", "waste"]),
        ("Furniture", ["desk", "chair", "bench", "table", "door", "window", "broken handle"]),
        ("Safety", ["unsafe", "slippery", "broken glass", "blocked exit"])
    ]
    category = "Other"
    for cat, words in category_rules:
        if any(word in text for word in words):
            category = cat
            break
    if category == "Safety":
        priority, reason = "High", "The description contains a possible safety-related keyword."
    elif category in ("Electrical", "Plumbing") and any(word in text for word in ["not working", "broken", "leak", "leaking", "sparking"]):
        priority, reason = "Medium", "This may affect campus facilities and should be reviewed by staff."
    elif any(word in text for word in ["urgent", "immediately", "severe", "large leak", "no water"]):
        priority, reason = "High", "Urgency-related wording was detected."
    else:
        priority, reason = "Medium", "Default priority suggestion; staff can change it after review."
    return category, priority, reason

def normalized_words(value):
    return set(re.findall(r"[a-z0-9]+", (value or "").lower()))

def similarity_score(a, b):
    wa, wb = normalized_words(a), normalized_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def find_possible_duplicates(title, description, location):
    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, description, location, status FROM complaints ORDER BY id DESC LIMIT 200"
        ).fetchall()
    new_text = f"{title} {description}"
    matches = []
    for row in rows:
        same_location = (row["location"] or "").strip().lower() == (location or "").strip().lower()
        score = similarity_score(new_text, f'{row["title"]} {row["description"]}')
        if same_location and score >= 0.28:
            matches.append({"id": row["id"], "title": row["title"], "score": round(score * 100), "status": row["status"]})
    return matches[:3]

@app.route("/")
def index():
    with get_db() as db:
        totals = db.execute("""
            SELECT COUNT(*) AS total,
            SUM(CASE WHEN status NOT IN ('Resolved','Closed') THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN priority='High' AND status NOT IN ('Resolved','Closed') THEN 1 ELSE 0 END) AS high_count,
            SUM(CASE WHEN status='Resolved' THEN 1 ELSE 0 END) AS resolved_count
            FROM complaints
        """).fetchone()
        recent = db.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 5").fetchall()
        category_rows = db.execute("SELECT category, COUNT(*) AS count FROM complaints GROUP BY category ORDER BY count DESC").fetchall()
        status_rows = db.execute("SELECT status, COUNT(*) AS count FROM complaints GROUP BY status").fetchall()
    return render_template("index.html", totals=totals, recent=recent,
                           category_rows=category_rows, status_rows=status_rows)

@app.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "POST":
        student_name = request.form.get("student_name", "").strip() or "Anonymous"
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        location = request.form.get("location", "").strip()
        if not title or not description or not location:
            flash("Please fill in the complaint title, description, and location.", "error")
            return redirect(url_for("report"))
        if len(title) > 120 or len(description) > 2000 or len(location) > 120:
            flash("Please shorten one or more fields and try again.", "error")
            return redirect(url_for("report"))

        category, priority, reason = classify_complaint(title, description, location)
        image_path = ""
        uploaded = request.files.get("photo")
        if uploaded and uploaded.filename:
            filename = secure_filename(uploaded.filename)
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in ALLOWED_EXTENSIONS:
                flash("Photo must be PNG, JPG, JPEG, or WEBP.", "error")
                return redirect(url_for("report"))
            unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{filename}"
            uploaded.save(UPLOAD_DIR / unique_name)
            image_path = f"uploads/{unique_name}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with get_db() as db:
            cur = db.execute("""
                INSERT INTO complaints
                (student_name,title,description,category,location,priority,priority_reason,status,assigned_to,image_path,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'Reported','',?,?,?)
            """, (student_name, title, description, category, location, priority, reason, image_path, now, now))
            complaint_id = cur.lastrowid
            db.execute("""INSERT INTO complaint_updates
                (complaint_id,old_status,new_status,note,created_at)
                VALUES (?,?,?,?,?)""", (complaint_id, "New", "Reported", "Complaint submitted.", now))
        duplicates = find_possible_duplicates(title, description, location)
        if duplicates:
            flash(f"Complaint #{complaint_id} submitted. We found {len(duplicates)} possible similar report(s); staff can review them.", "success")
        else:
            flash(f"Complaint #{complaint_id} submitted successfully. Suggested category: {category}; priority: {priority}.", "success")
        return redirect(url_for("complaint_detail", complaint_id=complaint_id))
    return render_template("report.html", categories=CATEGORIES)

@app.route("/complaints")
def complaints():
    status = request.args.get("status", "").strip()
    category = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    query = "SELECT * FROM complaints WHERE 1=1"
    params = []
    if status in STATUSES:
        query += " AND status=?"
        params.append(status)
    if category in CATEGORIES:
        query += " AND category=?"
        params.append(category)
    if search:
        query += " AND (title LIKE ? OR description LIKE ? OR location LIKE ? OR CAST(id AS TEXT)=?)"
        params.extend([f"%{search}%"] * 3 + [search])
    query += " ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, id DESC"
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return render_template("complaints.html", complaints=rows, statuses=STATUSES, categories=CATEGORIES,
                           selected_status=status, selected_category=category, q=search)

@app.route("/complaint/<int:complaint_id>")
def complaint_detail(complaint_id):
    with get_db() as db:
        item = db.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        updates = db.execute("SELECT * FROM complaint_updates WHERE complaint_id=? ORDER BY id DESC", (complaint_id,)).fetchall()
        others = db.execute("SELECT id,title,status FROM complaints WHERE id<>? AND lower(location)=lower(?) ORDER BY id DESC LIMIT 100",
                            (complaint_id, item["location"] if item else "")).fetchall() if item else []
    if not item:
        flash("Complaint not found.", "error")
        return redirect(url_for("complaints"))
    similar = []
    for other in others:
        with get_db() as db:
            other_full = db.execute("SELECT title,description FROM complaints WHERE id=?", (other["id"],)).fetchone()
        score = similarity_score(f'{item["title"]} {item["description"]}', f'{other_full["title"]} {other_full["description"]}')
        if score >= 0.28:
            similar.append({"id": other["id"], "title": other["title"], "status": other["status"], "score": round(score*100)})
    return render_template("detail.html", item=item, updates=updates, similar=similar[:5], statuses=STATUSES, priorities=PRIORITIES)

@app.route("/complaint/<int:complaint_id>/update", methods=["POST"])
def update_complaint(complaint_id):
    status = request.form.get("status", "")
    priority = request.form.get("priority", "")
    assigned_to = request.form.get("assigned_to", "").strip()[:100]
    note = request.form.get("note", "").strip()[:1000]
    if status not in STATUSES or priority not in PRIORITIES:
        flash("Invalid status or priority.", "error")
        return redirect(url_for("complaint_detail", complaint_id=complaint_id))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db() as db:
        item = db.execute("SELECT status FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not item:
            flash("Complaint not found.", "error")
            return redirect(url_for("complaints"))
        db.execute("""UPDATE complaints SET status=?, priority=?, assigned_to=?, resolution_note=?, updated_at=? WHERE id=?""",
                   (status, priority, assigned_to, note, now, complaint_id))
        if item["status"] != status or note:
            db.execute("""INSERT INTO complaint_updates (complaint_id,old_status,new_status,note,created_at)
                          VALUES (?,?,?,?,?)""", (complaint_id, item["status"], status, note or "Complaint details updated.", now))
    flash("Complaint updated.", "success")
    return redirect(url_for("complaint_detail", complaint_id=complaint_id))

@app.route("/about")
def about():
    return render_template("about.html")

@app.errorhandler(413)
def too_large(_error):
    flash("The uploaded file is too large. Maximum photo size is 5 MB.", "error")
    return redirect(url_for("report"))

init_db()

if __name__ == "__main__":
    app.run(debug=True)
