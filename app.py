"""
Reel Deal — Fishing Competition backend.

A small Flask + SQLite API for posting fishing competitions, signing up
anglers, submitting measured catch photos, and announcing winners.

Designed to run standalone as an API (e.g. on Render, Railway, or
PythonAnywhere) while a separate static frontend (e.g. on Neocities) talks
to it over HTTP with CORS enabled.

Local run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000

Production run (what Render/Railway use, via the Procfile):
    gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4

Environment variables (all optional):
    PORT           Port to listen on. Cloud platforms set this for you.
    DATA_DIR       Where the SQLite DB + uploaded photos live. Point this at
                    a persistent disk/volume in production, or the data is
                    lost whenever the service restarts or redeploys.
    CORS_ORIGINS   Comma-separated list of allowed frontend origins, e.g.
                    "https://yourname.neocities.org". Defaults to "*" (any
                    origin) which is fine for testing but not once this is
                    a real, public site.
    FLASK_DEBUG    Set to "1" to enable Flask's debug/reload mode locally.
"""
import os
import sqlite3
import uuid
from datetime import datetime, date, timezone

from flask import Flask, request, jsonify, send_from_directory, g, abort
from flask_cors import CORS
from werkzeug.utils import secure_filename
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, "reeldeal.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_IMAGE_DIM = 1200  # px, longest side after resize

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")

_origins_env = os.environ.get("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = "*" if _origins_env == "*" else [o.strip() for o in _origins_env.split(",") if o.strip()]
# supports_credentials is off because the app uses no cookies/sessions —
# anglers are identified by a plain name field, so a wildcard origin is safe
# here as long as you don't later add cookie-based auth.
CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}, r"/uploads/*": {"origins": CORS_ORIGINS}})

app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB request cap


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        # timeout lets concurrent requests wait briefly instead of throwing
        # "database is locked" if two writes land at the same moment
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS competitions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            species TEXT,
            water_body TEXT,
            description TEXT,
            rules TEXT,
            organizer TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            ref_label TEXT,
            ref_unit TEXT NOT NULL DEFAULT 'in',
            created_at TEXT NOT NULL,
            winner_participant TEXT,
            winner_length REAL,
            announcement_message TEXT,
            announcement_posted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id TEXT NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            UNIQUE(competition_id, name COLLATE NOCASE)
        );

        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            competition_id TEXT NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
            participant TEXT NOT NULL,
            image_filename TEXT NOT NULL,
            length REAL NOT NULL,
            submitted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id TEXT NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
            recipient TEXT NOT NULL,
            message TEXT NOT NULL,
            sent_at TEXT NOT NULL
        );
        """
    )
    db.commit()
    db.close()


# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def row_to_competition_summary(row):
    db = get_db()
    pcount = db.execute(
        "SELECT COUNT(*) c FROM participants WHERE competition_id=?", (row["id"],)
    ).fetchone()["c"]
    ecount = db.execute(
        "SELECT COUNT(*) c FROM entries WHERE competition_id=?", (row["id"],)
    ).fetchone()["c"]
    return {
        "id": row["id"],
        "title": row["title"],
        "species": row["species"],
        "waterBody": row["water_body"],
        "startDate": row["start_date"],
        "endDate": row["end_date"],
        "status": row["status"],
        "unit": row["ref_unit"],
        "participantCount": pcount,
        "entryCount": ecount,
    }


def row_to_competition_detail(row):
    db = get_db()
    participants = [
        {"name": r["name"], "joinedAt": r["joined_at"]}
        for r in db.execute(
            "SELECT name, joined_at FROM participants WHERE competition_id=? ORDER BY joined_at",
            (row["id"],),
        ).fetchall()
    ]
    entries = [
        {
            "id": r["id"],
            "participant": r["participant"],
            "imageUrl": f"/uploads/{r['image_filename']}",
            "length": r["length"],
            "submittedAt": r["submitted_at"],
        }
        for r in db.execute(
            "SELECT * FROM entries WHERE competition_id=? ORDER BY length DESC",
            (row["id"],),
        ).fetchall()
    ]
    announcement = None
    if row["winner_participant"]:
        announcement = {
            "winner": row["winner_participant"],
            "length": row["winner_length"],
            "message": row["announcement_message"],
            "postedAt": row["announcement_posted_at"],
        }
    return {
        "id": row["id"],
        "title": row["title"],
        "species": row["species"],
        "waterBody": row["water_body"],
        "description": row["description"],
        "rules": row["rules"],
        "organizer": row["organizer"],
        "startDate": row["start_date"],
        "endDate": row["end_date"],
        "status": row["status"],
        "refLength": {"label": row["ref_label"], "unit": row["ref_unit"]},
        "createdAt": row["created_at"],
        "participants": participants,
        "entries": entries,
        "announcement": announcement,
    }


def get_competition_row(comp_id):
    db = get_db()
    row = db.execute("SELECT * FROM competitions WHERE id=?", (comp_id,)).fetchone()
    if row is None:
        abort(404, description="Competition not found")
    return row


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ---------------------------------------------------------------- routes: frontend

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------- routes: API

@app.get("/api/competitions")
def list_competitions():
    db = get_db()
    rows = db.execute("SELECT * FROM competitions ORDER BY created_at DESC").fetchall()
    return jsonify([row_to_competition_summary(r) for r in rows])


@app.post("/api/competitions")
def create_competition():
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    organizer = (data.get("organizer") or "").strip()
    end_date = (data.get("endDate") or "").strip()
    if not title or not organizer or not end_date:
        return jsonify({"error": "title, organizer, and endDate are required"}), 400

    comp_id = uuid.uuid4().hex[:12]
    db = get_db()
    db.execute(
        """INSERT INTO competitions
           (id, title, species, water_body, description, rules, organizer,
            start_date, end_date, status, ref_label, ref_unit, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            comp_id,
            title,
            (data.get("species") or "").strip(),
            (data.get("waterBody") or "").strip(),
            (data.get("description") or "").strip(),
            (data.get("rules") or "").strip(),
            organizer,
            (data.get("startDate") or date.today().isoformat()).strip(),
            end_date,
            "open",
            (data.get("refLabel") or "reference length").strip(),
            data.get("refUnit") if data.get("refUnit") in ("in", "cm") else "in",
            now_iso(),
        ),
    )
    db.commit()
    row = get_competition_row(comp_id)
    return jsonify(row_to_competition_detail(row)), 201


@app.get("/api/competitions/<comp_id>")
def get_competition(comp_id):
    row = get_competition_row(comp_id)
    return jsonify(row_to_competition_detail(row))


@app.post("/api/competitions/<comp_id>/join")
def join_competition(comp_id):
    row = get_competition_row(comp_id)
    if row["status"] != "open":
        return jsonify({"error": "This competition has ended."}), 400
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO participants (competition_id, name, joined_at) VALUES (?,?,?)",
            (comp_id, name, now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        pass  # already joined — treat as idempotent
    return jsonify(row_to_competition_detail(get_competition_row(comp_id)))


@app.post("/api/competitions/<comp_id>/entries")
def submit_entry(comp_id):
    row = get_competition_row(comp_id)
    if row["status"] != "open":
        return jsonify({"error": "This competition has ended."}), 400

    participant = (request.form.get("participant") or "").strip()
    length_raw = request.form.get("length")
    image = request.files.get("image")

    if not participant or not length_raw or image is None:
        return jsonify({"error": "participant, length, and image are required"}), 400
    try:
        length = float(length_raw)
        if length <= 0 or length > 1000:
            raise ValueError()
    except ValueError:
        return jsonify({"error": "length must be a positive number"}), 400
    if image.filename == "" or not allowed_file(image.filename):
        return jsonify({"error": "unsupported image type"}), 400

    # Must have joined to submit
    db = get_db()
    joined = db.execute(
        "SELECT 1 FROM participants WHERE competition_id=? AND name=? COLLATE NOCASE",
        (comp_id, participant),
    ).fetchone()
    if not joined:
        return jsonify({"error": "join the competition before submitting a catch"}), 403

    # Re-encode & downsize server-side so we never trust the raw upload as-is
    try:
        img = Image.open(image.stream)
        img.verify()
        image.stream.seek(0)
        img = Image.open(image.stream).convert("RGB")
    except Exception:
        return jsonify({"error": "could not read image file"}), 400

    img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
    entry_id = uuid.uuid4().hex[:12]
    filename = secure_filename(f"{comp_id}_{entry_id}.jpg")
    filepath = os.path.join(UPLOAD_DIR, filename)
    img.save(filepath, "JPEG", quality=85)

    db.execute(
        """INSERT INTO entries (id, competition_id, participant, image_filename, length, submitted_at)
           VALUES (?,?,?,?,?,?)""",
        (entry_id, comp_id, participant, filename, length, now_iso()),
    )
    db.commit()
    return jsonify(row_to_competition_detail(get_competition_row(comp_id))), 201


@app.post("/api/competitions/<comp_id>/end")
def end_competition(comp_id):
    row = get_competition_row(comp_id)
    if row["status"] != "open":
        return jsonify({"error": "Competition already ended."}), 400
    data = request.get_json(force=True, silent=True) or {}
    requester = (data.get("requester") or "").strip()
    if requester.lower() != row["organizer"].lower():
        return jsonify({"error": "only the organizer can end this competition"}), 403

    db = get_db()
    winner = db.execute(
        "SELECT * FROM entries WHERE competition_id=? ORDER BY length DESC LIMIT 1",
        (comp_id,),
    ).fetchone()
    if winner is None:
        return jsonify({"error": "no entries have been submitted yet"}), 400

    message = (
        f"Congrats to {winner['participant']} for reeling in the winning catch at "
        f"{winner['length']:.2f} {row['ref_unit']}! Thanks to everyone who fished {row['title']}."
    )
    posted_at = now_iso()
    db.execute(
        """UPDATE competitions
           SET status='ended', winner_participant=?, winner_length=?,
               announcement_message=?, announcement_posted_at=?
           WHERE id=?""",
        (winner["participant"], winner["length"], message, posted_at, comp_id),
    )

    # "Post a message to all players" — persisted per-recipient notification log.
    participants = db.execute(
        "SELECT name FROM participants WHERE competition_id=?", (comp_id,)
    ).fetchall()
    for p in participants:
        db.execute(
            "INSERT INTO notifications (competition_id, recipient, message, sent_at) VALUES (?,?,?,?)",
            (comp_id, p["name"], message, posted_at),
        )
    db.commit()

    return jsonify(row_to_competition_detail(get_competition_row(comp_id)))


@app.get("/api/competitions/<comp_id>/notifications")
def list_notifications(comp_id):
    get_competition_row(comp_id)  # 404 if missing
    db = get_db()
    rows = db.execute(
        "SELECT recipient, message, sent_at FROM notifications WHERE competition_id=? ORDER BY sent_at",
        (comp_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/whoami")
def whoami():
    # Simple echo endpoint the frontend can ping to confirm the API is reachable.
    return jsonify({"ok": True, "time": now_iso()})


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e.description) if hasattr(e, "description") else "not found"}), 404
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"Reel Deal backend running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
else:
    # Imported by a WSGI server (gunicorn, PythonAnywhere's WSGI wrapper, etc.)
    init_db()
