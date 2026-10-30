"""LinkedIn URL se email lookup (Prospeo / Apollo), quota + verify ke saath.
lead_pipeline ka waterfall reuse hota hai verify/quota helpers ke liye.
"""
import os

import requests

import lead_pipeline as lp

TIMEOUT = 30


def _prospeo(url):
    r = requests.post(
        "https://api.prospeo.io/enrich-person",
        headers={"X-KEY": os.environ["PROSPEO_API_KEY"], "Content-Type": "application/json"},
        json={"only_verified_email": True, "enrich_mobile": False,
              "data": {"linkedin_url": url}},
        timeout=TIMEOUT,
    )
    if r.status_code == 429:
        raise lp.QuotaExhausted("prospeo")
    try:
        body = r.json()
    except ValueError:
        return None
    if body.get("error"):
        if "CREDIT" in str(body.get("error_code", "")).upper():
            raise lp.QuotaExhausted("prospeo")
        return None
    person = body.get("person") or {}
    email = person.get("email")
    if isinstance(email, dict):
        email = email.get("email")
    return {"email": email, "first_name": person.get("first_name", ""),
            "last_name": person.get("last_name", ""),
            "domain": (person.get("company_website") or "").strip()} if email else None


def _apollo(url):
    r = requests.post(
        "https://api.apollo.io/api/v1/people/match",
        headers={"x-api-key": os.environ["APOLLO_API_KEY"], "Content-Type": "application/json"},
        json={"linkedin_url": url},
        timeout=TIMEOUT,
    )
    if r.status_code in (402, 429):
        raise lp.QuotaExhausted("apollo")
    if r.status_code != 200:
        return None
    person = r.json().get("person") or {}
    email = person.get("email")
    if not email or any(p in email for p in lp.PLACEHOLDER_EMAILS):
        return None
    org = person.get("organization") or {}
    return {"email": email, "first_name": person.get("first_name", ""),
            "last_name": person.get("last_name", ""),
            "domain": (org.get("website_url") or "").strip()}


LOOKUP = [("prospeo", _prospeo, "PROSPEO_API_KEY"), ("apollo", _apollo, "APOLLO_API_KEY")]


def find_by_linkedin(conn, url):
    """Returns (person_dict, status, source, log_lines). person None = not found."""
    tried, fallback, logs = set(), None, []
    for name, func, env in LOOKUP:
        if not os.getenv(env):
            continue
        if not lp.has_quota(conn, name):
            logs.append(f"  - {name}: quota used up, skipping")
            continue
        try:
            person = func(url)
        except lp.QuotaExhausted:
            lp.mark_exhausted(conn, name)
            logs.append(f"  - {name}: API reports no credits left")
            continue
        except requests.RequestException as exc:
            logs.append(f"  - {name}: network error ({exc})")
            continue
        if not person:
            logs.append(f"  - {name}: no match")
            continue
        lp.bump(conn, name)
        email = person["email"].strip().lower()
        person["email"] = email
        if email in tried:
            continue
        tried.add(email)
        status = lp.verify(email)
        logs.append(f"  - {name}: {email} -> {status}")
        if status in lp.VALID:
            return person, "valid", name, logs
        if status in lp.RISKY and fallback is None:
            fallback = (person, "risky", name)
    if fallback:
        return fallback[0], fallback[1], fallback[2], logs
    return None, "not_found", None, logs