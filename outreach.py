"""Outreach push: verified leads ko Instantly / Smartlead campaign mein bhejo."""
import os

import requests

TIMEOUT = 30


def push_leads(leads):
    """leads: [{email, first_name, last_name, domain}] -> (ok, info)"""
    provider = os.getenv("OUTREACH_PROVIDER", "instantly").lower()
    key = os.getenv("OUTREACH_API_KEY", "").strip()
    campaign = os.getenv("OUTREACH_CAMPAIGN", "").strip()
    if not key or not campaign:
        return False, "Outreach API key aur campaign ID set karo (Automation tab)."
    if not leads:
        return False, "Koi valid lead nahi hai push karne ke liye."

    if provider == "instantly":
        r = requests.post(
            "https://api.instantly.ai/api/v1/lead/add",
            json={
                "api_key": key,
                "campaign_id": campaign,
                "skip_if_in_workspace": True,
                "leads": [{"email": x["email"], "first_name": x["first_name"],
                           "last_name": x["last_name"], "company_domain": x["domain"]} for x in leads],
            },
            timeout=TIMEOUT,
        )
    elif provider == "smartlead":
        r = requests.post(
            f"https://server.smartlead.ai/api/v1/campaigns/{campaign}/leads",
            params={"api_key": key},
            json={
                "lead_list": [{"email": x["email"], "first_name": x["first_name"],
                               "last_name": x["last_name"], "company_name": x["domain"]} for x in leads],
                "settings": {"ignore_global_block_list": False, "ignore_community_bounce_list": True},
            },
            timeout=TIMEOUT,
        )
    else:
        return False, f"Unknown outreach provider: {provider}"

    if not r.ok:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    return True, f"{len(leads)} leads {provider} campaign mein push ho gaye."