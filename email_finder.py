"""
email_finder.py – Extract a contact email from a business website.

Techniques used (in order of reliability):
  1. Cloudflare email-protection decoder (very common on SMB sites)
  2. mailto: href links
  3. JSON-LD schema.org email field
  4. Meta tag email fields
  5. Regex on visible text + obfuscation decoding ([at], (at), HTML entities)
  6. Raw HTML source regex
  7. Common pattern guessing (info@, contact@, hello@) as last resort
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
    "gravatar.com", "googleapis.com", "gstatic.com", "apple.com",
}
_SKIP_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
    "abuse", "spam", "bounce", "privacy", "legal", "unsubscribe",
}
_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".js", ".css", ".woff"}

_CONTACT_PATHS = [
    "/contact", "/contact-us", "/contactus", "/contact_us",
    "/about",   "/about-us",   "/aboutus",
    "/reach-us", "/get-in-touch", "/getintouch",
    "/team", "/our-team", "/staff", "/hello",
    "/enquiry", "/enquiries", "/info",
]

# Common email prefixes used by small businesses
_COMMON_PREFIXES = [
    "info", "contact", "hello", "enquiries", "enquiry",
    "office", "mail", "admin", "sales",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_email(raw: str) -> Optional[str]:
    email = raw.lower().strip().rstrip(".,;")
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
    if len(domain.split(".")[-1]) < 2:
        return None
    return email


def _decode_cloudflare_email(encoded: str) -> str:
    """
    Decode Cloudflare's __cf_email__ hex-encoded email protection.
    Many SMB websites use Cloudflare which hides emails this way.
    """
    try:
        key = int(encoded[:2], 16)
        email = "".join(
            chr(int(encoded[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded), 2)
        )
        return email
    except Exception:
        return ""


def _decode_obfuscated(text: str) -> str:
    text = re.sub(r"\s*\[at\]\s*",   "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(at\)\s*",   "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\{at\}\s*",   "@", text, flags=re.IGNORECASE)
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
    soup  = BeautifulSoup(html, "lxml")
    found = []

    def _add(e: str):
        c = _clean_email(e)
        if c and c not in found:
            found.append(c)

    # ── 1. Cloudflare email protection (very common on SMB sites) ────────────
    for el in soup.select(".__cf_email__, [data-cfemail]"):
        encoded = el.get("data-cfemail", "")
        if encoded:
            _add(_decode_cloudflare_email(encoded))
    # Also check anchor hrefs for CF protection
    for a in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection")):
        encoded = a.get("data-cfemail", "")
        if encoded:
            _add(_decode_cloudflare_email(encoded))

    # ── 2. mailto: links ─────────────────────────────────────────────────────
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        raw = a["href"].replace("mailto:", "").split("?")[0].strip()
        _add(raw)

    # ── 3. JSON-LD schema.org ────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    for key in ("email", "contactEmail", "contactPoint"):
                        val = item.get(key, "")
                        if isinstance(val, str) and val:
                            _add(val.replace("mailto:", ""))
                        elif isinstance(val, dict):
                            _add(val.get("email", "").replace("mailto:", ""))
        except Exception:
            pass

    # ── 4. Meta tags ─────────────────────────────────────────────────────────
    for meta in soup.find_all("meta"):
        name    = (meta.get("name", "") or meta.get("property", "")).lower()
        content = meta.get("content", "")
        if "email" in name and content:
            _add(content)

    # ── 5. Regex on visible text + obfuscation decoding ──────────────────────
    page_text = soup.get_text(separator=" ")
    for raw in _EMAIL_RE.findall(page_text):
        _add(raw)
    for raw in _EMAIL_RE.findall(_decode_obfuscated(page_text)):
        _add(raw)

    # ── 6. Raw HTML source (catches JS-embedded strings) ─────────────────────
    for raw in _EMAIL_RE.findall(html):
        _add(raw)

    return found


def _guess_email_from_domain(domain: str) -> Optional[str]:
    """
    Last resort: return the most common email pattern for a business domain.
    Marked as guessed so user is aware.
    """
    for prefix in _COMMON_PREFIXES:
        candidate = f"{prefix}@{domain}"
        c = _clean_email(candidate)
        if c:
            return c
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_email_on_website(
    website_url: str,
    use_guess_fallback: bool = True,
) -> tuple:
    """
    Visit a business website and return (email, source) where source is
    'found' (extracted from site) or 'guessed' (common pattern fallback).
    Returns (None, None) if nothing found.
    """
    if not website_url:
        return None, None

    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    try:
        parsed = urlparse(website_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"
        domain = parsed.netloc.lstrip("www.")
    except Exception:
        return None, None

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
            return emails[0], "found"

        time.sleep(random.uniform(0.3, 0.7))

    # ── Last resort: guess common pattern ────────────────────────────────────
    if use_guess_fallback and domain:
        guessed = _guess_email_from_domain(domain)
        if guessed:
            return guessed, "guessed"

    return None, None
