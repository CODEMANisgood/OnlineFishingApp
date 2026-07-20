# Reel Deal — Fishing Competition App

A full-stack fishing tournament site: post competitions, sign up, submit
ruler-calibrated catch photos, and auto-crown a winner who gets announced
to every signed-up angler.

**This version is split for cross-origin hosting**: the Python/Flask API
runs on a cloud platform (Render, Railway, or PythonAnywhere), and the
static frontend (`static/index.html`) can be uploaded as-is to Neocities
or any other static host. They talk to each other over HTTP with CORS.

## Project layout
```
reel-deal/
├── app.py              Flask API — deploy this to Render/Railway/PythonAnywhere
├── requirements.txt
├── Procfile             Tells Render/Railway how to start the app with gunicorn
├── runtime.txt           Pins the Python version for platforms that read it
├── static/index.html     Frontend — upload this to Neocities (or wherever)
└── uploads/               Catch photos are saved here at runtime
```

---

## 1. Deploy the backend

Pick one. All three work; they differ mainly in how persistent your data
is and how you configure things.

### Option A — Render
1. Push this project to a GitHub repo.
2. Render dashboard → **New → Web Service** → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
   (Render also auto-detects the `Procfile`, so this may already be filled in.)
5. Add an environment variable: `CORS_ORIGINS` = `https://yourname.neocities.org`
6. Deploy. Render gives you a URL like `https://reel-deal.onrender.com`.
7. **Persistence matters here**: Render's free web services use an
   *ephemeral* disk — every redeploy or restart wipes `reeldeal.db` and any
   uploaded photos. For real persistence, add a paid **Render Disk**,
   mount it (e.g. at `/data`), and set the env var `DATA_DIR=/data`.

### Option B — Railway
1. Push to GitHub → Railway dashboard → **New Project → Deploy from repo**.
2. Railway auto-detects the `Procfile`/Python and builds it.
3. Add a **Volume**, mount it at `/data`, and set env var `DATA_DIR=/data`
   so your SQLite DB and uploaded photos survive redeploys.
4. Add env var `CORS_ORIGINS` = `https://yourname.neocities.org`.
5. Railway assigns a public domain automatically (Settings → Networking →
   Generate Domain).

### Option C — PythonAnywhere
PythonAnywhere doesn't use gunicorn/Procfiles — it runs your Flask app
through its own WSGI wrapper, and its disk *is* persistent by default,
which suits SQLite well.
1. Open a **Bash console** on PythonAnywhere, `git clone` your repo (or
   upload the files via the Files tab).
2. Create a virtualenv and install deps:
   ```bash
   mkvirtualenv reel-deal-env --python=python3.12
   pip install -r requirements.txt
   ```
3. **Web** tab → **Add a new web app** → **Manual configuration** → pick
   your Python version.
4. Set the virtualenv path in the Web tab to the one you just created.
5. Open the **WSGI configuration file** it links to, and set it to:
   ```python
   import sys, os
   path = '/home/yourusername/reel-deal'
   if path not in sys.path:
       sys.path.append(path)
   os.environ['CORS_ORIGINS'] = 'https://yourname.neocities.org'
   from app import app as application
   ```
6. Hit **Reload** on the Web tab. Your API is now live at
   `https://yourusername.pythonanywhere.com`.
   (Free PythonAnywhere accounts restrict outbound requests to an
   allowlist, but this app makes none, so that's not an issue.)

---

## 2. Point the frontend at your backend

Open `static/index.html`, find this near the top of the `<script>` block:

```js
const API = 'https://your-backend.onrender.com';
```

Change it to whichever URL you got from step 1 (no trailing slash).

## 3. Upload the frontend to Neocities

Just upload `static/index.html` to your Neocities site as `index.html`
(drag-and-drop in their editor, or via the Neocities CLI/API). It has no
build step and no dependencies beyond Google Fonts.

## 4. Test it

Visit your Neocities URL. The small dot next to "API" in the header turns
green once it can reach the backend. If it stays red/grey, check:
- the `API` constant matches your backend's actual URL exactly
- `CORS_ORIGINS` on the backend includes your exact Neocities origin
  (`https://yourname.neocities.org`, including `https://`, no trailing slash)
- the backend service is actually awake (free tiers on Render/Railway can
  sleep after inactivity and take a few seconds to spin back up)

---

## Accounts

Anglers now sign up with a username and password instead of just typing a
name. Passwords are hashed with Werkzeug's `generate_password_hash`
(PBKDF2) — never stored in plaintext. Logging in issues a bearer token
(`/api/auth/login`, `/api/auth/register`) that the frontend stores in
`localStorage` and sends as `Authorization: Bearer <token>` on every
request that needs to know who you are.

This replaces the old "type any name" identity model. Creating a
competition, joining one, submitting a catch, and ending a competition
all now require a valid token, and the *username on that token* — not
anything typed into a form — is what gets recorded as organizer or
participant. That closes the gap the previous version's README called
out: it's no longer possible to end someone else's competition, or log
a catch under someone else's name, just by typing their name in a box.

Sessions are simple opaque tokens in a `sessions` table (not JWTs), so
"sign out" is a real server-side revocation (`/api/auth/logout` deletes
the row) rather than just forgetting a token client-side.

## How the measuring tool works

After uploading a photo, the angler clicks the two ends of a known length
on their ruler (entering that real-world distance), then the snout and
tail tip of the fish. The browser computes a pixel-to-real ratio to
estimate length — a photo alone can't tell scale, but a ruler in frame
plus two clicks can. The photo and computed length are then uploaded to
the backend, which re-encodes and downsizes the image server-side (via
Pillow) before storing it.

## How winner announcements work

Only the organizer (matched by name) can end a competition. The backend
finds the longest logged entry, marks it the winner, and writes a
notification row for every signed-up participant — that's the "post a
message to all players" step. It's stored server-side and shown to
anyone viewing the competition. There's no outbound email/SMS wired up;
`notifications` in the database is exactly where you'd hook that in (see
`end_competition()` in `app.py`) if you want real emails, e.g. via SMTP
or a service like SendGrid.

## API reference

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/register` | Create an account `{username, password}` → `{username, token}` |
| POST | `/api/auth/login` | Log in `{username, password}` → `{username, token}` |
| POST | `/api/auth/logout` | Revoke the current token (send `Authorization: Bearer <token>`) |
| GET | `/api/auth/me` | Returns the logged-in username, or 401 |
| GET | `/api/whoami` | Health check |
| GET | `/api/competitions` | List all competitions (summary) |
| POST | `/api/competitions` | Create a competition |
| GET | `/api/competitions/<id>` | Full competition detail |
| POST | `/api/competitions/<id>/join` | Sign up (requires auth — signs up whoever's token this is) |
| POST | `/api/competitions/<id>/entries` | Submit a catch (requires auth; multipart: `length`, `image`) |
| POST | `/api/competitions/<id>/end` | Requires auth; only works if you're the organizer — ends comp, crowns winner |
| GET | `/api/competitions/<id>/notifications` | Winner-announcement log |

## Environment variables (backend)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Set automatically by Render/Railway |
| `DATA_DIR` | project folder | Where `reeldeal.db` and `uploads/` live — point at a persistent disk/volume in production |
| `CORS_ORIGINS` | `*` | Comma-separated list of frontend origins allowed to call the API |
| `FLASK_DEBUG` | `0` | Set to `1` for local dev auto-reload |

## Known limitations

- **No password reset flow.** If someone forgets their password, the
  only fix right now is deleting their row from the `users` table
  directly in the DB.
- **No outbound email/push** — see "winner announcements" above.
- **SQLite + gunicorn**: the `Procfile` runs a single worker with
  multiple threads specifically to avoid SQLite locking issues under
  concurrent writes. If you outgrow that, move to Postgres (Render and
  Railway both offer a free/managed Postgres instance).
