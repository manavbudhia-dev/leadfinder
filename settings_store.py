"""UI-saved settings (API keys, limits, flags) persisted in SQLite so they
survive restarts and container redeploys. Sensitive values SECRET_KEY-derived
Fernet key se encrypt hote hain (env SECRET_KEY stable hona chahiye).
Bina SECRET_KEY ke (dev mode) plaintext store hota hai.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

import lead_pipeline as lp

SETTING_KEYS = ["REOON_API_KEY", "HUNTER_API_KEY", "PROSPEO_API_KEY", "APOLLO_API_KEY"]
LIMIT_KEYS = [f"{name.upper()}_LIMIT" for name, _, _, _ in lp.PROVIDERS]
SENSITIVE = set(SETTING_KEYS) | {"WEBHOOK_URL", "OUTREACH_API_KEY"}
ENC_PREFIX = "enc:"


def _fernet():
    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _encrypt(key, value):
    f = _fernet()
    if f is None or key not in SENSITIVE or not value:
        return value
    return ENC_PREFIX + f.encrypt(value.encode()).decode()


def _decrypt(value):
    if not value.startswith(ENC_PREFIX):
        return value  # legacy plaintext ya non-sensitive
    f = _fernet()
    if f is None:
        return ""  # SECRET_KEY badal gaya — value recover nahi ho sakti
    try:
        return f.decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


def _ensure_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def save(values):
    conn = lp.db()
    try:
        _ensure_table(conn)
        for key, val in values.items():
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, _encrypt(key, str(val))))
        conn.commit()
    finally:
        conn.close()


def load_into_env():
    conn = lp.db()
    try:
        _ensure_table(conn)
        conn.commit()
        for row in conn.execute("SELECT key, value FROM settings"):
            val = _decrypt(row["value"])
            if val:
                os.environ[row["key"]] = val
    finally:
        conn.close()