"""
email_finder.py – Extract a contact email from a business website.

Strategy (in order):
  1. mailto: links on homepage & contact pages
  2. Regex over full page text
  3. Decode obfuscated emails ([at], (at), HTML entities)
  4. JSON-LD schema.org email field
  5. Meta tag email fields
  6. Raw HTML source regex (catches JS-embedded emails)
"""
import json
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
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_SKIP_DOMAINS = {
    "example.com", "test.com", "domain.com", "email.com",
    "sentry.io", "wixpress.com", "squarespace.com", "wordpress.com",
    "shopify.com", "amazonaws.com", "googletagmanager.com",
    "google.com", "facebook.com", "twitter.com", "instagram.com",
    "schema.org", "w3.org", "jquery.com", "cloudflare.com",
}
_SKIP_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
    "abuse", "spam", "bounce", "privacy", "legal",
}
_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".js", ".css", ".woff"}

_CONTACT_PATHS = [
    "/contact", "/contact-us", "/contactus", "/contact_us",
    "/about",   "/about-us",   "/aboutus",
    "/reach-us", "/get-in-touch", "/getintouch",
    "/team", "/our-team", "/staff",
    "/hello", "/enquiry", "/enquiries",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_email(raw: str) -> Optional[str]:
    email = raw.lower().strip().rstrip(".").rstrip(",")
    if not _EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@")[1]
    prefix = email.split("@")[0]
    if domain in _SKIP_DOMAINS:
        return None
    if prefix in _SKIP_PREFIXES:
        return None
    if any(email.endswith(ext) for ext in _ASSET_EXTS):
        return None
    # Must have a real TLD (at least 2 chars after last dot)
    if len(domain.split(".")[-1]) < 2:
        return None
    return email


def _decode_obfuscated(text: str) -> str:
    """Decode common email obfuscation patterns."""
    text = re.sub(r"\s*\[at\]\s*",   "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(at\)\s*",   "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\{at\}\s*",   "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[ at \]\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[dot\]\s*",  ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(dot\)\s*",  ".", text, flags=re.IGNORECASE)
    text = text.replace("&#64;", "@").replace("&#46;", ".").replace("&amp;", "&")
    return text


def _fetch(url: str, timeout: int = 12) -> Optional[str]:
    try:
        headers = {
            "User-Agent":      random.choice(_USER_AGENTS),
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and "text/html" in ct:
            return r.text
    except Exception:
        pass
    return None


def _emails_from_page(html: str) -> list:
    """
    Try every technique to extract emails from a page.
    Returns deduplicated list, best candidates first.
    """
    soup   = BeautifulSoup(html, "lxml")
    found  = []

    def _add(e):
        c = _clean_email(e)
        if c and c not in found:
            found.append(c)

    # 1. mailto: href links (most reliable)
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        raw = a["href"].replace("mailto:", "").split("?")[0].strip()
        _add(raw)

    # 2. JSON-LD schema.org (many business sites include email here)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                for key in ("email", "contactEmail"):
                    val = data.get(key, "")
                    if val:
                        _add(val.replace("mailto:", ""))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        for key in ("email", "contactEmail"):
                            val = item.get(key, "")
                            if val:
                                _add(val.replace("mailto:", ""))
        except Exception:
            pass

    # 3. Meta tags
    for meta in soup.find_all("meta"):
        name    = (meta.get("name", "") or meta.get("property", "")).lower()
        content = meta.get("content", "")
        if "email" in name and content:
            _add(content)

    # 4. Regex on visible text (catches plain-text emails)
    for raw in _EMAIL_RE.findall(soup.get_text(separator=" ")):
        _add(raw)

    # 5. Obfuscated emails in full page text
    decoded_text = _decode_obfuscated(soup.get_text(separator=" "))
    for raw in _EMAIL_RE.findall(decoded_text):
        _add(raw)

    # 6. Raw HTML source (catches JS-embedded strings like "email":"x@y.com")
    for raw in _EMAIL_RE.findall(html):
        _add(raw)

    return found


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_email_on_website(website_url: str) -> Optional[str]:
    """
    Visit a business website and return the best contact email found,
    or None if nothing usable is found.
    """
    if not website_url:
        return None

    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    try:
        parsed = urlparse(website_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None

    pages_to_try = [website_url] + [urljoin(base, p) for p in _CONTACT_PATHS]
    seen: set    = set()

    for url in pages_to_try:
        if url in seen:
            continue
        seen.add(url)

        html = _fetch(url)
        if not html:
            continue

        emails = _emails_from_page(html)
        if emails:
            return emails[0]

        time.sleep(random.uniform(0.3, 0.8))

    return None
