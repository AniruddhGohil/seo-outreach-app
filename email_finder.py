"""
email_finder.py – Extract a real contact email from a business website.

Extraction techniques (in order of reliability):
  1.  Cloudflare email-protection decoder (__cf_email__)
  2.  mailto: href links
  3.  JSON-LD schema.org email / contactPoint fields
  4.  Meta tag email fields (og:email, meta name="email")
  5.  <address> and <footer> tags (semantically high-signal)
  6.  onclick / JS string mailto: extraction
  7.  data-email / data-mail / data-contact attributes
  8.  Regex near contact-keyword context windows (±300 chars)
  9.  Obfuscation decoding ([at], (at), {at}, "name at domain dot com")
 10.  Regex on all visible page text
 11.  Raw HTML source regex
 12.  Homepage internal-link discovery → follow contact/about links
 13.  Sitemap.xml discovery of extra contact/about pages
 14.  Email ranking: prefer owner-name emails over generic info@
 15.  Guess fallback (info@, contact@) ONLY if domain confirmed live
      and at least one page was actually fetched.
"""
import json
import random
import re
import time
from typing import Optional, List, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Domains that are never real business contacts
_SKIP_DOMAINS = {
    "example.com", "test.com", "domain.com", "email.com",
    "sentry.io", "wixpress.com", "squarespace.com", "wordpress.com",
    "shopify.com", "amazonaws.com", "googletagmanager.com",
    "google.com", "google.co.uk", "google.com.au",
    "facebook.com", "twitter.com", "instagram.com", "x.com",
    "schema.org", "w3.org", "jquery.com", "cloudflare.com",
    "gravatar.com", "googleapis.com", "gstatic.com", "apple.com",
    "youtube.com", "linkedin.com", "pinterest.com", "tiktok.com",
    "whatsapp.com", "mailchimp.com", "hubspot.com", "zendesk.com",
    "constantcontact.com", "sendgrid.com", "mailgun.com",
    "campaignmonitor.com", "activecampaign.com",
    "wpengine.com", "godaddy.com", "bluehost.com", "siteground.com",
    "elementor.com", "wp.com", "weebly.com", "webflow.com",
    "yoast.com", "ahrefs.com", "semrush.com", "moz.com",
}

# Email prefixes that are system/noreply — skip these
_SKIP_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
    "abuse", "spam", "bounce", "privacy", "legal", "unsubscribe",
    "notifications", "newsletter", "support-noreply", "auto-reply",
    "feedback", "daemon", "root", "hostmaster",
}

# Generic email prefixes — still valid but ranked lower than personal ones
_GENERIC_PREFIXES = {
    "info", "contact", "hello", "enquiries", "enquiry",
    "office", "mail", "admin", "sales", "team", "general",
    "reception", "service", "services", "help", "support",
}

_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".js", ".css",
               ".woff", ".woff2", ".ttf", ".eot", ".ico", ".pdf", ".zip"}

# Ordered by expected yield — tried after homepage
_CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/contactus",
    "/contact_us",
    "/contact.html",
    "/contact.php",
    "/contact.asp",
    "/about",
    "/about-us",
    "/aboutus",
    "/about.html",
    "/reach-us",
    "/get-in-touch",
    "/getintouch",
    "/reach-out",
    "/connect",
    "/email-us",
    "/write-to-us",
    "/enquiry",
    "/enquiries",
    "/info",
    "/team",
    "/our-team",
    "/staff",
    "/hello",
    "/help",
    "/support",
    "/pages/contact",        # Shopify
    "/pages/about",
    "/pages/contact-us",
    "/pages/get-in-touch",
    "/en/contact",           # Multilingual sites
    "/en/about",
]

# Common email prefixes for guess fallback (ordered by likelihood)
_COMMON_PREFIXES = [
    "info", "contact", "hello", "enquiries", "office",
    "mail", "admin", "sales", "team",
]

# Keywords whose nearby text very likely contains an email
_CONTACT_KEYWORDS = re.compile(
    r"(e[\s\-]?mail|contact|reach\s+us|write\s+to|get\s+in\s+touch|"
    r"send\s+us|drop\s+us|email\s+us|reach\s+out|enquir|get\s+a\s+quote)",
    re.IGNORECASE,
)

# Patterns for spelled-out obfuscation: "john at domain dot com"
_SPELLED_AT  = re.compile(r"(\w[\w.\-]+)\s+at\s+([\w\-]+)\s+dot\s+([\w]{2,6})",
                           re.IGNORECASE)
_SPELLED_AT2 = re.compile(r"(\w[\w.\-]+)\s*\[at\]\s*([\w\-]+)\s*\[dot\]\s*([\w]{2,6})",
                           re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_email(raw: str) -> Optional[str]:
    email = raw.lower().strip().rstrip(".,;:>\"'/\\)")
    email = email.lstrip("<\"'(")
    if not _EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@")[1]
    prefix = email.split("@")[0]
    if any(domain == d or domain.endswith("." + d) for d in _SKIP_DOMAINS):
        return None
    if prefix in _SKIP_PREFIXES:
        return None
    if any(email.endswith(ext) for ext in _ASSET_EXTS):
        return None
    if len(domain.split(".")[-1]) < 2:
        return None
    # Skip CSS selectors, template strings
    if re.search(r"[{}()\[\]#]", email):
        return None
    # Skip emails that look like version strings (e.g. 1.0@something)
    if re.match(r"^\d+[\d.]*@", email):
        return None
    # Skip very long prefixes (likely junk)
    if len(prefix) > 40:
        return None
    return email


def _rank_email(email: str) -> int:
    """
    Lower score = better (return first).
    Personal/direct emails ranked above generic info@ addresses.
    """
    prefix = email.split("@")[0]
    if prefix in _GENERIC_PREFIXES:
        return 2    # generic but valid
    if prefix in _SKIP_PREFIXES:
        return 99   # should already be filtered, but just in case
    return 1        # personal / named email — best


def _decode_cloudflare_email(encoded: str) -> str:
    try:
        key   = int(encoded[:2], 16)
        email = "".join(
            chr(int(encoded[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded), 2)
        )
        return email
    except Exception:
        return ""


def _decode_obfuscated(text: str) -> str:
    # Standard bracket/paren obfuscation
    text = re.sub(r"\s*\[at\]\s*",  "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(at\)\s*",  "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\{at\}\s*",  "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[dot\]\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(dot\)\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\{dot\}\s*", ".", text, flags=re.IGNORECASE)
    # HTML entities
    text = text.replace("&#64;", "@").replace("&#46;", ".")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("@", "@")   # unicode @
    # Spelled-out: "john at domain dot com"
    text = _SPELLED_AT.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)
    text = _SPELLED_AT2.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)
    return text


def _fetch(url: str, timeout: int = 12) -> Optional[str]:
    try:
        headers = {
            "User-Agent":      random.choice(_USER_AGENTS),
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer":         "https://www.google.com/",
            "DNT":             "1",
        }
        r = requests.get(url, headers=headers, timeout=timeout,
                         allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ("text/html" in ct or "xml" in ct):
            return r.text
    except Exception:
        pass
    return None


def _emails_from_soup(soup: BeautifulSoup, html: str) -> List[str]:
    """
    Extract ALL valid contact emails from a parsed page.
    Returns a deduplicated list ordered: personal > generic > guessed.
    """
    raw_found: List[str] = []

    def _add(e: str):
        c = _clean_email(e)
        if c and c not in raw_found:
            raw_found.append(c)

    # ── 1. Cloudflare email protection ──────────────────────────────────────
    for el in soup.select(".__cf_email__, [data-cfemail]"):
        encoded = el.get("data-cfemail", "")
        if encoded:
            _add(_decode_cloudflare_email(encoded))
    for a in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection")):
        encoded = a.get("data-cfemail", "")
        if encoded:
            _add(_decode_cloudflare_email(encoded))
    # Also look in raw HTML for cf_email patterns
    for encoded in re.findall(r'data-cfemail="([a-f0-9]+)"', html, re.IGNORECASE):
        _add(_decode_cloudflare_email(encoded))

    # ── 2. mailto: links ─────────────────────────────────────────────────────
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        raw = a["href"].replace("mailto:", "").split("?")[0].strip()
        _add(raw)

    # ── 3. JSON-LD schema.org ─────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data  = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("email", "contactEmail"):
                    val = item.get(key, "")
                    if isinstance(val, str) and val:
                        _add(val.replace("mailto:", ""))
                # contactPoint may be list or dict
                for cp in (item.get("contactPoint") or []):
                    if isinstance(cp, dict):
                        _add(cp.get("email", "").replace("mailto:", ""))
                    elif isinstance(cp, str):
                        _add(cp.replace("mailto:", ""))
                # author / creator blocks
                for key in ("author", "creator", "founder", "employee"):
                    person = item.get(key)
                    if isinstance(person, dict):
                        _add(person.get("email", "").replace("mailto:", ""))
        except Exception:
            pass

    # ── 4. Meta tags ─────────────────────────────────────────────────────────
    for meta in soup.find_all("meta"):
        name    = (meta.get("name", "") or meta.get("property", "")).lower()
        content = meta.get("content", "")
        if any(k in name for k in ("email", "contact", "author")) and content:
            _add(content)

    # ── 5. <address> tags ───────────────────────────────────────────────────
    for addr in soup.find_all("address"):
        text = _decode_obfuscated(addr.get_text(separator=" "))
        for raw in _EMAIL_RE.findall(text):
            _add(raw)
        for a in addr.find_all("a", href=re.compile(r"^mailto:", re.I)):
            _add(a["href"].replace("mailto:", "").split("?")[0].strip())

    # ── 6. <footer> and footer-like divs ─────────────────────────────────────
    for footer in soup.find_all(["footer", "div"],
                                 class_=re.compile(
                                     r"footer|bottom|contact|widget|sidebar",
                                     re.I)):
        text = _decode_obfuscated(footer.get_text(separator=" "))
        for raw in _EMAIL_RE.findall(text):
            _add(raw)
        for a in footer.find_all("a", href=re.compile(r"^mailto:", re.I)):
            _add(a["href"].replace("mailto:", "").split("?")[0].strip())

    # ── 7. onclick / JavaScript mailto strings ────────────────────────────────
    for tag in soup.find_all(onclick=True):
        onclick = tag.get("onclick", "")
        for raw in re.findall(r"mailto:([^\s\"'\\?]+)", onclick, re.IGNORECASE):
            _add(raw)
    # Also scan inline <script> blocks for mailto strings
    for script in soup.find_all("script"):
        js = script.string or ""
        for raw in re.findall(r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
                               js, re.IGNORECASE):
            _add(raw)
        # email strings assigned to variables: var email = "x@y.com"
        for raw in re.findall(
            r"""(?:email|mail|contact)\s*[:=]\s*["']([^"'@\s]+@[^"'\s]+)["']""",
            js, re.IGNORECASE,
        ):
            _add(raw)

    # ── 8. data-email / data-mail / data-contact attributes ──────────────────
    for tag in soup.find_all(True):
        for attr in ("data-email", "data-mail", "data-contact",
                     "data-mailto", "data-address"):
            val = tag.get(attr, "")
            if val and "@" in val:
                _add(val)

    # ── 9. Emails near contact-keyword context windows (±300 chars) ──────────
    page_text    = soup.get_text(separator=" ")
    decoded_text = _decode_obfuscated(page_text)
    for m in _CONTACT_KEYWORDS.finditer(decoded_text):
        start   = max(0, m.start() - 80)
        end     = min(len(decoded_text), m.end() + 300)
        snippet = decoded_text[start:end]
        for raw in _EMAIL_RE.findall(snippet):
            _add(raw)

    # ── 10. Full visible text regex (catches anything missed above) ───────────
    for raw in _EMAIL_RE.findall(decoded_text):
        _add(raw)

    # ── 11. Raw HTML source (JS strings, data attrs, comments) ───────────────
    decoded_html = _decode_obfuscated(html)
    for raw in _EMAIL_RE.findall(decoded_html):
        _add(raw)

    # ── Rank: personal emails first, then generic ─────────────────────────────
    return sorted(raw_found, key=_rank_email)


def _discover_contact_links_from_homepage(soup: BeautifulSoup,
                                          base: str) -> List[str]:
    """
    Scan the homepage for internal links that look like contact/about pages.
    Returns absolute URLs not already in _CONTACT_PATHS.
    """
    contact_like = re.compile(
        r"(contact|about|reach|team|enquir|connect|hello|email|touch|staff|"
        r"getintouch|get-in-touch|write|support|find-us|find_us|location)",
        re.IGNORECASE,
    )
    found: List[str] = []
    seen_paths = set(_CONTACT_PATHS)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # Skip external, anchors, js, mail links
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "http")):
            continue
        if contact_like.search(href) or contact_like.search(a.get_text()):
            abs_url = urljoin(base, href)
            path    = urlparse(abs_url).path
            if path not in seen_paths and abs_url not in found:
                found.append(abs_url)
                seen_paths.add(path)
    return found[:8]   # cap to avoid over-crawling


def _discover_contact_pages_from_sitemap(base: str) -> List[str]:
    contact_like = re.compile(
        r"(contact|about|reach|team|enquir|connect|hello|email|touch|staff)",
        re.IGNORECASE,
    )
    extra: List[str] = []
    for sitemap_url in [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]:
        xml = _fetch(sitemap_url, timeout=8)
        if not xml:
            continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.DOTALL)
        for loc in locs:
            if contact_like.search(loc):
                extra.append(loc.strip())
        if extra:
            break
    return extra[:8]


def _guess_email_from_domain(domain: str) -> Optional[str]:
    """Last resort: return info@domain. Only called when domain is confirmed live."""
    candidate = f"info@{domain}"
    return _clean_email(candidate)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def find_email_on_website(
    website_url: str,
    use_guess_fallback: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Visit a business website and return (email, source):
      source = 'found'  → extracted from real page content
      source = 'guessed' → info@ pattern, only if domain confirmed live

    Strategy:
      1. Fetch homepage → extract emails + discover contact links
      2. Try standard contact paths
      3. Try sitemap-discovered contact pages
      4. If still nothing and domain was live → guess info@domain ONLY
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

    domain_is_live   = False   # True once we get any successful HTTP response
    extra_from_home:  List[str] = []

    # ── Phase 1: Homepage ────────────────────────────────────────────────────
    html = _fetch(website_url)
    if html:
        domain_is_live = True
        soup   = BeautifulSoup(html, "lxml")
        emails = _emails_from_soup(soup, html)
        if emails:
            return emails[0], "found"

        # Discover contact links from the homepage nav/footer
        extra_from_home = _discover_contact_links_from_homepage(soup, base)

    # ── Phase 2: Standard contact paths + homepage-discovered links ──────────
    standard_pages = [urljoin(base, p) for p in _CONTACT_PATHS]
    # Put homepage-discovered links first (higher signal)
    pages_to_try = extra_from_home + [p for p in standard_pages
                                       if p not in extra_from_home]

    seen: set = {website_url}
    for url in pages_to_try:
        if url in seen:
            continue
        seen.add(url)

        html = _fetch(url)
        if not html:
            continue

        domain_is_live = True
        soup   = BeautifulSoup(html, "lxml")
        emails = _emails_from_soup(soup, html)
        if emails:
            return emails[0], "found"

        time.sleep(random.uniform(0.2, 0.5))

    # ── Phase 3: Sitemap discovery ───────────────────────────────────────────
    try:
        sitemap_pages = _discover_contact_pages_from_sitemap(base)
        for url in sitemap_pages:
            if url in seen:
                continue
            seen.add(url)
            html = _fetch(url)
            if not html:
                continue
            domain_is_live = True
            soup   = BeautifulSoup(html, "lxml")
            emails = _emails_from_soup(soup, html)
            if emails:
                return emails[0], "found"
            time.sleep(random.uniform(0.2, 0.4))
    except Exception:
        pass

    # ── Phase 4: Guess ONLY if domain was confirmed live ─────────────────────
    # If the domain never responded, guessing is pointless (will 100% bounce).
    if use_guess_fallback and domain_is_live:
        guessed = _guess_email_from_domain(domain)
        if guessed:
            return guessed, "guessed"

    return None, None
