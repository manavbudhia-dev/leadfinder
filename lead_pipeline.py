#!/usr/bin/env python3
"""
Email-finding waterfall with quota tracking and Reoon verification.

    pip install requests python-dotenv
    cp .env.example .env            # fill in your keys
    python lead_pipeline.py run leads.csv     # columns: first_name,last_name,domain
    python lead_pipeline.py quota             # show usage per provider

Flow per lead:
    for each provider (in PLAN order, skipping exhausted ones):
        find email -> verify with Reoon -> stop on "valid"
    keep the best "risky" result (catch-all / role account) as a fallback.

.env keys:
    HUNTER_API_KEY, PROSPEO_API_KEY, APOLLO_API_KEY, REOON_API_KEY
    HUNTER_LIMIT, PROSPEO_LIMIT, APOLLO_LIMIT          (your real monthly credits)
    HUNTER_RESET_DAY, PROSPEO_RESET_DAY, APOLLO_RESET_DAY   (day of month, 1-28)
"""
import argparse
import csv
import os
import sqlite3
import sys
import time
from datetime import date

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DB_PATH = os.getenv("DB_PATH", "leads.db")
TIMEOUT = 30
PLACEHOLDER_EMAILS = ("email_not_unlocked",)


class QuotaExhausted(Exception):
    """Raised by a provider when the API says we're out of credits / rate limited."""


# ---------------------------------------------------------------- providers
def hunter(first, last, domain):
    r = requests.get(
        "https://api.hunter.io/v2/email-finder",
        params={
            "domain": domain,
            "first_name": first,
            "last_name": last,
            "api_key": os.environ["HUNTER_API_KEY"],
        },
        timeout=TIMEOUT,
    )
    if r.status_code in (402, 429):
        raise QuotaExhausted("hunter")
    if r.status_code != 200:
        return None
    return (r.json().get("data") or {}).get("email")


def prospeo(first, last, domain):
    r = requests.post(
        "https://api.prospeo.io/enrich-person",
        headers={"X-KEY": os.environ["PROSPEO_API_KEY"], "Content-Type": "application/json"},
        json={
            "only_verified_email": True,  # only pay for emails Prospeo has verified
            "enrich_mobile": False,       # mobile costs 10x, keep it off
            "data": {"first_name": first, "last_name": last, "company_website": domain},
        },
        timeout=TIMEOUT,
    )
    if r.status_code == 429:
        raise QuotaExhausted("prospeo")
    try:
        body = r.json()
    except ValueError:
        return None
    if body.get("error"):
        if "CREDIT" in str(body.get("error_code", "")).upper():
            raise QuotaExhausted("prospeo")
        return None  # NO_MATCH etc.
    email = (body.get("person") or {}).get("email")
    if isinstance(email, dict):  # response nests the address under email.email
        email = email.get("email")
    return email or None


def apollo(first, last, domain):
    r = requests.post(
        "https://api.apollo.io/api/v1/people/match",
        headers={"x-api-key": os.environ["APOLLO_API_KEY"], "Content-Type": "application/json"},
        json={"first_name": first, "last_name": last, "domain": domain},
        timeout=TIMEOUT,
    )
    if r.status_code in (402, 429):
        raise QuotaExhausted("apollo")
    if r.status_code != 200:
        return None
    email = (r.json().get("person") or {}).get("email")
    if not email or any(p in email for p in PLACEHOLDER_EMAILS):
        return None
    return email


# Waterfall order: most generous free tier first, scarcest last.
# (name, function, env var holding the key, default monthly limit)
PROVIDERS = [
    ("hunter", hunter, "HUNTER_API_KEY", 50),
    ("prospeo", prospeo, "PROSPEO_API_KEY", 75),
    ("apollo", apollo, "APOLLO_API_KEY", 50),
]


# ---------------------------------------------------------------- verification
VALID = {"valid", "safe"}
RISKY = {"catch_all", "role_account", "unknown"}  # sendable with caution / manual review


def verify(email):
    """Reoon power mode (checks the actual inbox). Returns Reoon's status string."""
    try:
        r = requests.get(
            "https://emailverifier.reoon.com/api/v1/verify",
            params={"email": email, "key": os.environ["REOON_API_KEY"], "mode": "power"},
            timeout=90,  # power mode waits on the recipient's mail server
        )
        return str(r.json().get("status", "unknown")).lower()
    except (requests.RequestException, ValueError):
        return "unknown"


# ---------------------------------------------------------------- quota tracking
def next_reset(day, today=None):
    today = today or date.today()
    d = date(today.year, today.month, day)
    if d <= today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = date(y, m, day)
    return d


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS quotas (
            provider TEXT PRIMARY KEY, used INTEGER, monthly_limit INTEGER, resets_on TEXT);
        CREATE TABLE IF NOT EXISTS leads (
            first_name TEXT, last_name TEXT, domain TEXT, email TEXT, status TEXT,
            source TEXT, found_at TEXT, PRIMARY KEY (first_name, last_name, domain));
        """
    )
    today = date.today()
    for name, _, _, default_limit in PROVIDERS:
        up = name.upper()
        limit = int(os.getenv(f"{up}_LIMIT", default_limit))
        reset_day = min(int(os.getenv(f"{up}_RESET_DAY", "1")), 28)
        row = conn.execute("SELECT * FROM quotas WHERE provider=?", (name,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO quotas VALUES (?,?,?,?)",
                (name, 0, limit, next_reset(reset_day).isoformat()),
            )
        else:
            used = 0 if today >= date.fromisoformat(row["resets_on"]) else row["used"]
            resets = next_reset(reset_day) if used == 0 and today >= date.fromisoformat(row["resets_on"]) else row["resets_on"]
            conn.execute(
                "UPDATE quotas SET used=?, monthly_limit=?, resets_on=? WHERE provider=?",
                (used, limit, resets, name),
            )
    conn.commit()
    return conn


def has_quota(conn, provider):
    row = conn.execute("SELECT used, monthly_limit FROM quotas WHERE provider=?", (provider,)).fetchone()
    return row["used"] < row["monthly_limit"]


def bump(conn, provider):
    conn.execute("UPDATE quotas SET used = used + 1 WHERE provider=?", (provider,))
    conn.commit()


def mark_exhausted(conn, provider):
    conn.execute("UPDATE quotas SET used = monthly_limit WHERE provider=?", (provider,))
    conn.commit()


# ---------------------------------------------------------------- waterfall
def find_email(conn, first, last, domain):
    tried, fallback = set(), None
    for name, func, env_var, _ in PROVIDERS:
        if not os.getenv(env_var):
            continue  # key not configured
        if not has_quota(conn, name):
            print(f"  - {name}: quota used up, skipping")
            continue
        try:
            email = func(first, last, domain)
        except QuotaExhausted:
            mark_exhausted(conn, name)
            print(f"  - {name}: API reports no credits left, skipping for now")
            continue
        except requests.RequestException as exc:
            print(f"  - {name}: network error ({exc})")
            continue

        if not email:
            print(f"  - {name}: no match")
            continue
        bump(conn, name)  # providers charge on a match; sync with the real dashboard now and then
        email = email.strip().lower()
        if email in tried:
            continue
        tried.add(email)

        status = verify(email)
        print(f"  - {name}: {email} -> {status}")
        if status in VALID:
            return email, "valid", name
        if status in RISKY and fallback is None:
            fallback = (email, "risky", name)
        # invalid / disposable / spamtrap etc. -> try the next provider

    return fallback or (None, "not_found", None)


def run(path, out_path):
    conn = db()
    results = []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, 1):
        first = row["first_name"].strip()
        last = row["last_name"].strip()
        domain = row["domain"].strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
        print(f"[{i}/{len(rows)}] {first} {last} @ {domain}")

        cached = conn.execute(
            "SELECT email, status, source FROM leads WHERE first_name=? AND last_name=? AND domain=? AND status='valid'",
            (first, last, domain),
        ).fetchone()
        if cached:
            print("  = already found, skipping")
            email, status, source = cached["email"], cached["status"], cached["source"]
        else:
            email, status, source = find_email(conn, first, last, domain)
            conn.execute(
                "INSERT OR REPLACE INTO leads VALUES (?,?,?,?,?,?,?)",
                (first, last, domain, email, status, source, date.today().isoformat()),
            )
            conn.commit()
        results.append({**row, "email": email or "", "status": status, "source": source or ""})
        time.sleep(0.3)

    fields = list(rows[0].keys()) + ["email", "status", "source"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    counts = {s: sum(r["status"] == s for r in results) for s in ("valid", "risky", "not_found")}
    print(f"\nDone -> {out_path}  {counts}")


def show_quota():
    conn = db()
    print(f"{'provider':<10} {'used':>5} {'limit':>6}  resets_on")
    for r in conn.execute("SELECT * FROM quotas"):
        print(f"{r['provider']:<10} {r['used']:>5} {r['monthly_limit']:>6}  {r['resets_on']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="process a CSV of leads")
    p_run.add_argument("csv")
    p_run.add_argument("-o", "--out", default="results.csv")
    sub.add_parser("quota", help="show quota usage")
    args = ap.parse_args()
    if args.cmd == "run":
        if not os.getenv("REOON_API_KEY"):
            sys.exit("REOON_API_KEY is missing, set it in .env")
        run(args.csv, args.out)
    else:
        show_quota()
