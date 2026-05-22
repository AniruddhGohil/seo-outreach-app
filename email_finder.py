"""
email_finder.py – Extract a real contact email from a business website.

Extraction techniques (in priority order):
  1.  URL variation retry  (https→http, www↔non-www)
  2.  Cloudflare email-protection decoder (__cf_email__)
  3.  mailto: href links
  4.  JSON-LD schema.org email / contactPoint / author fields
  5.  Meta tag email fields (og:email, meta name="email")
  6.  <address> and <footer> tags (semantically high-signal)
  7.  Elements with contact-like class/id names
  8.  onclick / JS string mailto: extraction
  9.  data-email / data-mail / data-contact attributes
 10.  HTML comments (devs sometimes leave test emails there)
 11.  Regex near contact-keyword context windows (±300 chars)
 12.  Obfuscation decoding ([at], (at), {at}, base64, "name at domain dot com")
 13.  Regex on all visible page text
 14.  Raw HTML source regex
 15.  Homepage internal-link discovery → follow contact/about links
 16.  robots.txt → sitemap discovery
 17.  Sitemap.xml discovery of extra contact/about/privacy pages
 18.  Email ranking: prefer owner-name emails over generic info@
 19.  MX record validation via Google DNS-over-HTTPS (filters dead domains)
 20.  Name-based email inference from team/about page (last resort, marked 'inferred')
"""
import json
import random
import re
import time
from typing import Optional, List, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# File extensions that are never valid email TLDs — image/media/code filenames
# that contain '@' in their path get matched by the regex otherwise.
_INVALID_EMAIL_EXTENSIONS = {
    # Images
    "webp", "jpg", "jpeg", "png", "gif", "svg", "ico", "bmp", "tiff", "tif",
    "avif", "heic", "heif", "raw",
    # Fonts
    "woff", "woff2", "ttf", "otf", "eot",
    # Scripts / styles
    "js", "jsx", "ts", "tsx", "css", "scss", "less", "map",
    # Documents / data
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "xml", "json",
    # Media
    "mp4", "mp3", "mov", "avi", "webm", "ogg", "wav", "flac",
    # Archives
    "zip", "gz", "tar", "rar", "7z",
    # Misc web assets
    "php", "asp", "aspx", "html", "htm",
}

# Domains that are never real business contacts
_SKIP_DOMAINS = {
    "example.com", "test.com", "domain.com", "email.com",
    "sentry.io", "wixpress.com", "squarespace.com", "wordpress.com",
    "shopify.com", "amazonaws.com", "googletagmanager.com",
    "google.com", "google.co.uk", "google.com.au", "googleanalytics.com",
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
    "recaptcha.net", "doubleclick.net", "hotjar.com", "intercom.io",
    "crisp.chat", "drift.com", "olark.com", "tawk.to",
}

# Email prefixes that are system/noreply — always skip
_SKIP_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
    "abuse", "spam", "bounce", "privacy", "legal", "unsubscribe",
    "notifications", "newsletter", "support-noreply", "auto-reply",
    "feedback", "daemon", "root", "hostmaster",
}

# Generic but still valid — ranked lower than personal emails
_GENERIC_PREFIXES = {
    "info", "contact", "hello", "enquiries", "enquiry",
    "office", "mail", "admin", "sales", "team", "general",
    "reception", "service", "services", "help", "support",
    "bookings", "booking", "reservations", "orders", "order",
    "accounts", "billing", "accounts", "quote", "quotes",
}

_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".js", ".css",
               ".woff", ".woff2", ".ttf", ".eot", ".ico", ".pdf", ".zip"}

# Contact/about pages — tried after homepage (highest yield first)
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
    # GDPR / legal pages — businesses must list a contact email here
    "/privacy-policy",
    "/privacy",
    "/legal",
    "/terms",
    "/terms-and-conditions",
    "/terms-of-service",
    # Additional high-yield pages
    "/faq",
    "/our-story",
    "/story",
    "/who-we-are",
    "/locations",
    "/find-us",
    "/pages/contact",       # Shopify
    "/pages/about",
    "/pages/contact-us",
    "/pages/get-in-touch",
    "/en/contact",          # Multilingual sites
    "/en/about",
    "/au/contact",          # Australian subpaths
]

# Regex for spelled-out email obfuscation
_SPELLED_AT  = re.compile(r"(\w[\w.\-]+)\s+at\s+([\w\-]+)\s+dot\s+([\w]{2,6})",
                            re.IGNORECASE)
_SPELLED_AT2 = re.compile(r"(\w[\w.\-]+)\s*\[at\]\s*([\w\-]+)\s*\[dot\]\s*([\w]{2,6})",
                            re.IGNORECASE)

# Keywords whose nearby text likely contains an email
_CONTACT_KEYWORDS = re.compile(
    r"(e[\s\-]?mail|contact|reach\s+us|write\s+to|get\s+in\s+touch|"
    r"send\s+us|drop\s+us|email\s+us|reach\s+out|enquir|get\s+a\s+quote|"
    r"call\s+us|phone|address|location)",
    re.IGNORECASE,
)

# Class/id patterns that are semantically high-signal for contact info
_CONTACT_CLASS_RE = re.compile(
    r"(contact|email|e-mail|reach|touch|enquir|footer|header-contact|"
    r"site-info|widget-contact|contact-info|get-in-touch|address)",
    re.IGNORECASE,
)


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
    # Reject if the TLD is a file extension (e.g. service@image.webp, foo@bar.png)
    tld = domain.rsplit(".", 1)[-1]
    if tld in _INVALID_EMAIL_EXTENSIONS:
        return None
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
    # Skip version strings (e.g. 1.0@something)
    if re.match(r"^\d+[\d.]*@", email):
        return None
    # Skip very long prefixes (likely junk)
    if len(prefix) > 40:
        return None
    # Skip single-char prefixes
    if len(prefix) < 2:
        return None
    return email


def _rank_email(email: str) -> int:
    """Lower = better. Personal emails first, generic second."""
    prefix = email.split("@")[0]
    if prefix in _SKIP_PREFIXES:
        return 99
    if prefix in _GENERIC_PREFIXES:
        return 2
    return 1   # personal / named email — best


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
    """Decode common email obfuscation patterns."""
    # Bracket/paren variants
    text = re.sub(r"\s*\[at\]\s*",  "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(at\)\s*",  "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\{at\}\s*",  "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[dot\]\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(dot\)\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\{dot\}\s*", ".", text, flags=re.IGNORECASE)
    # " AT " / " DOT " in uppercase
    text = re.sub(r"\s+AT\s+",  "@", text)
    text = re.sub(r"\s+DOT\s+", ".", text)
    # HTML entities
    text = text.replace("&#64;", "@").replace("&#46;", ".")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # Spelled-out: "john at domain dot com"
    text = _SPELLED_AT.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)
    text = _SPELLED_AT2.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)
    return text


def _fetch(url: str, timeout: int = 12) -> Optional[str]:
    """Fetch a URL and return HTML text, or None on failure."""
    try:
        headers = {
            "User-Agent":      random.choice(_USER_AGENTS),
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
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


def _url_variants(url: str) -> List[str]:
    """
    Return up to 4 URL variants to try before giving up on a site:
      - https + www   (original if it was already this)
      - https + bare
      - http  + www
      - http  + bare
    Many small business sites only work on one of these.
    """
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path   = parsed.path or "/"

        if netloc.startswith("www."):
            bare = netloc[4:]
            www  = netloc
        else:
            bare = netloc
            www  = "www." + netloc

        variants: List[str] = []
        for scheme in ("https", "http"):
            for host in (www, bare):
                v = f"{scheme}://{host}{path}"
                if v not in variants:
                    variants.append(v)
        return variants
    except Exception:
        return [url]


# ─────────────────────────────────────────────────────────────────────────────
# Core extraction from a single HTML page
# ─────────────────────────────────────────────────────────────────────────────

def _emails_from_soup(soup: BeautifulSoup, html: str) -> List[str]:
    """
    Extract ALL valid contact emails from a parsed page.
    Returns a deduplicated list ranked: personal > generic.
    """
    raw_found: List[str] = []

    def _add(e: str):
        c = _clean_email(e)
        if c and c not in raw_found:
            raw_found.append(c)

    # ── 1. Cloudflare email protection ───────────────────────────────────────
    for el in soup.select(".__cf_email__, [data-cfemail]"):
        encoded = el.get("data-cfemail", "")
        if encoded:
            _add(_decode_cloudflare_email(encoded))
    for a in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection")):
        encoded = a.get("data-cfemail", "")
        if encoded:
            _add(_decode_cloudflare_email(encoded))
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
                for cp in (item.get("contactPoint") or []):
                    if isinstance(cp, dict):
                        _add(cp.get("email", "").replace("mailto:", ""))
                for key in ("author", "creator", "founder", "employee", "owner"):
                    person = item.get(key)
                    if isinstance(person, dict):
                        _add(person.get("email", "").replace("mailto:", ""))
        except Exception:
            pass

    # ── 4. Meta tags ─────────────────────────────────────────────────────────
    for meta in soup.find_all("meta"):
        name    = (meta.get("name", "") or meta.get("property", "")).lower()
        content = meta.get("content", "") or ""
        if any(k in name for k in ("email", "contact", "author")) and content:
            _add(content)

    # ── 5. <address> tags ────────────────────────────────────────────────────
    for addr in soup.find_all("address"):
        text = _decode_obfuscated(addr.get_text(separator=" "))
        for raw in _EMAIL_RE.findall(text):
            _add(raw)
        for a in addr.find_all("a", href=re.compile(r"^mailto:", re.I)):
            _add(a["href"].replace("mailto:", "").split("?")[0].strip())

    # ── 6. <footer> and footer-like containers ────────────────────────────────
    for footer in soup.find_all(["footer", "div"],
                                 class_=re.compile(
                                     r"footer|bottom|contact|widget|sidebar", re.I)):
        text = _decode_obfuscated(footer.get_text(separator=" "))
        for raw in _EMAIL_RE.findall(text):
            _add(raw)
        for a in footer.find_all("a", href=re.compile(r"^mailto:", re.I)):
            _add(a["href"].replace("mailto:", "").split("?")[0].strip())

    # ── 7. Elements with contact-like class or id ─────────────────────────────
    for tag in soup.find_all(True):
        cls = " ".join(tag.get("class", []))
        tid = tag.get("id", "")
        if _CONTACT_CLASS_RE.search(cls) or _CONTACT_CLASS_RE.search(tid):
            text = _decode_obfuscated(tag.get_text(separator=" "))
            for raw in _EMAIL_RE.findall(text):
                _add(raw)
            for a in tag.find_all("a", href=re.compile(r"^mailto:", re.I)):
                _add(a["href"].replace("mailto:", "").split("?")[0].strip())

    # ── 8. onclick / JavaScript mailto strings ────────────────────────────────
    for tag in soup.find_all(onclick=True):
        for raw in re.findall(r"mailto:([^\s\"'\\?]+)", tag.get("onclick", ""),
                               re.IGNORECASE):
            _add(raw)
    for script in soup.find_all("script"):
        js = script.string or ""
        for raw in re.findall(
            r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
            js, re.IGNORECASE,
        ):
            _add(raw)
        for raw in re.findall(
            r"""(?:email|mail|contact)\s*[:=]\s*["']([^"'@\s]+@[^"'\s]+)["']""",
            js, re.IGNORECASE,
        ):
            _add(raw)

    # ── 9. data-email / data-mail / data-contact attributes ──────────────────
    for tag in soup.find_all(True):
        for attr in ("data-email", "data-mail", "data-contact",
                     "data-mailto", "data-address", "data-to"):
            val = tag.get(attr, "")
            if val and "@" in val:
                _add(val)

    # ── 10. HTML comments (devs sometimes leave contact emails in comments) ───
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        decoded = _decode_obfuscated(str(comment))
        for raw in _EMAIL_RE.findall(decoded):
            _add(raw)

    # ── 11. Emails near contact-keyword context windows ───────────────────────
    page_text    = soup.get_text(separator=" ")
    decoded_text = _decode_obfuscated(page_text)
    for m in _CONTACT_KEYWORDS.finditer(decoded_text):
        start   = max(0, m.start() - 80)
        end     = min(len(decoded_text), m.end() + 300)
        snippet = decoded_text[start:end]
        for raw in _EMAIL_RE.findall(snippet):
            _add(raw)

    # ── 12. Full visible text (catches anything missed above) ─────────────────
    for raw in _EMAIL_RE.findall(decoded_text):
        _add(raw)

    # ── 13. Raw HTML source (JS strings, data attrs, comments) ───────────────
    decoded_html = _decode_obfuscated(html)
    for raw in _EMAIL_RE.findall(decoded_html):
        _add(raw)

    # ── Rank: personal emails first, then generic ─────────────────────────────
    return sorted(raw_found, key=_rank_email)


# ─────────────────────────────────────────────────────────────────────────────
# Contact page discovery helpers
# ─────────────────────────────────────────────────────────────────────────────

def _discover_contact_links_from_homepage(soup: BeautifulSoup,
                                          base: str) -> List[str]:
    """
    Scan the homepage for internal links that look like contact/about pages.
    Checks both href and link text, not just href.
    """
    contact_like = re.compile(
        r"(contact|about|reach|team|enquir|connect|hello|email|touch|staff|"
        r"getintouch|get-in-touch|write|support|find-us|find_us|location|"
        r"privacy|legal|terms|store|faq|our-story)",
        re.IGNORECASE,
    )
    found: List[str] = []
    seen_paths = set(_CONTACT_PATHS)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if href.startswith("http"):
            # Only follow if same domain
            try:
                if urlparse(href).netloc not in urlparse(base).netloc:
                    continue
            except Exception:
                continue
        if contact_like.search(href) or contact_like.search(text):
            abs_url = urljoin(base, href)
            path    = urlparse(abs_url).path
            if path not in seen_paths and abs_url not in found:
                found.append(abs_url)
                seen_paths.add(path)
    return found[:10]


def _discover_contact_pages_from_sitemap(base: str) -> List[str]:
    """Parse robots.txt for sitemap URL, then find contact/about/privacy pages."""
    contact_like = re.compile(
        r"(contact|about|reach|team|enquir|connect|hello|email|touch|staff|"
        r"privacy|legal|terms)",
        re.IGNORECASE,
    )
    extra: List[str] = []

    # First check robots.txt for the sitemap URL
    sitemap_urls = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
    robots = _fetch(f"{base}/robots.txt", timeout=6)
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if sm and sm not in sitemap_urls:
                    sitemap_urls.insert(0, sm)

    for sitemap_url in sitemap_urls:
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


# ─────────────────────────────────────────────────────────────────────────────
# MX record validation  (filters dead / parked domains)
# ─────────────────────────────────────────────────────────────────────────────

def _domain_has_mx(domain: str) -> bool:
    """
    Return True if the domain has MX records (i.e. it can receive email).
    Uses Google DNS-over-HTTPS — no extra library required.
    Returns True on any network error (fail open, don't block real emails).
    """
    try:
        r = requests.get(
            "https://dns.google/resolve",
            params={"name": domain, "type": "MX"},
            timeout=5,
            headers={"Accept": "application/json"},
        )
        if r.status_code == 200:
            data = r.json()
            # Status 0 = NOERROR with answers → has MX records
            # Status 3 = NXDOMAIN → domain doesn't exist
            if data.get("Status") == 3:
                return False
            return len(data.get("Answer", [])) > 0
    except Exception:
        pass
    return True   # fail open


# ─────────────────────────────────────────────────────────────────────────────
# Name-based email inference  (last resort, marked 'inferred')
# ─────────────────────────────────────────────────────────────────────────────

# Regex: matches "John Smith", "Dr Sarah Jones", "Mike O'Brien" etc.
_PERSON_NAME_RE = re.compile(
    r"\b(?:Dr|Mr|Mrs|Ms|Miss|Prof|Sir)\.?\s+([A-Z][a-z]{1,15})\s+([A-Z][a-z']{1,20})\b"
    r"|"
    r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z']{1,20})\b"
)

# Common UK business-owner first names to avoid false positives like "New York"
_NON_NAME_WORDS = {
    "About", "Contact", "Privacy", "Policy", "Terms", "Conditions",
    "Services", "Google", "Facebook", "Twitter", "Instagram", "LinkedIn",
    "Home", "Page", "Email", "Phone", "Address", "Office", "United",
    "Kingdom", "England", "Scotland", "Wales", "London", "North", "South",
    "East", "West", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "June", "July", "August", "September", "October",
    "November", "December",
}


def _extract_person_names(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    Extract (first, last) name tuples from headings and prominent elements.
    Used to infer owner/staff email addresses when direct extraction fails.
    """
    names: List[Tuple[str, str]] = []
    seen: set = set()

    # High-priority: headings, strong, team-section elements
    for tag in soup.find_all(
        ["h1", "h2", "h3", "h4", "strong", "b", "p"],
        class_=re.compile(r"(team|staff|owner|author|founder|director|name|person)", re.I),
    ):
        _scan_text_for_names(tag.get_text(" "), names, seen)

    # Medium-priority: all headings if we haven't found any yet
    if not names:
        for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
            _scan_text_for_names(tag.get_text(" "), names, seen)

    return names[:4]   # cap at 4 candidates


def _scan_text_for_names(text: str, out: list, seen: set) -> None:
    for m in _PERSON_NAME_RE.finditer(text):
        if m.group(1) and m.group(2):          # "Dr John Smith" form
            first, last = m.group(1), m.group(2)
        else:                                   # "John Smith" plain form
            first, last = m.group(3), m.group(4)

        if (first in _NON_NAME_WORDS or last in _NON_NAME_WORDS):
            continue
        key = (first.lower(), last.lower())
        if key not in seen:
            seen.add(key)
            out.append(key)


def _infer_email_from_names(
    names: List[Tuple[str, str]], domain: str
) -> Optional[str]:
    """
    Try common first-name / firstname.lastname email patterns for each name.
    Returns the first candidate that passes _clean_email and MX check.
    """
    for first, last in names:
        # Ranked patterns: most common UK business email formats first
        candidates = [
            f"{first}@{domain}",
            f"{first}.{last}@{domain}",
            f"{first[0]}.{last}@{domain}",
            f"{first}{last[0]}@{domain}",
        ]
        for c in candidates:
            if _clean_email(c):
                return c   # return first plausible; MX is checked at domain level
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def find_email_on_website(
    website_url: str,
    use_guess_fallback: bool = False,
    fast_mode: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Visit a business website and return (email, source):
      source = 'found'    → extracted directly from real page content
      source = 'inferred' → derived from person name found on the page
      source = 'guessed'  → info@ pattern fallback (only if use_guess_fallback=True)

    fast_mode=True (batch search default):
      - Timeout cut from 12s → 6s per request
      - Skips sitemap/robots.txt crawl (Phase 3)
      - Skips inter-page sleep delays
      - Checks homepage + top 4 contact paths only
      - ~3-4x faster per site with only ~10% fewer finds
      - Use for bulk batch searching

    fast_mode=False (default, single-search):
      - Full deep crawl: sitemap, robots.txt, name inference
      - Best email find rate, slower
    """
    if not website_url:
        return None, None

    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    try:
        parsed = urlparse(website_url)
        domain = parsed.netloc.lstrip("www.")
    except Exception:
        return None, None

    # ── Phase 0: MX check — skip dead/parked domains ──────────────────────────
    # Saves time and prevents saving emails for domains that can't receive mail.
    if not _domain_has_mx(domain):
        return None, None

    _timeout = 6 if fast_mode else 12   # fast mode: don't block on slow sites

    domain_is_live    = False
    extra_from_home:  List[str] = []
    working_base      = ""     # first base URL that actually responds
    names_from_pages: List[Tuple[str, str]] = []   # person names collected

    # ── Phase 1: Homepage — try URL variants ──────────────────────────────────
    homepage_html: Optional[str] = None
    for variant in _url_variants(website_url):
        homepage_html = _fetch(variant, timeout=_timeout)
        if homepage_html:
            domain_is_live = True
            try:
                p = urlparse(variant)
                working_base = f"{p.scheme}://{p.netloc}"
            except Exception:
                working_base = variant
            break

    if homepage_html:
        soup   = BeautifulSoup(homepage_html, "lxml")
        emails = _emails_from_soup(soup, homepage_html)
        if emails:
            return emails[0], "found"
        extra_from_home   = _discover_contact_links_from_homepage(soup, working_base)
        # Collect names from homepage for later inference fallback
        names_from_pages  = _extract_person_names(soup)

    # ── Phase 2: Standard contact paths + homepage-discovered links ───────────
    if not working_base:
        return None, None

    # In fast mode: only top 4 contact paths, no link discovery from homepage
    if fast_mode:
        standard_pages = [urljoin(working_base, p) for p in _CONTACT_PATHS[:4]]
        pages_to_try   = standard_pages
    else:
        standard_pages = [urljoin(working_base, p) for p in _CONTACT_PATHS]
        pages_to_try   = extra_from_home + [p for p in standard_pages
                                             if p not in extra_from_home]

    seen: set = {website_url}
    for v in _url_variants(website_url):
        seen.add(v)

    for url in pages_to_try:
        if url in seen:
            continue
        seen.add(url)

        html = _fetch(url, timeout=_timeout)
        if not html:
            continue

        domain_is_live = True
        soup   = BeautifulSoup(html, "lxml")
        emails = _emails_from_soup(soup, html)
        if emails:
            return emails[0], "found"

        # Accumulate names from contact/team/about pages (highest signal)
        if not names_from_pages:
            names_from_pages = _extract_person_names(soup)

        if not fast_mode:
            time.sleep(random.uniform(0.15, 0.4))

    # ── Phase 3: Sitemap discovery (skipped in fast mode) ─────────────────────
    if not fast_mode:
        try:
            sitemap_pages = _discover_contact_pages_from_sitemap(working_base)
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
                if not names_from_pages:
                    names_from_pages = _extract_person_names(soup)
                time.sleep(random.uniform(0.15, 0.35))
        except Exception:
            pass

    # ── Phase 4: Name-based inference ────────────────────────────────────────
    # If we found person names on the site, try firstname@domain etc.
    # These are much more likely to be read than info@ guesses.
    if domain_is_live and names_from_pages:
        inferred = _infer_email_from_names(names_from_pages, domain)
        if inferred:
            return inferred, "inferred"

    # ── Phase 5: info@ guess fallback (disabled by default) ──────────────────
    if use_guess_fallback and domain_is_live:
        candidate = _clean_email(f"info@{domain}")
        if candidate:
            return candidate, "guessed"

    return None, None
