"""
Lead Finder web app: Flask API + vanilla JS frontend, production-ready.

Local dev:   python app.py            -> http://127.0.0.1:5000 (no auth)
Production:  gunicorn -w 1 --threads 8 --timeout 300 -b 0.0.0.0:8000 app:app
             (APP_PASSWORD set karna zaruri hai public server par)

Env:
    APP_PASSWORD / SECRET_KEY / DATA_DIR / DB_PATH   (basic)
    PATTERN_GUESS / PUSH_VALID / WEBHOOK_URL         (features)
    PARALLEL_LEADS=3            batch concurrency
    PROVIDER_MIN_INTERVAL=0.5   per-provider rate limit (seconds)
    BACKUP_HOURS=24 / BACKUP_KEEP=7                  SQLite auto-backup
    TRUST_PROXY=1               reverse proxy ke peeche (X-Forwarded-*)
    OUTREACH_PROVIDER / OUTREACH_API_KEY / OUTREACH_CAMPAIGN
    GOOGLE_SERVICE_ACCOUNT_JSON                      Sheets writeback
"""
import contextlib
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta

# lead_pipeline DB_PATH import-time par padhta hai, isliye pehle set karo
os.environ.setdefault("DB_PATH", os.path.join(os.getenv("DATA_DIR", "."), "leads.db"))

import requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

import backup
import jobs
import lead_pipeline as lp
import linkedin
import outreach
import settings_store
import sheets
import throttle

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB CSV cap
if os.getenv("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# lp.db() reads-then-inserts quota rows; concurrent requests would race on a
# fresh database. Deploy with ONE gunicorn worker + threads.
DB_LOCK = jobs.DB_LOCK

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_meta (
    first_name TEXT, last_name TEXT, domain TEXT,
    pipeline TEXT DEFAULT '', tags TEXT DEFAULT '', notes TEXT DEFAULT '',
    updated_at TEXT, PRIMARY KEY (first_name, last_name, domain));
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY, name TEXT, token_hash TEXT, created_at TEXT, last_used TEXT);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, name TEXT, csv_url TEXT, interval_hours REAL,
    sheet_id TEXT, enabled INTEGER, last_run TEXT, last_job_id TEXT);
"""

KEY_ENVS = ["REOON_API_KEY", "HUNTER_API_KEY", "PROSPEO_API_KEY", "APOLLO_API_KEY"]
PUBLIC_PATHS = {"/login", "/api/login", "/healthz", "/style.css"}
PIPELINE_VALUES = {"", "contacted", "replied", "interested", "closed"}
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_SECONDS = 900

_login_fails = {}  # ip -> [count, locked_until_monotonic]
_login_lock = threading.Lock()


def db():
    with DB_LOCK:
        conn = lp.db()
    conn.executescript(EXTRA_SCHEMA)
    return conn


def auth_enabled():
    return bool(os.getenv("APP_PASSWORD"))


def is_authed():
    return not auth_enabled() or session.get("auth") is True


def _bearer_token_ok():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    token_hash = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    conn = db()
    try:
        row = conn.execute("SELECT id, last_used FROM api_tokens WHERE token_hash=?", (token_hash,)).fetchone()
        if row is None:
            return False
        now = datetime.now()
        last = row["last_used"]
        if not last or (now - datetime.fromisoformat(last)).total_seconds() > 60:
            conn.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (now.isoformat(timespec="seconds"), row["id"]))
            conn.commit()
        return True
    finally:
        conn.close()


@app.before_request
def require_login():
    if request.path in PUBLIC_PATHS:
        return None
    if is_authed():
        return None
    if request.path.startswith("/api/") and _bearer_token_ok():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Login zaruri hai.", "login": True}), 401
    return redirect("/login")


def clean_domain(d):
    d = str(d).strip().lower()
    for prefix in ("https://", "http://", "www."):
        d = d.removeprefix(prefix)
    return d.strip("/")


def pattern_enabled():
    return os.getenv("PATTERN_GUESS", "1") == "1"


def keys_ready():
    if not os.getenv("REOON_API_KEY"):
        return "Pehle sidebar mein Reoon key daalo."
    if not pattern_enabled() and not any(os.getenv(env) for _, _, env, _ in lp.PROVIDERS):
        return "Kam se kam ek finder (Hunter / Prospeo / Apollo) ki key daalo, ya pattern-guess on karo."
    return None


def pattern_guesses(first, last, domain):
    f, l = first.lower(), last.lower()
    locals_ = [f"{f}.{l}", f, f"{f[0]}{l}", f"{f}{l[0]}", f"{f}{l}", f"{f}_{l}"]
    seen, out = set(), []
    for x in locals_:
        if x not in seen:
            seen.add(x)
            out.append(f"{x}@{domain}")
    return out


def push_webhook(payload, force=False):
    url = os.getenv("WEBHOOK_URL", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return False, "Webhook URL set nahi hai."
    if not force and os.getenv("PUSH_VALID") != "1":
        return False, "Auto-push off hai."
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok, f"HTTP {r.status_code}"
    except requests.RequestException as exc:
        return False, str(exc)


def lead_payload(first, last, domain, email, source):
    return {"event": "lead.valid", "first_name": first, "last_name": last,
            "domain": domain, "email": email, "source": source,
            "found_at": datetime.now().isoformat(timespec="seconds")}


def save_lead(conn, first, last, domain, email, status, source):
    conn.execute(
        "INSERT OR REPLACE INTO leads VALUES (?,?,?,?,?,?,?)",
        (first, last, domain, email, status, source, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    if status == "valid" or (status == "risky" and os.getenv("PUSH_ONLY_VALID", "1") == "0"):
        ok, info = push_webhook(lead_payload(first, last, domain, email, source))
        if os.getenv("PUSH_VALID") == "1":
            return f"  - webhook push: {'ok' if ok else 'FAIL'} ({info})\n"
    return ""


def process_lead(conn, first, last, domain):
    """Cache -> free pattern-guess -> paid waterfall -> save -> webhook."""
    cached = conn.execute(
        "SELECT email, status, source FROM leads WHERE first_name=? AND last_name=? AND domain=? AND status='valid'",
        (first, last, domain),
    ).fetchone()
    if cached:
        return cached["email"], cached["status"], cached["source"], "Pehle se mil chuka tha, credit kharch nahi hua."

    log = io.StringIO()
    email = status = source = None
    fallback = None
    with contextlib.redirect_stdout(log):
        if pattern_enabled():
            for guess in pattern_guesses(first, last, domain):
                st = lp.verify(guess)
                print(f"  - pattern: {guess} -> {st}")
                if st in lp.VALID:
                    email, status, source = guess, "valid", "pattern"
                    break
                if st in lp.RISKY and fallback is None:
                    fallback = (guess, "risky", "pattern")
        if email is None:
            e2, s2, src2 = lp.find_email(conn, first, last, domain)
            if s2 == "valid":
                email, status, source = e2, s2, src2
            elif s2 == "risky" and fallback is None:
                fallback = (e2, s2, src2)
    if email is None and fallback is not None:
        email, status, source = fallback
    if email is None:
        status = "not_found"
    log.write(save_lead(conn, first, last, domain, email, status, source))
    return email, status, source, log.getvalue()


def reverify_row(conn, row):
    lead = conn.execute(
        "SELECT * FROM leads WHERE first_name=? AND last_name=? AND domain=?",
        (row["first_name"], row["last_name"], row["domain"]),
    ).fetchone()
    email = (row.get("email_in") or (lead["email"] if lead else "") or "").strip()
    if not email:
        return "", "not_found", "", "Re-verify ke liye email nahi tha."
    st = lp.verify(email)
    if st in lp.VALID:
        status = "valid"
    elif st in lp.RISKY:
        status = "risky"
    else:
        status, email = "not_found", ""
    source = lead["source"] if lead else ""
    save_lead(conn, row["first_name"], row["last_name"], row["domain"], email, status, source)
    return email, status, source, f"re-verify: {email or '(invalid)'} -> {st}"


def job_process(conn, kind, row):
    if kind == "reverify":
        return reverify_row(conn, row)
    return process_lead(conn, row["first_name"].strip(), row["last_name"].strip(),
                        clean_domain(row["domain"]))


def pause_check(conn):
    if pattern_enabled():
        return None  # guesses chalte rehte hain, Reoon credits alag hain
    configured = [name for name, _, env, _ in lp.PROVIDERS if os.getenv(env)]
    if configured and not any(lp.has_quota(conn, n) for n in configured):
        return "Sab finder credits khatam — job paused. Reset ke baad Resume karo."
    return None


# ------------------------------------------------------------------ schedules
def run_schedule(schedule_id):
    conn = db()
    try:
        s = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    finally:
        conn.close()
    if s is None:
        return None, "Schedule nahi mila."
    try:
        r = requests.get(s["csv_url"], timeout=60)
        r.raise_for_status()
        rows = parse_csv_rows(r.text)
    except (requests.RequestException, ValueError) as exc:
        return None, f"CSV fetch/parse fail: {exc}"
    if not rows:
        return None, "CSV khali hai."
    job_id = jobs.create_job("batch", rows, f"schedule:{s['name']}")
    conn = db()
    try:
        conn.execute("UPDATE schedules SET last_run=?, last_job_id=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), job_id, schedule_id))
        conn.commit()
    finally:
        conn.close()
    jobs.ensure_worker(job_process, pause_check, on_job_done)
    return job_id, None


def scheduler_tick():
    conn = db()
    try:
        schedules = [dict(r) for r in conn.execute("SELECT * FROM schedules WHERE enabled=1")]
    finally:
        conn.close()
    now = datetime.now()
    for s in schedules:
        last = s["last_run"]
        if last and now - datetime.fromisoformat(last) < timedelta(hours=s["interval_hours"]):
            continue
        run_schedule(s["id"])


def _scheduler_loop():
    while True:
        time.sleep(float(os.getenv("SCHEDULER_INTERVAL", "300")))
        try:
            scheduler_tick()
        except Exception:
            pass  # scheduler best-effort hai


def on_job_done(job_id):
    """Schedule-linked job khatam -> results Google Sheet mein append."""
    conn = db()
    try:
        s = conn.execute("SELECT * FROM schedules WHERE last_job_id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if s is None or not s["sheet_id"]:
        return
    job = jobs.get_job(job_id)
    rows = [r for r in job["rows"] if r["state"] == "done"]
    if not rows:
        return
    header = ["first_name", "last_name", "domain", "email", "status", "source"]
    values = [header] + [[r.get(c, "") for c in header] for r in rows]
    ok, info = sheets.append_rows(s["sheet_id"], values)
    print(f"[schedule:{s['name']}] sheet writeback: {'ok' if ok else 'FAIL'} ({info})")


settings_store.load_into_env()
throttle.wrap_providers()
with DB_LOCK:
    _init_conn = lp.db()
_init_conn.executescript(EXTRA_SCHEMA)
_init_conn.close()
jobs.recover_interrupted()
jobs.ensure_worker(job_process, pause_check, on_job_done)
backup.start()
threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()


# ------------------------------------------------------------------ auth
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/login")
def login_page():
    if is_authed():
        return redirect("/")
    return send_from_directory(app.static_folder, "login.html")


@app.post("/api/login")
def login():
    if not auth_enabled():
        session["auth"] = True
        return jsonify({"ok": True})
    ip = request.remote_addr or "?"
    with _login_lock:
        count, locked_until = _login_fails.get(ip, (0, 0.0))
        if time.monotonic() < locked_until:
            wait = int(locked_until - time.monotonic())
            return jsonify({"error": f"Bahut zyada galat attempts. {wait}s baad try karo."}), 429
    body = request.get_json(silent=True) or {}
    password = str(body.get("password", ""))
    if hmac.compare_digest(password.encode(), os.environ["APP_PASSWORD"].encode()):
        with _login_lock:
            _login_fails.pop(ip, None)
        session["auth"] = True
        return jsonify({"ok": True})
    with _login_lock:
        count += 1
        locked = time.monotonic() + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_FAILS else 0.0
        _login_fails[ip] = (count, locked)
    time.sleep(1)  # brute force slow karo
    return jsonify({"error": "Galat password."}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ------------------------------------------------------------------ API tokens
@app.get("/api/tokens")
def list_tokens():
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, created_at, last_used FROM api_tokens ORDER BY created_at DESC")]
    finally:
        conn.close()
    return jsonify(rows)


@app.post("/api/tokens")
def create_token():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip() or "token"
    token = "lf_" + secrets.token_urlsafe(32)
    conn = db()
    try:
        conn.execute("INSERT INTO api_tokens VALUES (?,?,?,?,?)",
                     (secrets.token_hex(6), name, hashlib.sha256(token.encode()).hexdigest(),
                      datetime.now().isoformat(timespec="seconds"), None))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"token": token, "name": name})  # plaintext sirf ek baar dikhta hai


@app.delete("/api/tokens/<token_id>")
def delete_token(token_id):
    conn = db()
    try:
        conn.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
def get_settings():
    return jsonify({
        "keys": {env: bool(os.getenv(env)) for env in KEY_ENVS},
        "limits": {
            name: int(os.getenv(f"{name.upper()}_LIMIT", default))
            for name, _, _, default in lp.PROVIDERS
        },
        "auth": auth_enabled(),
        "flags": {"pattern_guess": pattern_enabled(), "push_valid": os.getenv("PUSH_VALID") == "1",
                  "push_only_valid": os.getenv("PUSH_ONLY_VALID", "1") != "0"},
        "webhook_set": bool(os.getenv("WEBHOOK_URL", "").strip()),
        "outreach": {
            "provider": os.getenv("OUTREACH_PROVIDER", "instantly"),
            "campaign": os.getenv("OUTREACH_CAMPAIGN", ""),
            "key_set": bool(os.getenv("OUTREACH_API_KEY", "").strip()),
            "enabled": os.getenv("OUTREACH_ENABLED", "1") != "0",
        },
    })


@app.post("/api/settings")
def save_settings():
    body = request.get_json(silent=True) or {}
    persisted = {}
    for env in KEY_ENVS:
        if env in body.get("keys", {}):
            val = str(body["keys"][env]).strip()
            if val:
                os.environ[env] = val
            else:
                os.environ.pop(env, None)
            persisted[env] = val
    for name, _, _, _ in lp.PROVIDERS:
        limit_env = f"{name.upper()}_LIMIT"
        if name in body.get("limits", {}):
            try:
                n = max(0, int(body["limits"][name]))
            except (TypeError, ValueError):
                return jsonify({"error": f"{name} limit ek number hona chahiye."}), 400
            os.environ[limit_env] = str(n)
            persisted[limit_env] = str(n)
    for flag, env in [("pattern_guess", "PATTERN_GUESS"), ("push_valid", "PUSH_VALID"),
                      ("push_only_valid", "PUSH_ONLY_VALID")]:
        if flag in body.get("flags", {}):
            val = "1" if body["flags"][flag] else "0"
            os.environ[env] = val
            persisted[env] = val
    if "webhook_url" in body:
        url = str(body["webhook_url"]).strip()
        if url and not url.startswith(("http://", "https://")):
            return jsonify({"error": "Webhook URL http(s):// se shuru hona chahiye."}), 400
        if url:
            os.environ["WEBHOOK_URL"] = url
        else:
            os.environ.pop("WEBHOOK_URL", None)
        persisted["WEBHOOK_URL"] = url
    oc = body.get("outreach", {})
    if "provider" in oc:
        if oc["provider"] not in ("instantly", "smartlead", "lemlist"):
            return jsonify({"error": "Outreach provider instantly/smartlead/lemlist hona chahiye."}), 400
        os.environ["OUTREACH_PROVIDER"] = oc["provider"]
        persisted["OUTREACH_PROVIDER"] = oc["provider"]
    if "enabled" in oc:
        val = "1" if oc["enabled"] else "0"
        os.environ["OUTREACH_ENABLED"] = val
        persisted["OUTREACH_ENABLED"] = val
    if "campaign" in oc:
        val = str(oc["campaign"]).strip()
        if val:
            os.environ["OUTREACH_CAMPAIGN"] = val
        else:
            os.environ.pop("OUTREACH_CAMPAIGN", None)
        persisted["OUTREACH_CAMPAIGN"] = val
    if "api_key" in oc:
        val = str(oc["api_key"]).strip()
        if val:
            os.environ["OUTREACH_API_KEY"] = val
        else:
            os.environ.pop("OUTREACH_API_KEY", None)
        persisted["OUTREACH_API_KEY"] = val
    if persisted:
        with DB_LOCK:
            settings_store.save(persisted)
    return get_settings()


# ------------------------------------------------------------------ single lead
@app.post("/api/find")
def find_one():
    body = request.get_json(silent=True) or {}
    linkedin_url = str(body.get("linkedin_url", "")).strip()

    if linkedin_url:
        if not linkedin_url.startswith(("http://", "https://")):
            return jsonify({"error": "LinkedIn URL http(s):// se shuru hona chahiye."}), 400
        if not os.getenv("REOON_API_KEY"):
            return jsonify({"error": "Reoon key set nahi hai."}), 400
        if not (os.getenv("PROSPEO_API_KEY") or os.getenv("APOLLO_API_KEY")):
            return jsonify({"error": "LinkedIn lookup ke liye Prospeo ya Apollo key chahiye."}), 400
        conn = db()
        try:
            person, status, source, logs = linkedin.find_by_linkedin(conn, linkedin_url)
            if person is None:
                return jsonify({"email": None, "status": "not_found", "source": None,
                                "log": "\n".join(logs)})
            first, last = person["first_name"] or "", person["last_name"] or ""
            domain = clean_domain(person["domain"]) or person["email"].split("@")[1]
            extra = save_lead(conn, first, last, domain, person["email"], status, source)
        finally:
            conn.close()
        return jsonify({"email": person["email"], "status": status, "source": source,
                        "first_name": first, "last_name": last, "domain": domain,
                        "log": "\n".join(logs) + extra})

    first = str(body.get("first_name", "")).strip()
    last = str(body.get("last_name", "")).strip()
    domain = clean_domain(body.get("domain", ""))
    if not (first and last and domain):
        return jsonify({"error": "Teeno boxes bharo (ya LinkedIn URL daalo)."}), 400
    err = keys_ready()
    if err:
        return jsonify({"error": err}), 400
    conn = db()
    try:
        email, status, source, log = process_lead(conn, first, last, domain)
    finally:
        conn.close()
    return jsonify({"email": email, "status": status, "source": source, "log": log})


# ------------------------------------------------------------------ batch jobs
def parse_csv_rows(text):
    reader = csv.DictReader(io.StringIO(text))
    fields = [(c or "").strip().lower() for c in (reader.fieldnames or [])]
    missing = {"first_name", "last_name", "domain"} - set(fields)
    if missing:
        raise ValueError(f"Ye columns missing hain: {', '.join(sorted(missing))}")
    return [{(k or "").strip().lower(): (v or "") for k, v in raw.items()} for raw in reader]


@app.post("/api/jobs")
def create_batch_job():
    up = request.files.get("file")
    if up is None:
        return jsonify({"error": "CSV file nahi mili."}), 400
    try:
        rows = parse_csv_rows(up.read().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        return jsonify({"error": str(exc) or "CSV padhi nahi ja saki."}), 400
    if not rows:
        return jsonify({"error": "CSV khali hai."}), 400
    err = keys_ready()
    if err:
        return jsonify({"error": err}), 400
    job_id = jobs.create_job("batch", rows, up.filename or "leads.csv")
    jobs.ensure_worker(job_process, pause_check, on_job_done)
    return jsonify({"job_id": job_id, "total": len(rows)})


@app.get("/api/jobs")
def list_jobs():
    return jsonify(jobs.list_jobs())


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.get_job(job_id)
    if job is None:
        return jsonify({"error": "Job nahi mila."}), 404
    return jsonify(job)


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id):
    body = request.get_json(silent=True) or {}
    if not jobs.resume_job(job_id, include_not_found=bool(body.get("include_not_found"))):
        return jsonify({"error": "Job nahi mila."}), 404
    jobs.ensure_worker(job_process, pause_check, on_job_done)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ history + CRM
def history_rows(q, status, pipeline=""):
    sql = ("SELECT l.*, COALESCE(m.pipeline,'') pipeline, COALESCE(m.tags,'') tags, "
           "COALESCE(m.notes,'') notes FROM leads l LEFT JOIN lead_meta m "
           "ON l.first_name=m.first_name AND l.last_name=m.last_name AND l.domain=m.domain WHERE 1=1")
    params = []
    if status:
        sql += " AND l.status=?"
        params.append(status)
    if pipeline:
        sql += " AND COALESCE(m.pipeline,'')=?"
        params.append(pipeline)
    if q:
        sql += " AND (l.first_name LIKE ? OR l.last_name LIKE ? OR l.domain LIKE ? OR l.email LIKE ? OR m.tags LIKE ?)"
        params += [f"%{q}%"] * 5
    sql += " ORDER BY l.found_at DESC"
    conn = db()
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


@app.get("/api/history")
def history():
    return jsonify(history_rows(request.args.get("q", "").strip(),
                                request.args.get("status", "").strip(),
                                request.args.get("pipeline", "").strip()))


@app.get("/api/history/export")
def history_export():
    rows = history_rows(request.args.get("q", "").strip(),
                        request.args.get("status", "").strip(),
                        request.args.get("pipeline", "").strip())
    out = io.StringIO()
    fields = ["first_name", "last_name", "domain", "email", "status", "source", "found_at",
              "pipeline", "tags", "notes"]
    w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
    )


@app.post("/api/leads/meta")
def update_meta():
    body = request.get_json(silent=True) or {}
    first = str(body.get("first_name", "")).strip()
    last = str(body.get("last_name", "")).strip()
    domain = clean_domain(body.get("domain", ""))
    if not (first and last and domain):
        return jsonify({"error": "Lead identify karne ke liye naam + domain chahiye."}), 400
    pipeline = str(body.get("pipeline", "")).strip()
    if pipeline not in PIPELINE_VALUES:
        return jsonify({"error": f"Pipeline value in mein se honi chahiye: {sorted(PIPELINE_VALUES)}"}), 400
    conn = db()
    try:
        conn.execute("INSERT OR REPLACE INTO lead_meta VALUES (?,?,?,?,?,?,?)",
                     (first, last, domain, pipeline, str(body.get("tags", "")),
                      str(body.get("notes", "")), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/reverify")
def reverify_one():
    body = request.get_json(silent=True) or {}
    first = str(body.get("first_name", "")).strip()
    last = str(body.get("last_name", "")).strip()
    domain = clean_domain(body.get("domain", ""))
    if not (first and last and domain):
        return jsonify({"error": "Lead identify karne ke liye naam + domain chahiye."}), 400
    if not os.getenv("REOON_API_KEY"):
        return jsonify({"error": "Reoon key set nahi hai."}), 400
    conn = db()
    try:
        email, status, source, log = reverify_row(
            conn, {"first_name": first, "last_name": last, "domain": domain})
    finally:
        conn.close()
    return jsonify({"email": email, "status": status, "source": source, "log": log})


@app.post("/api/reverify-bulk")
def reverify_bulk():
    body = request.get_json(silent=True) or {}
    status = str(body.get("status", "risky")).strip()
    rows = [r for r in history_rows("", status) if r["email"]]
    if not rows:
        return jsonify({"error": f"'{status}' status wale emails nahi mile."}), 400
    if not os.getenv("REOON_API_KEY"):
        return jsonify({"error": "Reoon key set nahi hai."}), 400
    job_rows = [{"first_name": r["first_name"], "last_name": r["last_name"],
                 "domain": r["domain"], "email_in": r["email"]} for r in rows]
    job_id = jobs.create_job("reverify", job_rows, f"reverify-{status}")
    jobs.ensure_worker(job_process, pause_check, on_job_done)
    return jsonify({"job_id": job_id, "total": len(rows)})


# ------------------------------------------------------------------ push / outreach
@app.post("/api/push")
def push_valid():
    if not os.getenv("WEBHOOK_URL", "").strip():
        return jsonify({"error": "Pehle webhook URL set karo."}), 400
    rows = [r for r in history_rows("", "valid") if r["email"]]
    pushed = failed = 0
    for r in rows:
        ok, _ = push_webhook(lead_payload(r["first_name"], r["last_name"], r["domain"],
                                          r["email"], r["source"]), force=True)
        pushed += ok
        failed += not ok
    return jsonify({"pushed": pushed, "failed": failed, "total": len(rows)})


@app.post("/api/webhook/test")
def webhook_test():
    ok, info = push_webhook({"event": "webhook.test", "first_name": "Test", "last_name": "Lead",
                             "domain": "example.com", "email": "test@example.com", "source": "test",
                             "found_at": datetime.now().isoformat(timespec="seconds")}, force=True)
    return jsonify({"ok": ok, "info": info, **({} if ok else {"error": info})}), (200 if ok else 400)


@app.post("/api/outreach/push")
def outreach_push():
    rows = [r for r in history_rows("", "valid") if r["email"]]
    ok, info = outreach.push_leads(rows)
    return jsonify({"ok": ok, "info": info, "total": len(rows), **({} if ok else {"error": info})}), (200 if ok else 400)


# ------------------------------------------------------------------ schedules API
@app.get("/api/schedules")
def list_schedules():
    conn = db()
    try:
        return jsonify([dict(r) for r in conn.execute("SELECT * FROM schedules ORDER BY rowid")])
    finally:
        conn.close()


@app.post("/api/schedules")
def create_schedule():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip() or "schedule"
    csv_url = str(body.get("csv_url", "")).strip()
    if not csv_url.startswith(("http://", "https://")):
        return jsonify({"error": "CSV URL http(s):// se shuru hona chahiye (Google Sheet publish-to-web CSV link bhi chalega)."}), 400
    try:
        interval = max(1.0, float(body.get("interval_hours", 24)))
    except (TypeError, ValueError):
        return jsonify({"error": "interval_hours number hona chahiye."}), 400
    sid = secrets.token_hex(6)
    conn = db()
    try:
        conn.execute("INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?)",
                     (sid, name, csv_url, interval, str(body.get("sheet_id", "")).strip(),
                      1 if body.get("enabled", True) else 0, None, None))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"id": sid})


@app.post("/api/schedules/<sid>")
def update_schedule(sid):
    body = request.get_json(silent=True) or {}
    conn = db()
    try:
        s = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        if s is None:
            return jsonify({"error": "Schedule nahi mila."}), 404
        enabled = 1 if body.get("enabled", s["enabled"]) else 0
        interval = s["interval_hours"]
        if "interval_hours" in body:
            try:
                interval = max(1.0, float(body["interval_hours"]))
            except (TypeError, ValueError):
                return jsonify({"error": "interval_hours number hona chahiye."}), 400
        conn.execute("UPDATE schedules SET enabled=?, interval_hours=?, sheet_id=? WHERE id=?",
                     (enabled, interval, str(body.get("sheet_id", s["sheet_id"])).strip(), sid))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.delete("/api/schedules/<sid>")
def delete_schedule(sid):
    conn = db()
    try:
        conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/schedules/<sid>/run")
def run_schedule_now(sid):
    err = keys_ready()
    if err:
        return jsonify({"error": err}), 400
    job_id, error = run_schedule(sid)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"job_id": job_id})


# ------------------------------------------------------------------ dashboard
@app.get("/api/quota")
def quota():
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT provider, used, monthly_limit, resets_on FROM quotas")]
    finally:
        conn.close()
    for r in rows:
        r["remaining"] = r["monthly_limit"] - r["used"]
        r["exhausted"] = r["remaining"] <= 0
        r["low"] = r["remaining"] <= max(2, r["monthly_limit"] // 10)
    return jsonify(rows)


@app.get("/api/stats")
def stats():
    conn = db()
    try:
        status_counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM leads GROUP BY status")}
        pipeline_counts = {r["pipeline"]: r["c"] for r in conn.execute(
            "SELECT COALESCE(m.pipeline,'') pipeline, COUNT(*) c FROM leads l LEFT JOIN lead_meta m "
            "ON l.first_name=m.first_name AND l.last_name=m.last_name AND l.domain=m.domain "
            "GROUP BY COALESCE(m.pipeline,'')")}
        daily = [dict(r) for r in conn.execute(
            "SELECT substr(found_at,1,10) found_at, status, COUNT(*) c FROM leads GROUP BY substr(found_at,1,10), status "
            "ORDER BY substr(found_at,1,10) DESC LIMIT 270")]
    finally:
        conn.close()
    return jsonify({"status_counts": status_counts, "pipeline_counts": pipeline_counts, "daily": daily})


@app.get("/api/sample.csv")
def sample_csv():
    return Response(
        "first_name,last_name,domain\nPatrick,Collison,stripe.com\n",
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample_leads.csv"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)