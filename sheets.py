"""Google Sheets writeback via service account (koi extra SDK nahi).

GOOGLE_SERVICE_ACCOUNT_JSON env: service-account JSON string ya file path.
Sheet ko service-account email se share karo (Editor), phir SHEET_ID use karo.
"""
import base64
import json
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _service_account():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    with open(raw, encoding="utf-8") as f:
        return json.load(f)


def access_token():
    sa = _service_account()
    if sa is None:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON set nahi hai.")
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({
        "iss": sa["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
        "iat": now, "exp": now + 3600,
    }).encode())
    signing_input = header + b"." + claims
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt = (signing_input + b"." + _b64url(sig)).decode()
    r = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def append_rows(sheet_id, rows):
    """rows: list of lists (pehli row header ho sakti hai). -> (ok, info)"""
    if not sheet_id:
        return False, "Sheet ID set nahi hai."
    try:
        token = access_token()
        r = requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:append",
            params={"valueInputOption": "RAW"},
            headers={"Authorization": f"Bearer {token}"},
            json={"values": rows},
            timeout=30,
        )
        if not r.ok:
            return False, f"Sheets HTTP {r.status_code}: {r.text[:200]}"
        return True, f"{len(rows)} rows sheet mein append ho gaye."
    except (ValueError, OSError, requests.RequestException) as exc:
        return False, str(exc)