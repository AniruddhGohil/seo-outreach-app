"""
brevo_sender.py – Send transactional emails via Brevo (formerly Sendinblue).

Free tier: 300 emails/day · unlimited contacts · open & click tracking included.
Sign up at: https://app.brevo.com  →  Settings → API Keys → Create API Key

Why Brevo over raw Gmail SMTP:
  • Dedicated sending infrastructure → better inbox placement
  • Automatic open & click tracking pixel injected by Brevo
  • Bounce / unsubscribe handling — complainers removed automatically
  • Contact CRM synced with every send
  • 300/day free vs Gmail's rate-limit throttling
"""
import requests
from typing import Optional

BREVO_BASE = "https://api.brevo.com/v3"


# ─────────────────────────────────────────────────────────────────────────────
# Account / key validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_key(api_key: str) -> dict:
    """
    Validate a Brevo API key and return account info.
    Returns {} on failure.
    Example returned keys: 'email', 'plan' (list), 'marketingCredits', 'smtpCredits'.
    """
    try:
        r = requests.get(
            f"{BREVO_BASE}/account",
            headers={"api-key": api_key},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Send a single transactional email
# ─────────────────────────────────────────────────────────────────────────────

def send_email(
    api_key: str,
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    recipient_name: str,
    subject: str,
    html_content: str,
    text_content: str,
    tags: Optional[list] = None,
    reply_to: Optional[str] = None,
) -> tuple:
    """
    Send one transactional email via Brevo.
    Returns (success: bool, message_id_or_error: str).

    Brevo automatically:
      - injects an open-tracking pixel
      - wraps links with click-tracking redirects
      - handles bounces and unsubscribes
    """
    headers = {
        "api-key":      api_key,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }
    payload = {
        "sender":      {"name": sender_name, "email": sender_email},
        "to":          [{"email": recipient_email, "name": recipient_name}],
        "replyTo":     {"email": reply_to or sender_email},
        "subject":     subject,
        "htmlContent": html_content,
        "textContent": text_content,
        "tags":        tags or ["seo-outreach"],
    }
    try:
        r = requests.post(
            f"{BREVO_BASE}/smtp/email",
            headers=headers,
            json=payload,
            timeout=20,
        )
        if r.status_code in (200, 201):
            message_id = r.json().get("messageId", "")
            return True, message_id
        return False, f"Brevo {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, f"Request error: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Contact management
# ─────────────────────────────────────────────────────────────────────────────

def upsert_contact(
    api_key: str,
    email: str,
    attributes: Optional[dict] = None,
    list_ids: Optional[list] = None,
) -> bool:
    """
    Create or update a contact in Brevo.
    Useful for building a CRM of all your leads inside Brevo's dashboard.
    """
    headers = {
        "api-key":      api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "email":         email,
        "attributes":    attributes or {},
        "listIds":       list_ids or [],
        "updateEnabled": True,   # update if contact already exists
    }
    try:
        r = requests.post(
            f"{BREVO_BASE}/contacts",
            headers=headers,
            json=payload,
            timeout=10,
        )
        return r.status_code in (200, 201, 204)
    except Exception:
        return False


def get_or_create_list(api_key: str, list_name: str = "SEO Outreach Leads") -> Optional[int]:
    """
    Return the Brevo list ID for list_name, creating it if it doesn't exist.
    Contacts are added to this list so you can see all your leads in Brevo.
    """
    headers = {"api-key": api_key, "Accept": "application/json"}
    try:
        # Fetch existing lists
        r = requests.get(f"{BREVO_BASE}/contacts/lists", headers=headers,
                         params={"limit": 50}, timeout=10)
        if r.status_code == 200:
            for lst in r.json().get("lists", []):
                if lst.get("name") == list_name:
                    return lst["id"]
        # Create new list
        r2 = requests.post(
            f"{BREVO_BASE}/contacts/lists",
            headers={**headers, "Content-Type": "application/json"},
            json={"name": list_name, "folderId": 1},
            timeout=10,
        )
        if r2.status_code in (200, 201):
            return r2.json().get("id")
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Open / click tracking
# ─────────────────────────────────────────────────────────────────────────────

def get_email_events(api_key: str, limit: int = 500) -> list:
    """
    Fetch recent transactional email events from Brevo.
    Each event dict has: 'email', 'event' (opened/clicked/bounced/…),
    'date', 'messageId', 'subject', 'tag'.
    """
    headers = {"api-key": api_key, "Accept": "application/json"}
    events = []
    try:
        for event_type in ["opened", "clicks", "hardBounces", "softBounces"]:
            r = requests.get(
                f"{BREVO_BASE}/smtp/statistics/events",
                headers=headers,
                params={"limit": limit, "event": event_type, "sort": "desc"},
                timeout=15,
            )
            if r.status_code == 200:
                events.extend(r.json().get("events", []))
    except Exception:
        pass
    return events


def get_aggregate_stats(api_key: str, days: int = 30) -> dict:
    """
    Return aggregate sent/opened/clicked/bounced counts for the last N days.
    """
    from datetime import datetime, timedelta
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    headers = {"api-key": api_key, "Accept": "application/json"}
    try:
        r = requests.get(
            f"{BREVO_BASE}/smtp/statistics/aggregatedReport",
            headers=headers,
            params={"startDate": start, "tag": "seo-outreach"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def get_today_stats(api_key: str) -> dict:
    """
    Return today's sent/delivered/opened/clicked counts only.
    Used for the daily budget tracker (300 emails/day free limit).
    """
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    headers = {"api-key": api_key, "Accept": "application/json"}
    try:
        r = requests.get(
            f"{BREVO_BASE}/smtp/statistics/aggregatedReport",
            headers=headers,
            params={"startDate": today, "endDate": today, "tag": "seo-outreach"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}
