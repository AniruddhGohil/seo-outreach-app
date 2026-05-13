"""
scraper.py – Multi-country business discovery.

Sources (in priority order):
  1. Yelp Fusion API  – if the user supplies a free API key (500 calls/day)
  2. Country-specific Yellow Pages scraping – no key required
"""
import json
import random
import re
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode, urljoin, urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
    )
    return s


def _safe_get(
    session: requests.Session, url: str, timeout: int = 15
) -> Optional[requests.Response]:
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
        return None
    except Exception:
        return None


def _rand_delay(lo: float = 2.0, hi: float = 4.5):
    time.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _text(el) -> str:
    return el.get_text(separator=" ", strip=True) if el else ""


def _first(parent, selectors: list):
    for sel in selectors:
        found = parent.select_one(sel)
        if found:
            return found
    return None


def _extract_next_data(soup: BeautifulSoup) -> Optional[dict]:
    """Pull __NEXT_DATA__ JSON from Next.js pages."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except Exception:
            pass
    return None


def _external_link(card: BeautifulSoup, skip_domains: tuple) -> str:
    """Find the first outbound link in a card that isn't a directory domain."""
    for a in card.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and not any(d in href for d in skip_domains):
            return href
    return ""


# ---------------------------------------------------------------------------
# Country scrapers
# ---------------------------------------------------------------------------

# ── Australia ──────────────────────────────────────────────────────────────

def scrape_australia(
    keyword: str, location: str, max_pages: int = 3,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    results: List[Dict] = []
    session = _make_session()

    for page in range(1, max_pages + 1):
        url = (
            "https://www.yellowpages.com.au/search/listings?"
            + urlencode({"clue": keyword, "locationClue": location, "pageNumber": page})
        )
        if log_cb:
            log_cb(f"🇦🇺 AU – page {page} …")

        r = _safe_get(session, url)
        if not r:
            if log_cb:
                log_cb("  ⚠️ No response – skipping.")
            break

        soup = BeautifulSoup(r.content, "lxml")

        # --- Try embedded Next.js JSON first ---
        nd = _extract_next_data(soup)
        if nd:
            try:
                # Path may vary; try several
                page_props = nd.get("props", {}).get("pageProps", {})
                items = (
                    page_props.get("initialData", {})
                              .get("searchResults", {})
                              .get("results", [])
                    or page_props.get("searchResults", {}).get("results", [])
                )
                for item in items:
                    addr_obj = item.get("primaryAddress", {}) or {}
                    b = {
                        "business_name": item.get("name", ""),
                        "phone":         item.get("primaryPhone", ""),
                        "website":       item.get("externalUrl", "") or item.get("primaryUrl", ""),
                        "address":       addr_obj.get("displayAddress", "")
                                         or addr_obj.get("addressLine", ""),
                        "city":          location,
                        "country":       "Australia",
                        "keyword":       keyword,
                        "source":        "Yellow Pages AU",
                    }
                    if b["business_name"]:
                        results.append(b)
                if items and log_cb:
                    log_cb(f"  ✅ {len(items)} businesses via JSON")
                if items:
                    _rand_delay()
                    continue
            except Exception:
                pass  # fall through to HTML

        # --- HTML fallback ---
        cards = (
            soup.select("div.listing-body")
            or soup.select("div[class*='listing']")
            or soup.select("article")
        )
        for card in cards:
            name_el = _first(card, ["h2", "h3", "[class*='name']", "a.listing-name"])
            if not name_el:
                continue
            b = {
                "business_name": _text(name_el),
                "city":    location,
                "country": "Australia",
                "keyword": keyword,
                "source":  "Yellow Pages AU",
            }
            ph = _first(card, ["[class*='phone']", "a[href^='tel:']"])
            if ph:
                b["phone"] = re.sub(r"[^\d\s+\-()]", "", _text(ph))
            ad = _first(card, ["[class*='address']", "[class*='location']"])
            if ad:
                b["address"] = _text(ad)
            b["website"] = _external_link(card, ("yellowpages.com.au",))
            results.append(b)

        if log_cb:
            log_cb(f"  ✅ {len(cards)} businesses via HTML")
        _rand_delay()

    return results


# ── USA ────────────────────────────────────────────────────────────────────

def scrape_usa(
    keyword: str, location: str, max_pages: int = 3,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    results: List[Dict] = []
    session = _make_session()

    for page in range(1, max_pages + 1):
        url = (
            "https://www.yellowpages.com/search?"
            + urlencode({"search_terms": keyword, "geo_location_terms": location, "page": page})
        )
        if log_cb:
            log_cb(f"🇺🇸 USA – page {page} …")

        r = _safe_get(session, url)
        if not r:
            break

        soup = BeautifulSoup(r.content, "lxml")
        cards = soup.select("div.result") or soup.select("div.v-card")

        for card in cards:
            name_el = _first(card, ["h2.n a", "a.business-name", "h2 a", "h3 a"])
            if not name_el:
                continue
            b = {
                "business_name": _text(name_el),
                "city":    location,
                "country": "USA",
                "keyword": keyword,
                "source":  "Yellow Pages USA",
            }
            ph = card.select_one("div.phones")
            if ph:
                b["phone"] = _text(ph)
            ad = card.select_one("p.adr")
            if ad:
                b["address"] = _text(ad)
            web_el = card.select_one("a.track-visit-website")
            if web_el:
                b["website"] = web_el.get("href", "")
            results.append(b)

        if log_cb:
            log_cb(f"  ✅ {len(cards)} businesses")
        _rand_delay()

    return results


# ── United Kingdom ─────────────────────────────────────────────────────────

def scrape_uk(
    keyword: str, location: str, max_pages: int = 3,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    results: List[Dict] = []
    session = _make_session()

    for page in range(1, max_pages + 1):
        url = (
            "https://www.yell.com/ucs/UcsSearchAction.do?"
            + urlencode({"keywords": keyword, "location": location, "pageNum": page})
        )
        if log_cb:
            log_cb(f"🇬🇧 UK – page {page} …")

        r = _safe_get(session, url)
        if not r:
            break

        soup = BeautifulSoup(r.content, "lxml")
        cards = (
            soup.select("article.businessCapsule")
            or soup.select("[class*='businessCapsule']")
            or soup.select("div.row--listing")
        )

        for card in cards:
            name_el = _first(card, [
                "span.businessCapsule--name", "[class*='businessName']",
                "h3 a", "h2 a",
            ])
            if not name_el:
                continue
            b = {
                "business_name": _text(name_el),
                "city":    location,
                "country": "United Kingdom",
                "keyword": keyword,
                "source":  "Yell.com UK",
            }
            ph = card.select_one("[class*='phone']")
            if ph:
                b["phone"] = _text(ph)
            ad = card.select_one("address, [class*='address']")
            if ad:
                b["address"] = _text(ad)
            web_el = card.select_one("a[href*='http'][class*='website']")
            if web_el:
                b["website"] = web_el.get("href", "")
            results.append(b)

        if log_cb:
            log_cb(f"  ✅ {len(cards)} businesses")
        _rand_delay()

    return results


# ── New Zealand ────────────────────────────────────────────────────────────

def scrape_nz(
    keyword: str, location: str, max_pages: int = 3,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    results: List[Dict] = []
    session = _make_session()

    for page in range(1, max_pages + 1):
        url = (
            "https://www.yellow.co.nz/search?"
            + urlencode({"q": keyword, "l": location, "p": page})
        )
        if log_cb:
            log_cb(f"🇳🇿 NZ – page {page} …")

        r = _safe_get(session, url)
        if not r:
            break

        soup = BeautifulSoup(r.content, "lxml")
        cards = soup.select("[class*='listing']") or soup.select("article")

        for card in cards:
            name_el = _first(card, ["h2", "h3", "[class*='name']"])
            if not name_el:
                continue
            b = {
                "business_name": _text(name_el),
                "city":    location,
                "country": "New Zealand",
                "keyword": keyword,
                "source":  "Yellow NZ",
            }
            ph = _first(card, ["[class*='phone']", "a[href^='tel:']"])
            if ph:
                b["phone"] = re.sub(r"[^\d\s+\-()]", "", _text(ph))
            ad = card.select_one("[class*='address']")
            if ad:
                b["address"] = _text(ad)
            b["website"] = _external_link(card, ("yellow.co.nz",))
            results.append(b)

        if log_cb:
            log_cb(f"  ✅ {len(cards)} businesses")
        _rand_delay()

    return results


# ── UAE ────────────────────────────────────────────────────────────────────

def scrape_uae(
    keyword: str, location: str, max_pages: int = 3,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    results: List[Dict] = []
    session = _make_session()
    loc = location or "Dubai"

    for page in range(1, max_pages + 1):
        url = (
            "https://www.yellowpages.ae/en/search?"
            + urlencode({"q": keyword, "where": loc, "page": page})
        )
        if log_cb:
            log_cb(f"🇦🇪 UAE – page {page} …")

        r = _safe_get(session, url)
        if not r:
            break

        soup = BeautifulSoup(r.content, "lxml")
        cards = (
            soup.select("[class*='listing']")
            or soup.select("[class*='result']")
            or soup.select("article")
        )

        for card in cards:
            name_el = _first(card, ["h2", "h3", "[class*='name']", "a.title"])
            if not name_el:
                continue
            b = {
                "business_name": _text(name_el),
                "city":    loc,
                "country": "UAE",
                "keyword": keyword,
                "source":  "Yellow Pages UAE",
            }
            ph = _first(card, ["[class*='phone']", "a[href^='tel:']"])
            if ph:
                b["phone"] = re.sub(r"[^\d\s+\-()]", "", _text(ph))
            b["website"] = _external_link(card, ("yellowpages.ae",))
            results.append(b)

        if log_cb:
            log_cb(f"  ✅ {len(cards)} businesses")
        _rand_delay()

    return results


# ── Foursquare Places API  (FREE – 1000 calls/day, no credit card) ────────

def scrape_foursquare(
    keyword: str, location: str, api_key: str,
    max_results: int = 50,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    """
    Foursquare Places API v3.
    - 100% free, no credit card required.
    - 1000 API calls/day free.
    - Global coverage including UAE, AU, NZ, UK, USA.
    Register free at: https://foursquare.com/developers/
    """
    results: List[Dict] = []
    headers = {
        "Authorization": api_key,
        "Accept":        "application/json",
    }

    # ── Step 1: Search for places (basic info) ───────────────────────────────
    try:
        r = requests.get(
            "https://api.foursquare.com/v3/places/search",
            headers=headers,
            params={
                "query": keyword,
                "near":  location,
                "limit": min(50, max_results),
            },
            timeout=20,
        )

        if r.status_code == 401:
            if log_cb:
                log_cb("  ❌ Foursquare: Invalid API key.")
            return results
        if r.status_code != 200:
            if log_cb:
                log_cb(f"  ⚠️ Foursquare HTTP {r.status_code}: {r.text[:100]}")
            return results

        places = r.json().get("results", [])
        if not places:
            if log_cb:
                log_cb("  ℹ️ Foursquare: No results found.")
            return results

        if log_cb:
            log_cb(f"  📍 {len(places)} businesses found — fetching details …")

    except Exception as exc:
        if log_cb:
            log_cb(f"  ⚠️ Foursquare search error: {exc}")
        return results

    # ── Step 2: Fetch details (website + phone) for each place ───────────────
    for place in places:
        fsq_id = place.get("fsq_id", "")
        loc    = place.get("location", {})

        b = {
            "business_name": place.get("name", ""),
            "address": (
                loc.get("formatted_address", "")
                or ", ".join(filter(None, [
                    loc.get("address", ""),
                    loc.get("locality", ""),
                    loc.get("region", ""),
                ]))
            ),
            "city":    loc.get("locality", location),
            "country": loc.get("country", ""),
            "keyword": keyword,
            "source":  "Foursquare",
            "phone":   "",
            "website": "",
        }

        # Details call for phone + website
        if fsq_id:
            try:
                det = requests.get(
                    f"https://api.foursquare.com/v3/places/{fsq_id}",
                    headers=headers,
                    params={"fields": "tel,website"},
                    timeout=10,
                )
                if det.status_code == 200:
                    d = det.json()
                    b["phone"]   = d.get("tel", "")
                    b["website"] = d.get("website", "")
            except Exception:
                pass  # details are optional; email finder will handle website

        results.append(b)

    if log_cb:
        log_cb(f"  ✅ Foursquare total: {len(results)} businesses")
    return results


# ── Yelp Fusion API (optional, free key) ──────────────────────────────────

def scrape_yelp(
    keyword: str, location: str, api_key: str,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    """
    Yelp Fusion API – free tier: 500 calls/day, max 50 results per call.
    Register at https://www.yelp.com/developers/v3/manage_app
    """
    results: List[Dict] = []
    headers = {"Authorization": f"Bearer {api_key}"}
    endpoint = "https://api.yelp.com/v3/businesses/search"

    for offset in range(0, 150, 50):
        params = {
            "term": keyword, "location": location,
            "limit": 50, "offset": offset,
        }
        try:
            r = requests.get(endpoint, headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                if log_cb:
                    log_cb(f"  ⚠️ Yelp API: {r.status_code} – {r.text[:120]}")
                break
            data = r.json()
            businesses = data.get("businesses", [])
            if not businesses:
                break
            for biz in businesses:
                loc_data = biz.get("location", {})
                b = {
                    "business_name": biz.get("name", ""),
                    "phone":         biz.get("display_phone", ""),
                    "website":       biz.get("url", ""),
                    "address":       ", ".join(loc_data.get("display_address", [])),
                    "city":          loc_data.get("city", location),
                    "country":       loc_data.get("country", ""),
                    "keyword":       keyword,
                    "source":        "Yelp",
                }
                results.append(b)
            if log_cb:
                log_cb(f"  ✅ Yelp: {len(businesses)} businesses (offset {offset})")
            if len(businesses) < 50:
                break
        except Exception as exc:
            if log_cb:
                log_cb(f"  ⚠️ Yelp error: {exc}")
            break

    return results


# ── Google Places API  (best quality, all countries including UAE) ─────────

def _get_place_details(place_id: str, api_key: str) -> dict:
    """Fetch phone + website for a single place_id."""
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "place_id": place_id,
                "fields":   "formatted_phone_number,website",
                "key":      api_key,
            },
            timeout=10,
        )
        res = r.json().get("result", {})
        return {
            "phone":   res.get("formatted_phone_number", ""),
            "website": res.get("website", ""),
        }
    except Exception:
        return {"phone": "", "website": ""}


def scrape_google_places(
    keyword: str, location: str, api_key: str,
    max_results: int = 60,
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    """
    Google Places Text Search API.
    – $200 free credit/month (~6 000 text-search calls + details).
    – Works for AU, NZ, UK, UAE, USA and everywhere else.
    Enable 'Places API' in Google Cloud Console → APIs & Services → Library.
    """
    results: List[Dict] = []
    endpoint = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params   = {"query": f"{keyword} in {location}", "key": api_key}

    page_num = 1
    while len(results) < max_results:
        try:
            r    = requests.get(endpoint, params=params, timeout=15)
            data = r.json()
            status = data.get("status", "")

            if status == "REQUEST_DENIED":
                if log_cb:
                    log_cb(f"  ❌ Google Places: {data.get('error_message', 'API key issue')}")
                break
            if status == "ZERO_RESULTS":
                if log_cb:
                    log_cb("  ℹ️ Google Places: no results for this query.")
                break
            if status not in ("OK",):
                if log_cb:
                    log_cb(f"  ⚠️ Google Places status: {status}")
                break

            places = data.get("results", [])
            if not places:
                break

            if log_cb:
                log_cb(f"  📍 Page {page_num}: {len(places)} businesses found – fetching details …")

            for place in places:
                place_id = place.get("place_id", "")
                details  = _get_place_details(place_id, api_key) if place_id else {}
                b = {
                    "business_name": place.get("name", ""),
                    "address":       place.get("formatted_address", ""),
                    "phone":         details.get("phone", ""),
                    "website":       details.get("website", ""),
                    "city":          location,
                    "country":       "",          # filled by router
                    "keyword":       keyword,
                    "source":        "Google Places",
                }
                results.append(b)
                if len(results) >= max_results:
                    break

            next_token = data.get("next_page_token")
            if not next_token or len(results) >= max_results:
                break

            # Google requires a short pause before using next_page_token
            if log_cb:
                log_cb("  ⏳ Waiting 2 s for next page …")
            time.sleep(2)
            params   = {"pagetoken": next_token, "key": api_key}
            page_num += 1

        except Exception as exc:
            if log_cb:
                log_cb(f"  ⚠️ Google Places error: {exc}")
            break

    if log_cb:
        log_cb(f"  ✅ Google Places total: {len(results)} businesses")
    return results


# ---------------------------------------------------------------------------
# Public router
# ---------------------------------------------------------------------------

COUNTRY_SCRAPERS: Dict[str, Callable] = {
    "Australia":      scrape_australia,
    "New Zealand":    scrape_nz,
    "United Kingdom": scrape_uk,
    "UAE":            scrape_uae,
    "USA":            scrape_usa,
}

COUNTRY_CODES: Dict[str, str] = {
    "Australia":      "Australia",
    "New Zealand":    "New Zealand",
    "United Kingdom": "United Kingdom",
    "UAE":            "UAE",
    "USA":            "USA",
}


def find_businesses(
    keyword: str,
    location: str,
    country: str,
    max_pages: int = 3,
    yelp_api_key: str = "",
    google_places_key: str = "",
    foursquare_key: str = "",
    log_cb: Optional[Callable] = None,
) -> List[Dict]:
    """
    Priority order:
      1. Foursquare Places API  (FREE, no card, 1000/day – recommended)
      2. Google Places API      (best quality, needs billing enabled)
      3. Yelp Fusion API        (good, 500/day free)
      4. Yellow Pages scrape    (fallback, no key needed)
    """
    # ── 1. Foursquare (free, no credit card, global) ─────────────────────────
    if foursquare_key and foursquare_key.strip():
        if log_cb:
            log_cb("📍 Using Foursquare Places API (free) …")
        results = scrape_foursquare(
            keyword, location, foursquare_key.strip(),
            max_results=max_pages * 20,
            log_cb=log_cb,
        )
        if results:
            for r in results:
                r["country"] = r.get("country") or COUNTRY_CODES.get(country, country)
            return results
        if log_cb:
            log_cb("  ⚠️ Foursquare returned nothing – trying next source.")

    # ── 2. Google Places (best quality, needs billing) ───────────────────────
    if google_places_key and google_places_key.strip():
        if log_cb:
            log_cb("🗺️ Using Google Places API …")
        results = scrape_google_places(
            keyword, location, google_places_key.strip(),
            max_results=max_pages * 20,
            log_cb=log_cb,
        )
        if results:
            for r in results:
                r["country"] = COUNTRY_CODES.get(country, country)
            return results
        if log_cb:
            log_cb("  ⚠️ Google Places returned nothing – trying next source.")

    # ── 3. Yelp ──────────────────────────────────────────────────────────────
    if yelp_api_key and yelp_api_key.strip():
        if log_cb:
            log_cb("🟡 Using Yelp Fusion API …")
        results = scrape_yelp(keyword, location, yelp_api_key.strip(), log_cb=log_cb)
        if results:
            return results
        if log_cb:
            log_cb("  ⚠️ Yelp returned nothing – falling back to directory scrape.")

    # ── 4. Directory scrape (no key fallback) ────────────────────────────────
    if log_cb:
        log_cb("🌐 Using Yellow Pages directory scrape …")
    scraper_fn = COUNTRY_SCRAPERS.get(country)
    if not scraper_fn:
        if log_cb:
            log_cb(f"  ⚠️ No scraper available for '{country}'.")
        return []
    return scraper_fn(keyword, location, max_pages, log_cb=log_cb)
