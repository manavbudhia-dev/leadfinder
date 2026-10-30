"""Background job queue (SQLite-backed) with resume-after-restart and retry.

One daemon worker thread processes jobs sequentially — deploy with ONE
gunicorn worker + threads. process_func(conn, kind, row) and pause_check(conn)
app.py se inject hote hain.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import lead_pipeline as lp

DB_LOCK = threading.Lock()  # fresh-DB init race se bachata hai (app bhi yahi use kare)

_worker = None
_worker_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT, status TEXT, total INTEGER,
    created_at TEXT, filename TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS job_rows (
    job_id TEXT, idx INTEGER, first_name TEXT, last_name TEXT, domain TEXT,
    email_in TEXT, extra TEXT, email TEXT, status TEXT, source TEXT,
    state TEXT, log TEXT, PRIMARY KEY (job_id, idx));
"""


def _conn():
    with DB_LOCK:
        conn = lp.db()
    conn.executescript(SCHEMA)
    return conn


def create_job(kind, rows, filename=""):
    """rows: [{first_name, last_name, domain, email_in?, ...extra}]"""
    job_id = uuid.uuid4().hex[:12]
    conn = _conn()
    try:
        conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                     (job_id, kind, "queued", len(rows), date.today().isoformat(), filename, None))
        for i, row in enumerate(rows):
            extra = {k: v for k, v in row.items()
                     if k not in ("first_name", "last_name", "domain", "email_in")}
            conn.execute("INSERT INTO job_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, i, row["first_name"], row["last_name"], row["domain"],
                          row.get("email_in", ""), json.dumps(extra, ensure_ascii=False),
                          "", "", "", "pending", ""))
        conn.commit()
    finally:
        conn.close()
    return job_id


def recover_interrupted():
    """Startup par running jobs ko wapas queued karo (pending rows resume hongi)."""
    conn = _conn()
    try:
        conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        conn.commit()
    finally:
        conn.close()


def resume_job(job_id, include_not_found=False):
    conn = _conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return False
        conn.execute("UPDATE job_rows SET state='pending' WHERE job_id=? AND state='failed'", (job_id,))
        if include_not_found:
            conn.execute("UPDATE job_rows SET state='pending' WHERE job_id=? AND state='done' AND status='not_found'",
                         (job_id,))
        conn.execute("UPDATE jobs SET status='queued', error=NULL WHERE id=? AND status IN ('paused','done')",
                     (job_id,))
        conn.commit()
    finally:
        conn.close()
    return True


def list_jobs(limit=20):
    conn = _conn()
    try:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]
        for j in jobs:
            j["done"] = conn.execute(
                "SELECT COUNT(*) c FROM job_rows WHERE job_id=? AND state!='pending'", (j["id"],)).fetchone()["c"]
            counts = conn.execute(
                "SELECT status, COUNT(*) c FROM job_rows WHERE job_id=? AND state='done' GROUP BY status",
                (j["id"],)).fetchall()
            j["counts"] = {r["status"]: r["c"] for r in counts}
            j["failed"] = conn.execute(
                "SELECT COUNT(*) c FROM job_rows WHERE job_id=? AND state='failed'", (j["id"],)).fetchone()["c"]
    finally:
        conn.close()
    return jobs


def get_job(job_id):
    conn = _conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return None
        job = dict(job)
        rows = []
        for r in conn.execute("SELECT * FROM job_rows WHERE job_id=? ORDER BY idx", (job_id,)):
            row = dict(r)
            row.update(json.loads(row.pop("extra") or "{}"))
            rows.append(row)
        job["done"] = sum(r["state"] != "pending" for r in rows)
        job["rows"] = rows
    finally:
        conn.close()
    return job


def _claim_next(conn):
    job = conn.execute(
        "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at, rowid LIMIT 1").fetchone()
    if job is None:
        return None
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    conn.commit()
    return dict(job)


def _process_row(job, row, process_func, pause_check):
    row = dict(row)
    row.update(json.loads(row["extra"] or "{}"))
    conn = _conn()  # har parallel task ka apna connection
    try:
        reason = pause_check(conn)
        if reason:
            return row["idx"], "", "", "", "paused", reason
        email, status, source, log = process_func(conn, job["kind"], row)
        return row["idx"], email or "", status, source or "", "done", log
    except Exception as exc:  # ek lead fail ho to job chalta rahe
        return row["idx"], "", "", "", "failed", f"Error: {exc}"
    finally:
        conn.close()


def _loop(process_func, pause_check, on_done):
    parallel = max(1, int(os.getenv("PARALLEL_LEADS", "3")))
    while True:
        try:
            conn = _conn()
        except sqlite3.Error:
            time.sleep(2)
            continue
        try:
            job = _claim_next(conn)
        except sqlite3.Error:
            conn.close()
            time.sleep(2)
            continue
        if job is None:
            conn.close()  # idle mein connection band rakho (Windows file locks)
            time.sleep(1)
            continue
        try:
            pending = [dict(r) for r in conn.execute(
                "SELECT * FROM job_rows WHERE job_id=? AND state='pending' ORDER BY idx", (job["id"],))]
            paused = False
            pause_reason = ""
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = [pool.submit(_process_row, job, row, process_func, pause_check)
                           for row in pending]
                for fut in as_completed(futures):
                    try:
                        idx, email, status, source, state, log = fut.result()
                    except Exception:
                        continue  # cancelled future
                    if state == "paused":
                        paused, pause_reason = True, log
                        for f in futures:
                            f.cancel()
                        continue  # row pending rehti hai, kuch kharch nahi hua
                    conn.execute(
                        "UPDATE job_rows SET email=?, status=?, source=?, state=?, log=? WHERE job_id=? AND idx=?",
                        (email, status, source, state, log, job["id"], idx))
                    conn.commit()
            if paused:
                conn.execute("UPDATE jobs SET status='paused', error=? WHERE id=?", (pause_reason, job["id"]))
                conn.commit()
            else:
                conn.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
                conn.commit()
                if on_done:
                    try:
                        on_done(job["id"])
                    except Exception:
                        pass  # callback (sheet writeback) job ko fail na kare
        finally:
            conn.close()


def ensure_worker(process_func, pause_check, on_done=None):
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, args=(process_func, pause_check, on_done),
                                       name="job-worker", daemon=True)
            _worker.start()