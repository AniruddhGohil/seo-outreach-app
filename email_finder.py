"""
email_finder.py – Extract a contact email from a business website.

Strategy:
  1. Visit the homepage and check for mailto: links.
  2. Crawl up to 5 common contact-page paths.
  3. Fall back to regex search of page text.
"""
import random
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Email addresses we never want to return
_SKIP_DOMAINS = {
    "example.com", "test.com", "domain.com", "email.com",
    "sentry.io", "wixpress.com", "squarespace.com",
    "wordpress.com", "shopify.com", "amazonaws.com",
}
_SKIP_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster", "admin",
    "abuse", "spam", "bounce", "support",          # keep 'support' borderline; remove if needed
}

_CONTACT_PATHS = [
    "/contact", "/contact-us", "/contactus",
    "/about", "/about-us", "/reach-us", "/get-in-touch",
]


def _clean_email(raw: str) -> Optional[str]:
    email = raw.lower().strip().rstrip(".")
    if not _EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@")[1]
    prefix = email.split("@")[0]
    if domain in _SKIP_DOMAINS:
        return None
    if prefix in _SKIP_PREFIXES:
        return None
    # Reject if looks like a file path (image/asset embedded in text)
    if any(email.endswith(ext) for ext in (".png", ".jpg", ".gif", ".svg", ".js", ".css")):
        return None
    return email


def _fetch(url: str, timeout: int = 10) -> Optional[str]:
    try:
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and "text/html" in ct:
            return r.text
    except Exception:
        pass
    return None


def _emails_from_page(html: str) -> list:
    """Extract valid emails from raw HTML; prioritise mailto: hrefs."""
    soup = BeautifulSoup(html, "lxml")

    found = []
    # 1. mailto: links (most reliable)
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        raw = a["href"].replace("mailto:", "").split("?")[0]
        cleaned = _clean_email(raw)
        if cleaned and cleaned not in found:
            found.append(cleaned)

    # 2. Regex over visible text (catches obfuscated emails written as plain text)
    for raw in _EMAIL_RE.findall(soup.get_text()):
        cleaned = _clean_email(raw)
        if cleaned and cleaned not in found:
            found.append(cleaned)

    return found


def find_email_on_website(website_url: str) -> Optional[str]:
    """
    Visit a business website and return the best contact email found,
    or None if nothing usable is found.
    """
    if not website_url:
        return None

    # Normalise URL
    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    try:
        parsed = urlparse(website_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None

    pages_to_try = [website_url] + [urljoin(base, p) for p in _CONTACT_PATHS]
    seen: set = set()

    for url in pages_to_try:
        if url in seen:
            continue
        seen.add(url)

        html = _fetch(url)
        if not html:
            continue

        emails = _emails_from_page(html)
        if emails:
            return emails[0]   # return first / best hit

        time.sleep(random.uniform(0.4, 1.2))

    return None
