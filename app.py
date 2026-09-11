"""
Altitude Exhibits -- Lead Intelligence & Outreach Dashboard (MVP)
=================================================================

A single-file Streamlit app that turns a trade-show exhibitor directory into a
ranked list of mid-market ("Goldilocks") prospects for Altitude Exhibits, a
Las Vegas custom exhibit builder, and generates a timed 4-touch email sequence
for each one.

Run it locally
--------------
    pip install -r requirements.txt        # streamlit pandas requests beautifulsoup4
    streamlit run app.py

Deploy (free, public URL in ~2 minutes)
---------------------------------------
    Push this folder to a GitHub repo -> https://share.streamlit.io -> New app ->
    pick the repo, main file = app.py -> Deploy.  (See README.md for Docker too.)

Pipeline
--------
    URL  ->  scrape: MapYourShow JSON API (full exhibitor list + floor-plan
             booth geometry, so booth sizes are exact), or generic HTML tables
             and cards for other directories. No sample data: if a site cannot
             be read the app says so and stops.
         ->  enrichment (simulated Apollo/Clearbit; real Apollo if a key is set)
         ->  Goldilocks scoring (0-100) on booth size, revenue, headcount
         ->  decision-maker targeting + timeline-aware 4-touch email sequence
         ->  dashboard grid, KPIs, campaign CSV export

Where a real Apollo API key goes
--------------------------------
See `enrich_company_apollo()` and `find_contact_apollo()`. Drop the key into
`.streamlit/secrets.toml` as APOLLO_API_KEY (or paste it into the sidebar) and
the simulated enrichment is replaced by live calls, one company at a time.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# =============================================================================
# 0. CONFIGURATION
# =============================================================================

COMPANY_NAME = "Altitude Exhibits"
COMPANY_SITE = "altitudeexhibits.com"
TAGLINE = "Lead Intelligence & Outreach Dashboard"

# The Goldilocks profile: big enough to buy a custom build, small enough that
# the mega-agencies aren't already camped in their lobby.
DEFAULTS = {
    "booth_range": (200, 600),   # sq ft  (10x20 .. 20x30)
    "revenue_range": (15, 100),  # $M annual revenue
    "headcount_range": (50, 500),
    "price_per_sqft": 150,       # $ per sq ft of exhibit -- used for pipeline value
    "only_goldilocks": False,
    # Second qualifying rule: a big company in a small booth. They are at the
    # show because they have to be, and the flagship build comes later.
    "big_fish": True,
    "big_fish_min_revenue": 100,  # $M -- anything above the Goldilocks ceiling
}
PRESETS = {
    "Mid-market (200-600 sq ft)": (200, 600),
    "Islands (400+ sq ft)": (400, 2500),
}

# How the 100 points are split. Booth size is the strongest signal we can
# actually observe on the show floor, so it carries the most weight.
SCORE_WEIGHTS = {"booth": 40, "revenue": 30, "headcount": 30}
NEAR_MISS_THRESHOLD = 65  # misses one criterion but still worth a call
BIG_FISH_FLOOR_SCORE = 75  # big-fish leads rank just under perfect Goldilocks matches

REQUEST_TIMEOUT = 12  # seconds
MIN_ROWS_FOR_VALID_SCRAPE = 3

# A browser-like header set. Plenty of directories return 403 to the default
# python-requests user agent even when they are perfectly happy to serve Chrome.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# -----------------------------------------------------------------------------
# Target shows: Las Vegas, March-June 2027. Custom-build RFPs are typically
# sourced 5-7 months before the floor opens, so the outreach window for these
# is September 2026 through January 2027. Dates come from the show calendars;
# confirm them on each show's site before a campaign goes out.
# -----------------------------------------------------------------------------
TARGET_SHOWS = [
    {"name": "IWCE 2027", "start": "2027-03-08", "end": "2027-03-11", "venue": "Las Vegas Convention Center",
     "industry": "Tech & Comms", "potential": "High", "site": "https://www.iwceexpo.com", "directory": ""},
    {"name": "Shoptalk 2027", "start": "2027-03-22", "end": "2027-03-24", "venue": "Mandalay Bay",
     "industry": "Retail Tech & E-commerce", "potential": "Very High", "site": "https://shoptalk.com", "directory": ""},
    {"name": "Bar & Restaurant Expo 2027", "start": "2027-03-22", "end": "2027-03-24", "venue": "Las Vegas Convention Center",
     "industry": "Food & Beverage", "potential": "Medium", "site": "https://www.barandrestaurantexpo.com", "directory": ""},
    {"name": "Indoor Ag-Con 2027", "start": "2027-03-24", "end": "2027-03-25", "venue": "Las Vegas Convention Center",
     "industry": "Agriculture Tech", "potential": "Medium", "site": "https://indoor.ag", "directory": ""},
    {"name": "NAB Show 2027", "start": "2027-04-04", "end": "2027-04-07", "venue": "Las Vegas Convention Center",
     "industry": "Broadcast, Media & Tech", "potential": "Massive", "site": "https://nabshow.com",
     "directory": "https://nab27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"},
    {"name": "Pizza Expo 2027", "start": "2027-04-13", "end": "2027-04-15", "venue": "Las Vegas Convention Center",
     "industry": "Food & Beverage", "potential": "Medium", "site": "https://www.pizzaexpo.com", "directory": ""},
    {"name": "WasteExpo 2027", "start": "2027-05-03", "end": "2027-05-06", "venue": "Las Vegas Convention Center",
     "industry": "Industrial & Heavy Machinery", "potential": "High", "site": "https://www.wasteexpo.com", "directory": ""},
    {"name": "HD Expo 2027", "start": "2027-05-04", "end": "2027-05-05", "venue": "Mandalay Bay",
     "industry": "Commercial Design", "potential": "Very High", "site": "https://www.hdexpo.com", "directory": ""},
    {"name": "HR in Hospitality 2027", "start": "2027-06-09", "end": "2027-06-10", "venue": "Las Vegas",
     "industry": "HR & Hospitality Tech", "potential": "Medium", "site": "https://www.hrinhospitality.com", "directory": ""},
]
SHOW_BY_NAME = {s["name"]: s for s in TARGET_SHOWS}

# Other Las Vegas directories already published on MapYourShow (exact booth
# sizes available). Handy for a live demo when a target show's list isn't out.
KNOWN_DIRECTORIES = [
    ("NAB Show 2027", "https://nab27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
    ("CES 2026", "https://ces26.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
    ("World of Concrete 2027", "https://ge27woc.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
    ("TISE 2027", "https://ge27tise.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
    ("AHR Expo 2027", "https://ahr27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
    ("PACK EXPO Las Vegas 2027", "https://packexpo27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
    ("International Roofing Expo 2027", "https://ge27ire.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false"),
]

# RFP timing model (months before the show floor opens).
WINDOW_OPEN_MONTHS = 9    # earliest sensible first touch
WINDOW_PRIME_MONTHS = 7   # "starting to map out your footprint"
WINDOW_CLOSE_MONTHS = 5   # exhibit house usually chosen by now
SEQUENCE_OFFSETS_DAYS = [0, 4, 10, 18]  # 4-touch cadence

# Decision-maker titles, in the order Apollo/Clay should try them.
TARGET_TITLES = [
    "Trade Show Manager",
    "Event Marketing Manager",
    "Director of Field Marketing",
    "Chief Marketing Officer",
]

# Hostname prefixes that Vegas shows use on MapYourShow / A2Z. Anything not
# listed is upper-cased as-is (e.g. "isc26" -> "ISC 2026").
KNOWN_SHOWS = {
    "ces": "CES", "nab": "NAB Show", "sema": "SEMA Show", "aapex": "AAPEX", "shot": "SHOT Show",
    "ibs": "NAHB International Builders' Show", "woc": "World of Concrete", "g2e": "Global Gaming Expo",
    "mjbiz": "MJBizCon", "nada": "NADA Show", "conexpo": "CONEXPO-CON/AGG", "iscwest": "ISC West",
    "isc": "ISC West", "asd": "ASD Market Week", "magic": "MAGIC Las Vegas", "awfs": "AWFS Fair",
    "reinvent": "AWS re:Invent", "kbis": "KBIS", "infocomm": "InfoComm", "packexpo": "PACK EXPO",
    "iwce": "IWCE", "shoptalk": "Shoptalk", "wasteexpo": "WasteExpo", "hdexpo": "HD Expo",
    "pizzaexpo": "Pizza Expo", "indoorag": "Indoor Ag-Con", "bre": "Bar & Restaurant Expo",
}

# Keyword -> industry lookup used by the enrichment simulation. First match wins.
INDUSTRY_PATTERNS = [
    (r"\baudio\b|\bsound\b|\bspeaker", "Pro Audio & Sound"),
    (r"\brobot", "Robotics & Camera Motion"),
    (r"\bdrone|\buav\b|\baerial", "Drones & Aerial Cinematography"),
    (r"\bsignal|\brf\b|\btransmi", "RF & Transmission"),
    (r"\blight", "Studio & Stage Lighting"),
    (r"\bwireless video|\bvideo\b", "Video Technology"),
    (r"\bnetwork|\btelecom|\b5g\b|\bwireless\b", "Networking & IP Media"),
    (r"\bstreaming|\bpodcast|\bott\b", "Streaming & Podcasting"),
    (r"\bdisplay|\bled\b|\bscreen", "LED & Display Technology"),
    (r"\bnewsroom|\bcaption|\bmedia software|\bsubtitl", "Media Software"),
    (r"\bintercom|\bcomms\b", "Comms & Intercom"),
    (r"\bsatellite|\buplink", "Satellite & Uplink"),
    (r"\bbattery|\bpower", "Power & Batteries"),
    (r"\bcamera|\bimaging|\bvision\b", "Cameras & Imaging"),
    (r"\brugged\b|\bcomput|\bserver", "Computing Hardware"),
    (r"\bar\b|\bvr\b|\bxr\b|\boptic|\bvirtual production", "AR/VR & Virtual Production"),
    (r"\bai\b|\bmachine learning\b", "AI & Media Analytics"),
    (r"\bsolar\b|\bclean energy\b", "Clean Energy"),
    (r"\bhealth|\bmed(ical)?\b|\bbio", "Digital Health & MedTech"),
    (r"\bgaming\b|\besports\b", "Gaming Hardware"),
    (r"\bsecurity|\bsurveillance", "Security & Surveillance"),
    (r"\bcloud\b|\bsaas\b|\bsoftware|\bplatform", "Enterprise Software"),
    (r"\bauto|\bvehicle|\bmotor", "Automotive Tech"),
    (r"\bhome\b|\bsmart", "Smart Home & IoT"),
]
GENERIC_INDUSTRIES = [
    "Broadcast Equipment", "Enterprise Software", "Industrial Equipment", "Audio/Video Technology",
    "Telecommunications", "Media Services", "Consumer Electronics", "Lighting & Displays",
]

# =============================================================================
# 1. BOOTH-SIZE HELPERS
# =============================================================================

DIMS_RE = re.compile(r"(\d{1,3})\s*(?:'|ft|′)?\s*[xX×]\s*(\d{1,3})\s*(?:'|ft|′)?")
SQFT_RE = re.compile(r"(\d{2,5})\s*(?:sq\.?\s*ft\.?|sqft|square\s+feet)", re.I)
BOOTH_NO_RE = re.compile(r"booth\s*(?:#|no\.?|number)?\s*:?\s*([A-Z]{0,2}-?\d{2,6}[A-Z]?)", re.I)


def booth_dims_to_sqft(dims: str) -> int:
    """'20x30' -> 600.  Unknown formats -> 0."""
    m = DIMS_RE.search(dims or "")
    if not m:
        return 0
    return int(m.group(1)) * int(m.group(2))


def sqft_to_dims_label(sqft: int) -> str:
    """Best-effort label for a square footage: 400 -> '20x20'."""
    common = {100: "10x10", 200: "10x20", 300: "10x30", 400: "20x20", 600: "20x30", 800: "20x40",
              900: "30x30", 1200: "30x40", 1600: "40x40", 2000: "40x50", 2500: "50x50"}
    return common.get(sqft, f"{sqft} sq ft")


def extract_booth_size(text: str) -> tuple[str, int] | None:
    """Pull '10x20' or '400 sq ft' out of arbitrary text."""
    m = DIMS_RE.search(text or "")
    if m:
        w, d = int(m.group(1)), int(m.group(2))
        if 5 <= w <= 200 and 5 <= d <= 200:
            return f"{w}x{d}", w * d
    m = SQFT_RE.search(text or "")
    if m:
        sqft = int(m.group(1))
        return sqft_to_dims_label(sqft), sqft
    return None


# =============================================================================
# 2. LIVE URL SCRAPER
# =============================================================================


class ScrapeError(Exception):
    """Raised when a directory cannot be read; the UI shows the reason and stops."""


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "mapyourshow" in host:
        return "MapYourShow"
    if "a2z" in host or "a2zinc" in host:
        return "A2Z Events"
    if "expocad" in host:
        return "ExpoCAD"
    return "Generic HTML"


def infer_convention_name(url: str, soup: BeautifulSoup | None = None) -> str:
    """
    Work out the show name from the page title first, then from the hostname
    (e.g. ces26.mapyourshow.com -> 'CES 2026').
    """
    if soup is not None:
        candidates = []
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            candidates.append(og["content"])
        if soup.title and soup.title.string:
            candidates.append(soup.title.string)
        noise = re.compile(r"exhibitor|directory|gallery|search|list|map your show|mapyourshow|a2z|floor ?plan|login", re.I)
        for cand in candidates:
            for part in re.split(r"\s+[|\-–—:]\s+", cand):
                part = part.strip()
                if part and not noise.search(part) and len(part) <= 60:
                    return part

    host = urlparse(url).netloc.lower()
    label = host.split(".")[0] if host else ""
    m = re.match(r"^([a-z]+?)[-_]?(\d{2}|\d{4})?$", label)
    if m:
        base, year = m.group(1), m.group(2)
        name = KNOWN_SHOWS.get(base, base.upper())
        if year:
            year = year if len(year) == 4 else f"20{year}"
            return f"{name} {year}"
        return name
    return "Trade Show"


def match_target_show(convention: str) -> dict | None:
    """'NAB Show 2027' or 'NAB 2027' -> the TARGET_SHOWS entry, if any."""
    raw = (convention or "").lower()
    year_in_key = re.search(r"(20\d\d)", raw)
    key = re.sub(r"20\d\d", "", re.sub(r"[^a-z0-9]", "", raw))
    if len(key) < 3:
        return None
    for show in TARGET_SHOWS:
        show_year = show["start"][:4]
        if year_in_key and year_in_key.group(1) != show_year:
            continue  # "NAB Show 2026" is last year's directory, not this target
        stem = re.sub(r"20\d\d", "", re.sub(r"[^a-z0-9]", "", show["name"].lower()))
        if key == stem or stem.startswith(key) or key.startswith(stem):
            return show
    return None


def fetch_html(url: str) -> str:
    """GET the directory page; translate blocks into ScrapeError."""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise ScrapeError(f"Request timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.ConnectionError:
        raise ScrapeError("Could not connect to the directory host")
    except requests.exceptions.RequestException as exc:
        raise ScrapeError(f"Request failed: {exc.__class__.__name__}")

    if resp.status_code in (401, 403, 429, 503):
        raise ScrapeError(f"Blocked by anti-bot protection (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise ScrapeError(f"Directory returned HTTP {resp.status_code}")
    if "text/html" not in resp.headers.get("Content-Type", "text/html"):
        raise ScrapeError("URL did not return an HTML page")
    return resp.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalise_website(href: str | None, base_url: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "#", "tel:")):
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http"):
        href = urljoin(base_url, href)
    return href


def _external_link(container, base_url: str) -> str:
    """First link in `container` that points off the directory's own host."""
    own_host = urlparse(base_url).netloc.lower().replace("www.", "")
    for a in container.find_all("a", href=True):
        href = _normalise_website(a["href"], base_url)
        host = urlparse(href).netloc.lower().replace("www.", "")
        if host and host != own_host and "mapyourshow" not in host and "a2z" not in host:
            return href
    return ""


def _booth_from_container(container) -> str:
    """Look for a booth number inside a card/row: class hints first, regex second."""
    for el in container.find_all(True, class_=re.compile("booth", re.I)):
        txt = _clean(el.get_text(" "))
        m = re.search(r"([A-Z]{0,2}-?\d{2,6}[A-Z]?)", txt)
        if m:
            return m.group(1)
    for attr in ("data-booth", "data-boothnumber", "data-booth-number"):
        if container.has_attr(attr):
            return _clean(container[attr])
    m = BOOTH_NO_RE.search(_clean(container.get_text(" ")))
    return m.group(1) if m else ""


def _card_rows(anchors, base_url: str) -> list[dict]:
    """
    Shared logic for MapYourShow / A2Z-style card or row layouts.

    Each exhibitor link sits inside a card (or table row) that also holds the
    booth number and website. We find that card by climbing to the deepest
    ancestor that contains only this one exhibitor link -- one level higher
    and we'd be looking at the whole list.
    """
    MAX_CLIMB = 6
    shared: dict[int, int] = {}  # id(ancestor) -> how many exhibitor links it contains
    for a in anchors:
        node = a
        for _ in range(MAX_CLIMB):
            node = node.parent
            if node is None:
                break
            shared[id(node)] = shared.get(id(node), 0) + 1

    rows = []
    for a in anchors:
        name = _clean(a.get_text(" "))
        if not name or len(name) > 90:
            continue
        container = a
        for _ in range(MAX_CLIMB):
            parent = container.parent
            if parent is None or parent.name in ("body", "html", "[document]") or shared.get(id(parent), 0) > 1:
                break
            container = parent
        booth = _booth_from_container(container)
        size = extract_booth_size(_clean(container.get_text(" ")))
        rows.append(
            {
                "Company": name,
                "Website": _external_link(container, base_url),
                "Booth": booth,
                "Booth Size": size[0] if size else "",
                "Sq Ft": size[1] if size else 0,
                "Size Source": "scraped" if size else "",
            }
        )
    return rows


def _parse_mapyourshow(soup: BeautifulSoup, base_url: str) -> list[dict]:
    anchors = soup.find_all("a", href=re.compile(r"exhibitor-details|/exhibitor/|exhid=", re.I))
    return _card_rows(anchors, base_url)


def _parse_a2z(soup: BeautifulSoup, base_url: str) -> list[dict]:
    anchors = soup.find_all("a", href=re.compile(r"eBooth\.aspx|BoothID=|exhibitor", re.I))
    return _card_rows(anchors, base_url)


def _parse_tables(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Any HTML table with a company/exhibitor column and a booth column."""
    rows = []
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [_clean(th.get_text(" ")).lower() for th in header_row.find_all(["th", "td"])]
        if not headers:
            continue
        name_idx = next((i for i, h in enumerate(headers) if re.search(r"company|exhibitor|name", h)), None)
        booth_idx = next((i for i, h in enumerate(headers) if "booth" in h and "size" not in h), None)
        size_idx = next((i for i, h in enumerate(headers) if re.search(r"size|sq|dimension", h)), None)
        site_idx = next((i for i, h in enumerate(headers) if re.search(r"web|site|url", h)), None)
        if name_idx is None:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= name_idx:
                continue
            name = _clean(cells[name_idx].get_text(" "))
            if not name:
                continue
            booth = _clean(cells[booth_idx].get_text(" ")) if booth_idx is not None and len(cells) > booth_idx else _booth_from_container(tr)
            size_text = _clean(cells[size_idx].get_text(" ")) if size_idx is not None and len(cells) > size_idx else _clean(tr.get_text(" "))
            size = extract_booth_size(size_text)
            website = ""
            if site_idx is not None and len(cells) > site_idx:
                link = cells[site_idx].find("a", href=True)
                website = _normalise_website(link["href"] if link else _clean(cells[site_idx].get_text()), base_url)
            if not website:
                website = _external_link(tr, base_url)
            rows.append(
                {
                    "Company": name,
                    "Website": website,
                    "Booth": booth,
                    "Booth Size": size[0] if size else "",
                    "Sq Ft": size[1] if size else 0,
                    "Size Source": "scraped" if size else "",
                }
            )
    return rows


def _parse_generic_cards(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Elements whose class mentions 'exhibitor' -- covers most custom directories."""
    rows = []
    for card in soup.find_all(True, class_=re.compile(r"exhibitor|company-card|vendor", re.I)):
        heading = card.find(["h1", "h2", "h3", "h4", "h5", "strong", "a"])
        if not heading:
            continue
        name = _clean(heading.get_text(" "))
        if not name or len(name) > 90:
            continue
        size = extract_booth_size(_clean(card.get_text(" ")))
        rows.append(
            {
                "Company": name,
                "Website": _external_link(card, base_url),
                "Booth": _booth_from_container(card),
                "Booth Size": size[0] if size else "",
                "Sq Ft": size[1] if size else 0,
                "Size Source": "scraped" if size else "",
            }
        )
    return rows


PARSERS: list[Callable[[BeautifulSoup, str], list[dict]]] = [
    _parse_mapyourshow, _parse_a2z, _parse_tables, _parse_generic_cards,
]


def parse_exhibitors(html: str, base_url: str) -> list[dict]:
    """
    Try every parser and keep whichever found the most exhibitors. Deduplicate
    by company name and fill in an estimated booth size where the page did
    not publish one.
    """
    soup = BeautifulSoup(html, "html.parser")
    best: list[dict] = []
    for parser in PARSERS:
        try:
            found = parser(soup, base_url)
        except Exception:  # a single broken parser must never sink the scrape
            found = []
        if len(found) > len(best):
            best = found

    seen, rows = set(), []
    for row in best:
        key = row["Company"].lower()
        if key in seen:
            continue
        seen.add(key)
        if not row["Sq Ft"]:
            # No dimensions published: leave the size unknown rather than invent one.
            row["Booth Size"], row["Sq Ft"], row["Size Source"] = "", 0, "unknown"
        row.setdefault("Hall", "")
        row.setdefault("Description", "")
        row.setdefault("Detail URL", "")
        rows.append(row)

    if len(rows) < MIN_ROWS_FOR_VALID_SCRAPE:
        raise ScrapeError(
            "No exhibitor rows found in the HTML. The directory is most likely "
            "rendered client-side (JavaScript) or behind a bot challenge. "
            "MapYourShow directories are fully supported; for other platforms "
            "paste a page that lists exhibitors in a plain HTML table or card grid."
        )
    return rows


# -----------------------------------------------------------------------------
# MapYourShow (the platform behind CES, NAB Show, World of Concrete, PACK EXPO,
# AHR, TISE and most other big Las Vegas shows).
#
# The public directory is a Vue app, so the HTML carries no exhibitor rows.
# The data comes from three JSON endpoints that answer to a plain GET with an
# X-Requested-With header and no cookies:
#
#   1. /8_0/ajax/remote-proxy.cfm?action=getsearchoptions&function=getBoothHalls
#        -> every hall in the show: {fieldvalue: "C", fielddisplay: "Central Hall"}
#   2. /8_0/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery&searchsize=20000
#        -> the complete exhibitor list in one call (name, exhibitor id, booth
#           numbers, hall ids, description)
#   3. /8_0/floorplan/02/_remote-proxy.cfm?showid=NAB27&hallid=C&action=GetBoothByHall
#        -> every booth polygon in that hall with boothWidth / boothHeight
#           (inches) and area (sq ft), plus the exhibitor id that holds it.
#
# Joining 2 and 3 on the exhibitor id gives exact booth footprints for the
# whole show -- the number this entire tool is built around.
# -----------------------------------------------------------------------------

MYS_JSON_HEADERS = {**REQUEST_HEADERS, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
MYS_MAX_WORKERS = 6


def mys_base(url: str) -> tuple[str, str]:
    """'https://nab27.mapyourshow.com/8_0/explore/...' -> ('https://nab27.mapyourshow.com', '8_0')."""
    p = urlparse(url)
    m = re.search(r"/(\d+_\d+)/", p.path)
    return f"{p.scheme or 'https'}://{p.netloc}", (m.group(1) if m else "8_0")


MYS_SESSION = requests.Session()  # keeps the site's cookies between the gallery and floor-plan calls


def _mys_get(url: str, params: dict, referer: str) -> requests.Response:
    origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    headers = {**MYS_JSON_HEADERS, "Referer": referer, "Origin": origin,
               "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    try:
        return MYS_SESSION.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT * 3)
    except requests.exceptions.RequestException as exc:
        raise ScrapeError(f"MapYourShow request failed: {exc.__class__.__name__}")


def _mys_json(url: str, params: dict, referer: str) -> dict:
    resp = _mys_get(url, params, referer)
    if resp.status_code != 200:
        raise ScrapeError(f"MapYourShow returned HTTP {resp.status_code} for {params.get('action', 'request')}")
    try:
        return resp.json()
    except ValueError:
        raise ScrapeError("MapYourShow returned a non-JSON response (has the show code changed?)")


# Per-hall diagnostics for the last floor-plan pull (shown in the status log
# when a show comes back with no booth geometry, so the reason is visible).
FLOORPLAN_DIAG: list[dict] = []


def _mys_booths_for_hall(booth_url: str, showid: str, hall: str, referer: str) -> list[dict]:
    """All booth records in one hall, with dimensions parsed out of FEATUREPROPERTIES."""
    resp = _mys_get(booth_url, {"showid": showid, "selectedbooth": "", "hallid": hall,
                                "action": "GetBoothByHall", "method": "GetBoothByHall", "regid": 0}, referer)
    diag = {"hall": hall, "url": booth_url, "status": resp.status_code,
            "type": resp.headers.get("Content-Type", ""), "bytes": len(resp.content),
            "head": resp.text[:240].replace("\n", " ")}
    try:
        data = resp.json() if resp.status_code == 200 else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    cols = data.get("COLUMNS") or []
    rows_raw = data.get("DATA") or []
    diag.update({"rows": len(rows_raw), "cols": cols[:6]})
    out = []
    for raw in rows_raw:
        rec = dict(zip(cols, raw))
        if rec.get("OBJECTTYPE") != "booth" or not rec.get("EXHID"):
            continue
        props = rec.get("FEATUREPROPERTIES") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except ValueError:
                props = {}
        props = props.get("properties") or {}
        try:
            area = float(props.get("area") or 0)
        except (TypeError, ValueError):
            area = 0.0
        width_in, depth_in = props.get("boothWidth"), props.get("boothHeight")
        if not area and width_in and depth_in:
            area = float(width_in) * float(depth_in) / 144.0
        out.append({
            "exhid": str(rec["EXHID"]),
            "name": _clean(str(rec.get("EXHNAME") or "")),
            "booth": _clean(str(rec.get("BOOTHDISPLAY") or rec.get("BOOTH") or "")),
            "hall": hall,
            "status": rec.get("BOOTHSTATUS") or "",
            "area": round(area),
            "width_ft": round(float(width_in) / 12, 1) if width_in else None,
            "depth_ft": round(float(depth_in) / 12, 1) if depth_in else None,
        })
    diag["booths_with_exhibitor"] = len(out)
    FLOORPLAN_DIAG.append(diag)
    print(f"[floorplan] {diag}")  # lands in the Streamlit Cloud log for debugging
    if resp.status_code != 200:
        raise ScrapeError(f"MapYourShow floor plan returned HTTP {resp.status_code} for hall {hall}")
    return out


def _strip_booth(value) -> str:
    """Gallery booth numbers arrive as 'C8520randomstring' -- MapYourShow pads them; the floor plan has the clean value."""
    return re.sub(r"randomstring$", "", _clean(str(value or "")), flags=re.I)


def _fmt_ft(v: float | None) -> str:
    return "" if v is None else (f"{int(v)}" if float(v).is_integer() else f"{v:g}")


def scrape_mapyourshow(url: str) -> tuple[list[dict], dict]:
    """Full MapYourShow pull: halls + exhibitor gallery + booth geometry, joined by exhibitor id."""
    origin, root = mys_base(url)
    proxy = f"{origin}/{root}/ajax/remote-proxy.cfm"
    referer = f"{origin}/{root}/explore/exhibitor-gallery.cfm"

    # 1. Halls ---------------------------------------------------------------
    halls_json = _mys_json(proxy, {"action": "getsearchoptions", "function": "getBoothHalls"}, referer)
    halls = {str(h.get("fieldvalue")): str(h.get("fielddisplay") or h.get("fieldvalue"))
             for h in (halls_json.get("DATA") or []) if h.get("fieldvalue")}

    # 2. Exhibitor gallery (all of it) ---------------------------------------
    gallery = _mys_json(proxy, {"action": "search", "searchtype": "exhibitorgallery", "searchsize": 20000}, referer)
    try:
        hits = gallery["DATA"]["results"]["exhibitor"]["hit"]
    except (KeyError, TypeError):
        raise ScrapeError("MapYourShow gallery search returned an unexpected shape.")
    exhibitors: dict[str, dict] = {}
    for hit in hits:
        f = hit.get("fields") or {}
        exhid = str(f.get("exhid_l") or hit.get("id") or "").strip()
        name = _clean(str(f.get("exhname_t") or ""))
        if not exhid or not name:
            continue
        exhibitors[exhid] = {
            "exhid": exhid,
            "Company": name,
            "Booth": ", ".join(_strip_booth(b) for b in (f.get("boothsdisplay_la") or f.get("booths_la") or []) if _strip_booth(b)),
            "Halls": [str(h) for h in (f.get("hallid_la") or [])],
            "Description": _clean(str(f.get("exhdesc_t") or ""))[:400],
        }
    if not exhibitors:
        raise ScrapeError("MapYourShow returned zero exhibitors -- the directory may not be published yet.")

    # 3. Show id + floor-plan app version (from the floor-plan page) ----------
    showid = urlparse(origin).netloc.split(".")[0].upper()
    fpver = "02"
    fp_referer = f"{origin}/{root}/floorplan/"
    try:
        fp_html = MYS_SESSION.get(fp_referer, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT).text
        m = re.search(r'ShowID\s*=\s*"([^"]+)"', fp_html)
        if m:
            showid = m.group(1)
        m = re.search(r"floorplan/(\d{2})/", fp_html)
        if m:
            fpver = m.group(1)
    except requests.exceptions.RequestException:
        pass  # fall back to the subdomain-derived show id

    # 4. Booth geometry, hall by hall (only halls that actually hold exhibitors)
    # The legacy "02" floor-plan proxy answers for every show we have seen,
    # including shows whose public floor plan already runs the newer "03" app
    # (NAB 2027), so it goes first; the detected version is the fallback.
    wanted = sorted({h for ex in exhibitors.values() for h in ex["Halls"] if h in halls}) or list(halls)
    booths_by_exh: dict[str, list[dict]] = {}
    hall_errors = 0
    FLOORPLAN_DIAG.clear()
    for ver in ["02"] + ([fpver] if fpver != "02" else []):
        booth_url = f"{origin}/{root}/floorplan/{ver}/_remote-proxy.cfm"
        booths_by_exh, hall_errors = {}, 0
        if not wanted:
            break
        with ThreadPoolExecutor(max_workers=MYS_MAX_WORKERS) as pool:
            for result in pool.map(lambda h: _safe_hall(booth_url, showid, h, fp_referer), wanted):
                if result is None:
                    hall_errors += 1
                    continue
                for b in result:
                    booths_by_exh.setdefault(b["exhid"], []).append(b)
        if booths_by_exh:
            break

    # 5. Join ----------------------------------------------------------------
    rows = []
    for exhid, ex in exhibitors.items():
        booths = booths_by_exh.get(exhid, [])
        if booths:
            total = int(round(sum(b["area"] for b in booths)))
            biggest = max(booths, key=lambda b: b["area"])
            if biggest["width_ft"] and biggest["depth_ft"]:
                dims = f"{_fmt_ft(biggest['width_ft'])}x{_fmt_ft(biggest['depth_ft'])}"
            else:
                dims = f"{biggest['area']} sq ft"
            if len(booths) > 1:
                dims += f" (+{len(booths) - 1})"
            hall_names = sorted({halls.get(b["hall"], b["hall"]) for b in booths})
            booth_no = ", ".join(sorted({b["booth"] for b in booths if b["booth"]})) or ex["Booth"]
            source = "floorplan"
        else:
            total, dims, source = 0, "", "unknown"
            hall_names = [halls.get(h, h) for h in ex["Halls"]]
            booth_no = ex["Booth"]
        rows.append({
            "Company": ex["Company"],
            "Website": "",
            "Booth": booth_no,
            "Booth Size": dims,
            "Sq Ft": total,
            "Size Source": source,
            "Hall": "; ".join(hall_names),
            "Description": ex["Description"],
            "Detail URL": f"{origin}/{root}/exhibitor/exhibitor-details.cfm?exhid={exhid}",
        })

    # Floor-plan booths whose exhibitor is not (yet) in the public gallery.
    for exhid, booths in booths_by_exh.items():
        if exhid in exhibitors:
            continue
        name = next((b["name"] for b in booths if b["name"] and b["name"].lower() != "unassigned"), "")
        if not name:
            continue
        total = int(round(sum(b["area"] for b in booths)))
        biggest = max(booths, key=lambda b: b["area"])
        dims = (f"{_fmt_ft(biggest['width_ft'])}x{_fmt_ft(biggest['depth_ft'])}"
                if biggest["width_ft"] and biggest["depth_ft"] else f"{biggest['area']} sq ft")
        rows.append({
            "Company": name, "Website": "",
            "Booth": ", ".join(sorted({b["booth"] for b in booths if b["booth"]})),
            "Booth Size": dims, "Sq Ft": total, "Size Source": "floorplan",
            "Hall": "; ".join(sorted({halls.get(b["hall"], b["hall"]) for b in booths})),
            "Description": "",
            "Detail URL": f"{origin}/{root}/exhibitor/exhibitor-details.cfm?exhid={exhid}",
        })

    rows.sort(key=lambda r: r["Company"].lower())
    convention = infer_convention_name(url)
    try:
        page = requests.get(referer, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        if page.status_code == 200:
            convention = infer_convention_name(url, BeautifulSoup(page.text, "html.parser"))
    except requests.exceptions.RequestException:
        pass
    meta = {
        "convention": convention,
        "platform": "MapYourShow",
        "source": "live",
        "url": url,
        "halls": len(wanted),
        "hall_errors": hall_errors,
        "sized": sum(1 for r in rows if r["Size Source"] == "floorplan"),
        "showid": showid,
        "fpver": fpver,
        "diag": list(FLOORPLAN_DIAG)[:4],
    }
    return rows, meta


def _safe_hall(booth_url: str, showid: str, hall: str, referer: str) -> list[dict] | None:
    try:
        return _mys_booths_for_hall(booth_url, showid, hall, referer)
    except ScrapeError:
        return None


WEBSITE_RE = re.compile(r'websiteValue:\s*"([^"]*)"')


def fetch_mys_website(detail_url: str) -> str:
    """The exhibitor detail page is server-rendered and carries websiteValue: "https://..."."""
    if not detail_url:
        return ""
    try:
        resp = MYS_SESSION.get(detail_url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return ""
        m = WEBSITE_RE.search(resp.text)
        if not m:
            return ""
        site = m.group(1).replace("\\/", "/").strip()
        return site if site.startswith("http") else (f"https://{site}" if site else "")
    except requests.exceptions.RequestException:
        return ""


def fetch_websites(detail_urls: list[str]) -> list[str]:
    with ThreadPoolExecutor(max_workers=MYS_MAX_WORKERS) as pool:
        return list(pool.map(fetch_mys_website, detail_urls))


def scrape_directory(url: str) -> tuple[list[dict], dict]:
    """
    Fetch + parse a directory URL. Returns (rows, meta). Raises ScrapeError
    with a plain-English reason when the site cannot be read. Results are
    cached for an hour so tweaking the sidebar never re-hits the show's
    servers -- except a MapYourShow pull that came back without any booth
    geometry (a transient floor-plan hiccup), which is dropped from the cache
    so the next click retries live instead of showing the miss for an hour.
    """
    rows, meta = _scrape_directory_cached(url)
    if meta.get("platform") == "MapYourShow" and not meta.get("sized"):
        _scrape_directory_cached.clear()
    return rows, meta


@st.cache_data(ttl=3600, show_spinner=False)
def _scrape_directory_cached(url: str) -> tuple[list[dict], dict]:
    if detect_platform(url) == "MapYourShow":
        return scrape_mapyourshow(url)
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    rows = parse_exhibitors(html, url)
    meta = {
        "convention": infer_convention_name(url, soup),
        "platform": detect_platform(url),
        "source": "live",
        "url": url,
        "halls": 0,
        "hall_errors": 0,
        "sized": sum(1 for r in rows if r["Size Source"] == "scraped"),
    }
    return rows, meta


# =============================================================================
# 4. ENRICHMENT ENGINE  (simulated Apollo / Clearbit + real Apollo slot-in)
# =============================================================================


def infer_industry(company: str, rng: random.Random, description: str = "") -> str:
    """Keyword match on the company name first, then its directory description."""
    for text in (company.lower(), (description or "").lower()):
        if not text:
            continue
        for pattern, industry in INDUSTRY_PATTERNS:
            if re.search(pattern, text):
                return industry
    return rng.choice(GENERIC_INDUSTRIES)


def recommend_titles(headcount: int) -> list[str]:
    """
    Which decision-maker to go after, by company size. At a 60-person company
    the CMO signs off on the booth; at 400 people there is a dedicated trade
    show manager and the CMO never sees the RFP.
    """
    if headcount < 150:
        return ["Chief Marketing Officer", "Event Marketing Manager", "Trade Show Manager"]
    if headcount <= 500:
        return ["Event Marketing Manager", "Trade Show Manager", "Director of Field Marketing"]
    return ["Trade Show Manager", "Director of Field Marketing", "Event Marketing Manager"]


def simulate_enrichment(company: str, sqft: int, description: str = "") -> dict:
    """
    Stand-in for an Apollo / Clearbit organisation-enrichment call. The company
    name, booth and description are real; only revenue and headcount are
    modelled until an Apollo key is present.

    Deterministic: the same company always gets the same numbers, so the demo
    is stable across reruns and the sidebar filters behave predictably. Booth
    footprint is used as a prior because it correlates with company scale on
    a real show floor (a 50x50 island is not a 12-person startup). Unknown
    footprint (0) is treated like a small inline booth.
    """
    seed = int(hashlib.md5(company.lower().encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)

    if sqft <= 100:
        revenue = rng.uniform(1.5, 24)
    elif sqft <= 300:
        revenue = rng.uniform(6, 70)
    elif sqft <= 600:
        revenue = rng.uniform(12, 160)
    elif sqft <= 1000:
        revenue = rng.uniform(60, 400)
    else:
        revenue = rng.uniform(250, 3000)

    revenue_per_employee = rng.uniform(0.16, 0.42)  # $M per head, typical for hardware/tech
    headcount = max(5, int(revenue / revenue_per_employee))

    return {
        "Revenue ($M)": round(revenue, 1),
        "Headcount": headcount,
        "Industry": infer_industry(company, rng, description),
        "Enrichment": "simulated",
    }


def _apollo_headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "accept": "application/json", "Cache-Control": "no-cache",
            "Content-Type": "application/json"}


LEGAL_SUFFIX_RE = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|ag|sa|srl|plc|lp|llp|group|holdings)\b\.?", re.I)


def _name_key(name: str) -> str:
    """'3Play Media, Inc.' -> '3playmedia' so directory names and registry names compare cleanly."""
    return re.sub(r"[^a-z0-9]", "", LEGAL_SUFFIX_RE.sub("", (name or "").lower()))


@st.cache_data(ttl=86400, show_spinner=False, persist="disk")
def find_domain_clearbit(company: str) -> str:
    """
    Company name -> domain, no key, no credits: Clearbit's public autocomplete.

    Trade-show directories rarely publish a website (NAB's do not) and every
    Apollo call is keyed on the domain, so this runs first for leads without
    one. The endpoint returns up to five fuzzy suggestions; only a suggestion
    whose name matches the exhibitor's (after dropping punctuation and legal
    suffixes) is accepted, so '16x9, Inc.' picks 16x9inc.com and not the
    unrelated first hit.
    """
    if not company:
        return ""
    try:
        resp = requests.get("https://autocomplete.clearbit.com/v1/companies/suggest",
                            params={"query": company}, headers=REQUEST_HEADERS, timeout=8)
        if resp.status_code != 200:
            return ""
        want = _name_key(company)
        if not want:
            return ""
        hits = [(_name_key(str(h.get("name") or "")), str(h.get("domain") or "").strip().lower())
                for h in (resp.json() or []) if h.get("domain")]
        for got, domain in hits:            # 1. exact name match
            if got == want:
                return domain
        for got, domain in hits:            # 2. one name extends the other ('3Play Media' vs '3Play Media Inc')
            shorter = min(len(got), len(want))
            if shorter >= max(4, 0.6 * max(len(got), len(want))) and (got.startswith(want) or want.startswith(got)):
                return domain
        return ""
    except Exception:
        return ""


# -----------------------------------------------------------------------------
# Tavily web search: makes the app work for any convention in the country.
#   - find_directories_tavily(): show name -> candidate exhibitor-directory URLs
#   - find_domain_tavily():      company name -> website, when Clearbit misses
# Key: https://app.tavily.com -> API keys; free tier is 1,000 credits a month.
# Store it as TAVILY_API_KEY next to the Apollo key.
# -----------------------------------------------------------------------------

TAVILY_URL = "https://api.tavily.com/search"
NOT_A_COMPANY_SITE = (
    "linkedin.", "facebook.", "instagram.", "x.com", "twitter.", "youtube.", "wikipedia.", "crunchbase.",
    "zoominfo.", "bloomberg.", "glassdoor.", "indeed.", "yelp.", "bbb.org", "dnb.com", "rocketreach.",
    "manta.", "opencorporates.", "google.", "amazon.", "apollo.io", "mapyourshow.", "a2zinc.", "expocad.",
    "tiktok.", "pinterest.", "reddit.", "trustpilot.", "owler.", "craft.co", "pitchbook.", "cbinsights.",
)


def _tavily_search(query: str, api_key: str, max_results: int = 6, include_domains: list[str] | None = None) -> list[dict]:
    """POST https://api.tavily.com/search with Authorization: Bearer <key>. Returns [] on any failure."""
    if not query or not api_key:
        return []
    body = {"query": query, "max_results": max_results, "search_depth": "basic", "topic": "general"}
    if include_domains:
        body["include_domains"] = include_domains
    try:
        resp = requests.post(TAVILY_URL, json=body, timeout=15,
                             headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        if resp.status_code != 200:
            return []
        return [r for r in (resp.json().get("results") or []) if r.get("url")]
    except Exception:
        return []


@st.cache_data(ttl=86400, show_spinner=False)
def find_directories_tavily(show_name: str, api_key: str) -> list[dict]:
    """
    Any convention, any city: 'IMTS 2026' -> the exhibitor-directory URLs the
    web knows about, MapYourShow first (exact booth sizes), then A2Z/ExpoCAD,
    then anything whose address or title says 'exhibitor'.
    """
    seen, out = set(), []
    for query in (f"{show_name} exhibitor list", f"{show_name} exhibitor directory floor plan booth"):
        for hit in _tavily_search(query, api_key, max_results=8):
            url = hit["url"].split("#")[0]
            host = urlparse(url).netloc.lower()
            title = _clean(str(hit.get("title") or ""))
            platform = detect_platform(url)
            looks_like_directory = platform != "Generic HTML" or re.search(r"exhibitor", url + " " + title, re.I)
            if not looks_like_directory or url in seen:
                continue
            seen.add(url)
            if platform == "MapYourShow" and "exhibitor-gallery" not in url:
                base, root = mys_base(url)
                url = f"{base}/{root}/explore/exhibitor-gallery.cfm?featured=false"  # the list, not the floor plan
                if url in seen:
                    continue
                seen.add(url)
            out.append({"title": title or host, "url": url, "platform": platform, "host": host})
    rank = {"MapYourShow": 0, "A2Z Events": 1, "ExpoCAD": 2}
    out.sort(key=lambda c: rank.get(c["platform"], 3))
    return out[:8]


@st.cache_data(ttl=86400, show_spinner=False, persist="disk")
def find_domain_tavily(company: str, api_key: str) -> str:
    """Company name -> website via web search, used only when Clearbit has no match. 1 Tavily credit."""
    want = _name_key(company)
    if not want or not api_key:
        return ""
    probe = want[:6]
    for hit in _tavily_search(f'"{company}" official website', api_key, max_results=6):
        host = urlparse(hit["url"]).netloc.lower().replace("www.", "")
        if not host or any(bad in host for bad in NOT_A_COMPANY_SITE):
            continue
        title_key = _name_key(str(hit.get("title") or ""))
        host_key = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
        if want in title_key or probe in host_key or host_key in want:
            return host
    return ""


@st.cache_data(ttl=86400, show_spinner=False, persist="disk")
def find_domain_apollo(company: str, api_key: str) -> str:
    """
    PRODUCTION PATH (paid plans) -- Apollo Organization Search, name -> domain.

    Second choice after find_domain_clearbit(): Apollo's Free plan does not
    expose this endpoint at all (the key-creation screen lists
    mixed_companies/search among the paid-only APIs), so on Free it simply
    returns nothing and costs nothing.

    Endpoint: POST https://api.apollo.io/api/v1/mixed_companies/search
    Query:    q_organization_name=<company>&page=1&per_page=1
    Header:   x-api-key: <master key>
    Cost:     1 credit per call (per page). Cached 24h.
    Mapped:   organizations[0].primary_domain (fallback: website_url host)
    """
    if not company or not api_key:
        return ""
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_companies/search",
            params={"q_organization_name": company, "page": 1, "per_page": 1},
            headers=_apollo_headers(api_key), timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        orgs = body.get("organizations") or body.get("accounts") or []
        if not orgs:
            return ""
        org = orgs[0]
        domain = str(org.get("primary_domain") or "").strip()
        if not domain:
            domain = urlparse(str(org.get("website_url") or "")).netloc.replace("www.", "")
        return domain
    except Exception:
        return ""


@st.cache_data(ttl=86400, show_spinner=False, persist="disk")
def enrich_company_apollo(domain: str, api_key: str) -> dict | None:
    """
    PRODUCTION PATH -- real Apollo.io organisation enrichment.

    How to wire it up:
      1. Create a key at https://app.apollo.io/#/settings/integrations/api
         (tick "Set as master API key": search + enrichment endpoints need it)
      2. Put it in .streamlit/secrets.toml (locally) or App settings -> Secrets
         on Streamlit Community Cloud:
             APOLLO_API_KEY = "xxxxxxxx"
         (or paste it in the sidebar for a one-off session)
      3. That's it -- after a scrape, apply_apollo() runs this for the top
         leads: domain lookup (if needed) -> organisation enrich -> people search.

    Endpoint: GET https://api.apollo.io/api/v1/organizations/enrich?domain=acme.com
    Header:   x-api-key: <key>
    Cost:     1 credit per organisation. Cached 24h so re-runs don't burn credits.
    Response fields we map:
        organization.annual_revenue          -> Revenue ($M)   (raw dollars / 1e6)
        organization.estimated_num_employees -> Headcount
        organization.industry                -> Industry

    Clearbit equivalent (if you prefer it):
        GET https://company.clearbit.com/v2/companies/find?domain=acme.com
        Authorization: Bearer <key>
        metrics.estimatedAnnualRevenue / metrics.employees / category.industry

    Rate limits: Apollo allows ~50 enrich calls/min on entry plans, so for a
    2,000-exhibitor show you would batch this (Apollo also has a bulk endpoint:
    POST /api/v1/organizations/bulk_enrich with up to 10 domains per call).
    """
    if not domain or not api_key:
        return None
    try:
        resp = requests.get(
            "https://api.apollo.io/api/v1/organizations/enrich",
            params={"domain": domain},
            headers=_apollo_headers(api_key),
            timeout=10,
        )
        resp.raise_for_status()
        org = resp.json().get("organization") or {}
        if not org:
            return None
        revenue = org.get("annual_revenue")
        return {
            "Revenue ($M)": round(revenue / 1e6, 1) if revenue else None,
            "Headcount": org.get("estimated_num_employees"),
            "Industry": (org.get("industry") or "").title() or None,
            "Enrichment": "Apollo",
        }
    except Exception:
        # Any API hiccup (bad key, rate limit, network) falls back to simulation
        # for this one company rather than crashing the whole run.
        return None


@st.cache_data(ttl=86400, show_spinner=False, persist="disk")
def find_contact_apollo(domain: str, titles: tuple[str, ...], api_key: str) -> dict | None:
    """
    PRODUCTION PATH -- Apollo People Search, mapping a domain to the person
    who actually owns the trade-show budget.

    Endpoint: POST https://api.apollo.io/api/v1/mixed_people/search
    Query:    q_organization_domains_list[]=acme.com
              person_titles[]=Trade Show Manager&person_titles[]=Event Marketing Manager ...
              page=1&per_page=3
    Header:   x-api-key: <key>   (use a *master* key: Apollo restricts the
              search + enrichment endpoints to master keys on most plans)

    The search result includes name + title + LinkedIn URL. Apollo does not
    hand out the email here (it returns a placeholder such as
    email_not_unlocked@domain.com); a verified address costs a credit and
    comes from a second call:
        POST https://api.apollo.io/api/v1/people/match  {"id": <person id>, "reveal_personal_emails": false}
    Clay users: the same two steps are the "Find people at company" and
    "Enrich person" columns.
    """
    if not domain or not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_people/search",
            params={"q_organization_domains_list[]": [domain], "person_titles[]": list(titles), "page": 1, "per_page": 3},
            headers=_apollo_headers(api_key),
            timeout=10,
        )
        resp.raise_for_status()
        people = resp.json().get("people") or []
        if not people:
            return None
        person = people[0]
        email = str(person.get("email") or "")
        if "email_not_unlocked" in email or "@" not in email:
            email = ""  # placeholder until the person is enriched (1 credit)
        return {
            "Contact": person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
            "Contact Title": person.get("title") or "",
            "Contact Email": email,
            "Contact LinkedIn": person.get("linkedin_url") or "",
            # Free ride: the search payload carries the employer record too.
            "_org": _org_fields(person.get("organization") or {}),
        }
    except Exception:
        return None


def _org_fields(org: dict) -> dict:
    """Map whatever organisation fields Apollo returned onto our columns (None where absent)."""
    revenue = org.get("annual_revenue")
    headcount = org.get("estimated_num_employees")
    try:
        revenue = round(float(revenue) / 1e6, 1) if revenue else None
    except (TypeError, ValueError):
        revenue = None
    try:
        headcount = int(headcount) if headcount else None
    except (TypeError, ValueError):
        headcount = None
    return {
        "Revenue ($M)": revenue,
        "Headcount": headcount,
        "Industry": (str(org.get("industry") or "").title() or None),
    }


CONTACT_COLUMNS = ("Contact", "Contact Title", "Contact Email", "Contact LinkedIn")

# Apollo credit ledger for this server process: domains that have been sent to
# the 1-credit organisation-enrichment endpoint. Cached results are free, so
# this is the number that counts against the monthly allowance.
APOLLO_CHARGED: set[str] = set()
APOLLO_FREE_MONTHLY_CREDITS = 75


def _worth_a_credit(sqft: int, free_headcount: int | None, params: dict | None) -> int:
    """
    Priority for spending a 1-credit organisation enrichment (lower = first).
    Booth size is the one thing we know for certain before spending anything,
    so in-band booths go first; a small booth is only worth a credit when the
    free people-search payload already says the company is big (a big-fish
    candidate). Islands and unplaced exhibitors never get a credit.
    """
    if not params:
        return 0
    b_lo, b_hi = params.get("booth_range", DEFAULTS["booth_range"])
    h_hi = params.get("headcount_range", DEFAULTS["headcount_range"])[1]
    if not sqft or sqft > b_hi:
        return 99
    if b_lo <= sqft <= b_hi:
        return 0
    if free_headcount and free_headcount > h_hi:
        return 1
    return 2


def enrich_companies(rows: list[dict], api_key: str | None = None, progress=None) -> pd.DataFrame:
    """
    Baseline pass: append modelled Revenue / Headcount / Industry and the
    decision-maker titles to every scraped row so the whole show can be
    scored at once. Contacts start blank -- the app never invents a person.

    The live Apollo pass (apply_apollo) runs afterwards on the top-scoring
    leads only, because every Apollo call costs credits and a 4,000-exhibitor
    show does not need 4,000 of them. `api_key` is accepted for backwards
    compatibility; the pipeline passes it to apply_apollo instead.
    """
    enriched = []
    for i, row in enumerate(rows):
        sim = simulate_enrichment(row["Company"], int(row.get("Sq Ft") or 0), row.get("Description", ""))
        titles = recommend_titles(int(sim["Headcount"]))
        contact = {c: "" for c in CONTACT_COLUMNS}
        enriched.append({**row, **sim, **contact, "Target Titles": " > ".join(titles)})
        if progress is not None and (i % 50 == 0 or i + 1 == len(rows)):
            progress(i + 1, len(rows), row["Company"])
    return pd.DataFrame(enriched)


def apply_apollo(leads: pd.DataFrame, api_key: str, companies: list[str], progress=None,
                 tavily_key: str | None = None, budget: int | None = None, params: dict | None = None) -> dict:
    """
    PRODUCTION PATH -- the live Apollo pass, in place, for the named companies,
    ordered so a Free plan (75 credits a month) goes as far as possible:

        1. domain (0 credits)     -> from the directory, else find_domain_clearbit(),
                                     then find_domain_tavily() (if a Tavily key is set),
                                     then find_domain_apollo() (paid Apollo plans only)
        2. people search (0 credits per Apollo's API pricing page)
                                  -> decision-maker name, title, LinkedIn, plus the
                                     employer record riding along in the payload
                                     (headcount / industry / sometimes revenue)
        3. organisation enrichment (1 credit) -> only if revenue is still unknown,
                                     only while the per-scrape budget lasts, and in
                                     priority order: in-band booths first, then small
                                     booths that the free payload says are big
                                     (big-fish candidates); islands never.

    Every call is cached for 24h (on disk), so re-running a scrape or clicking
    "run more" does not re-spend credits. Emails are never revealed here: that
    is 1 credit per person and the people/match endpoint is paid-plan only.
    """
    stats = {"domains": 0, "orgs": 0, "contacts": 0, "tried": 0, "credits": 0, "skipped_budget": 0}
    wanted = set(companies)
    idx = [i for i in leads.index if leads.at[i, "Company"] in wanted]
    pending: list[tuple[int, int, str]] = []  # (priority, row index, domain) for the credit step

    for n, i in enumerate(idx):
        company = str(leads.at[i, "Company"])
        website = str(leads.at[i, "Website"] or "")
        domain = urlparse(website).netloc.replace("www.", "") if website else ""
        if not domain:
            domain = (find_domain_clearbit(company)
                      or (find_domain_tavily(company, tavily_key) if tavily_key else "")
                      or find_domain_apollo(company, api_key))
            if domain:
                leads.at[i, "Website"] = f"https://{domain}"
                stats["domains"] += 1
        if not domain:
            continue
        stats["tried"] += 1

        # Free step: people search, with the employer record it carries.
        titles = recommend_titles(int(leads.at[i, "Headcount"]))
        contact = dict(find_contact_apollo(domain, tuple(titles), api_key) or {})
        free_org = contact.pop("_org", {}) or {}
        free_headcount = free_org.get("Headcount")
        if any(v not in (None, "") for v in free_org.values()):
            for k, v in free_org.items():
                if v not in (None, ""):
                    leads.at[i, k] = v
            leads.at[i, "Enrichment"] = "Apollo (search)"
        if contact:
            for k, v in contact.items():
                leads.at[i, k] = v
            stats["contacts"] += 1

        if free_org.get("Revenue ($M)") in (None, ""):
            pending.append((_worth_a_credit(int(leads.at[i, "Sq Ft"] or 0), free_headcount, params), i, domain))
        else:
            leads.at[i, "Enrichment"] = "Apollo"
            stats["orgs"] += 1
        if progress is not None:
            progress(n + 1, len(idx), company)

    # Credit step, best candidates first, capped by the budget.
    pending.sort(key=lambda t: t[0])
    spent = 0
    for priority, i, domain in pending:
        if priority >= 99:
            continue
        charged_before = domain in APOLLO_CHARGED
        if budget is not None and not charged_before and spent >= budget:
            stats["skipped_budget"] += 1
            continue
        org = enrich_company_apollo(domain, api_key)
        if not charged_before:
            APOLLO_CHARGED.add(domain)
            spent += 1
        if org:
            for k in ("Revenue ($M)", "Headcount", "Industry"):
                if org.get(k) not in (None, ""):
                    leads.at[i, k] = org[k]
            leads.at[i, "Enrichment"] = "Apollo"
            stats["orgs"] += 1
        titles = recommend_titles(int(leads.at[i, "Headcount"]))
        leads.at[i, "Target Titles"] = " > ".join(titles)
    stats["credits"] = spent
    return stats


# =============================================================================
# 5. GOLDILOCKS SCORING
# =============================================================================


def band_points(value: float, lo: float, hi: float, max_pts: float) -> float:
    """
    Full marks inside [lo, hi]; outside, points decay with distance from the
    nearest edge (measured relative to that edge) and hit zero at ~67% off.
    A 10x10 (100 sq ft) against a 200 sq ft floor scores 10/40; a 30x30 (900)
    against a 600 ceiling also scores 10/40; a 50x50 island scores 0.
    """
    if lo <= value <= hi:
        return float(max_pts)
    if value < lo:
        distance = (lo - value) / lo if lo else 1.0
    else:
        distance = (value - hi) / hi if hi else 1.0
    return round(max_pts * max(0.0, 1.0 - 1.5 * distance), 1)


def score_leads(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Add score / tier columns. Pure function -- re-run whenever the sidebar changes."""
    b_lo, b_hi = params["booth_range"]
    r_lo, r_hi = params["revenue_range"]
    h_lo, h_hi = params["headcount_range"]
    out = df.copy()

    out["Booth Pts"] = out["Sq Ft"].apply(lambda v: band_points(v, b_lo, b_hi, SCORE_WEIGHTS["booth"]))
    out["Revenue Pts"] = out["Revenue ($M)"].apply(lambda v: band_points(v, r_lo, r_hi, SCORE_WEIGHTS["revenue"]))
    out["Headcount Pts"] = out["Headcount"].apply(lambda v: band_points(v, h_lo, h_hi, SCORE_WEIGHTS["headcount"]))
    out["Score"] = (out["Booth Pts"] + out["Revenue Pts"] + out["Headcount Pts"]).round().astype(int)

    out["Booth OK"] = out["Sq Ft"].between(b_lo, b_hi)
    out["Revenue OK"] = out["Revenue ($M)"].between(r_lo, r_hi)
    out["Headcount OK"] = out["Headcount"].between(h_lo, h_hi)
    out["Goldilocks"] = out["Booth OK"] & out["Revenue OK"] & out["Headcount OK"]

    # Big fish: a company above the revenue ceiling in a booth no bigger than
    # the Goldilocks ceiling. They are on the floor because they have to be;
    # the flagship build comes later, and this is the shortlist conversation.
    big_min = float(params.get("big_fish_min_revenue") or r_hi)
    out["Big Fish"] = (
        bool(params.get("big_fish", True))
        & ~out["Goldilocks"]
        & (out["Revenue ($M)"] >= big_min)
        & (out["Sq Ft"] > 0)
        & (out["Sq Ft"] <= b_hi)
    )
    out.loc[out["Big Fish"], "Score"] = out.loc[out["Big Fish"], "Score"].clip(lower=BIG_FISH_FLOOR_SCORE)
    out["Qualified"] = out["Goldilocks"] | out["Big Fish"]

    def tier(row):
        if row["Goldilocks"]:
            return "Goldilocks"
        if row["Big Fish"]:
            return "Big fish"
        if not row["Sq Ft"]:
            return "No booth data"
        if row["Score"] >= NEAR_MISS_THRESHOLD:
            return "Near miss"
        return "Out of range"

    out["Tier"] = out.apply(tier, axis=1)
    # Deal value: booth sq ft x $/sq ft. A big fish is valued at the mid-market
    # floor because the build we are pitching is the one after this booth.
    deal_sqft = out["Sq Ft"].where(~out["Big Fish"], out["Sq Ft"].clip(lower=b_lo))
    out["Est. Deal ($)"] = (deal_sqft * params["price_per_sqft"]).astype(int)
    return out.sort_values(["Qualified", "Score"], ascending=[False, False]).reset_index(drop=True)


# =============================================================================
# 6. OUTREACH SYSTEM  (timeline + persona + 4-touch sequence)
# =============================================================================


def outreach_timeline(show_start: date | None, today: date | None = None) -> dict:
    """
    Where a show sits relative to the RFP window. Exhibitors with custom
    builds pick their exhibit house 5-7 months before the floor opens, so:

        > 9 months out   early   (plant the seed, low urgency)
        5-9 months out   prime   (they are mapping out the footprint NOW)
        2-5 months out   late    (house likely chosen; pitch rentals / rush builds)
        < 2 months out   closed  (target next year's show)
    """
    today = today or date.today()
    if show_start is None:
        return {"phase": "unknown", "label": "Set a show date", "days_out": None, "months_out": None,
                "window_open": None, "window_close": None, "first_touch": today}

    days_out = (show_start - today).days
    months_out = days_out / 30.44
    window_open = show_start - timedelta(days=int(WINDOW_OPEN_MONTHS * 30.44))
    window_close = show_start - timedelta(days=int(WINDOW_CLOSE_MONTHS * 30.44))

    if months_out > WINDOW_OPEN_MONTHS:
        phase, label = "early", f"Opens {window_open.strftime('%b %d, %Y')}"
        first_touch = window_open
    elif months_out >= WINDOW_CLOSE_MONTHS:
        phase, label = "prime", "Open now"
        first_touch = today
    elif months_out >= 2:
        phase, label = "late", "Late: pitch rentals"
        first_touch = today
    elif days_out >= 0:
        phase, label = "closed", "Too close"
        first_touch = today
    else:
        phase, label = "past", "Show has passed"
        first_touch = today

    return {
        "phase": phase, "label": label, "days_out": days_out, "months_out": months_out,
        "window_open": window_open, "window_close": window_close, "first_touch": first_touch,
    }


PERSONA_ANGLES = {
    "Trade Show Manager": (
        "Since you'll be the one living with the move-in schedule, we can also walk through how a local "
        "build crew changes install and dismantle -- no waiting on a truck from out of state."
    ),
    "Event Marketing Manager": (
        "Teams in {industry} usually use the rendering to get the exhibit budget signed off internally; "
        "it turns a line item into something leadership can actually picture."
    ),
    "Director of Field Marketing": (
        "If {show} is one of several stops on your 2027 calendar, we can design the build to re-deploy "
        "across shows so the spend works more than once."
    ),
    "Chief Marketing Officer": (
        "For a company your size, the rendering also gives you a clear all-in cost picture before budget "
        "is locked -- no design fees, no surprises."
    ),
}


def _timeline_line(show: str, show_start: date | None, timeline: dict) -> str:
    month = show_start.strftime("%B") if show_start else "next year"
    phase = timeline["phase"]
    if phase == "prime":
        return f"I know you're likely starting to map out your footprint for {show} this {month}"
    if phase == "early":
        months = int(round(timeline["months_out"]))
        return f"{show} is still about {months} months out, but the strongest custom builds get locked in early"
    if phase == "late":
        weeks = max(1, int(timeline["days_out"] // 7))
        return f"With {show} about {weeks} weeks out you may already have a build in motion, but if anything changes"
    return f"As you plan for {show}"


def generate_sequence(lead: pd.Series, show: str, show_start: date | None, venue: str, sender: dict) -> list[dict]:
    """
    Build the 4-touch email sequence for one lead. Every value in braces comes
    from the scraped + enriched row, the show calendar, or the sidebar, so each
    email is specific to that booth, that show, and that decision-maker.

    Touch 1 (day 0):   timeline hook + 3D LED render offer + local advantage
    Touch 2 (day 4):   short bump / referral ask
    Touch 3 (day 10):  the Las Vegas math (freight, drayage, on-site crew)
    Touch 4 (day 18):  close the loop
    """
    company = lead["Company"]
    dims = lead["Booth Size"]
    sqft = int(lead["Sq Ft"])
    booth = lead["Booth"] or "TBD"
    industry = str(lead["Industry"])
    contact = str(lead.get("Contact") or "")
    contact_title = str(lead.get("Contact Title") or "") or recommend_titles(int(lead["Headcount"]))[0]
    first_name = contact.split(" ")[0] if contact else None
    greeting = f"Hi {first_name}," if first_name else f"Hi {company} team,"
    big_fish = str(lead.get("Tier") or "") == "Big fish"

    timeline = outreach_timeline(show_start)
    hook = _timeline_line(show, show_start, timeline)
    angle = PERSONA_ANGLES.get(contact_title, PERSONA_ANGLES["Event Marketing Manager"]).format(industry=industry.lower(), show=show)
    venue_txt = venue or "the convention center"
    # The home-turf pitch only makes sense when the show is in Las Vegas; for
    # every other city the angle is a build that travels and re-deploys.
    local = "vegas" in (venue or "").lower() or "vegas" in show.lower()

    name = sender.get("name") or "[Your name]"
    title = sender.get("title") or "Account Executive"
    phone = sender.get("phone") or ""
    signature = f"{name}\n{title}, {COMPANY_NAME} -- Las Vegas, NV\n{COMPANY_SITE}" + (f"\n{phone}" if phone else "")

    first_touch = timeline["first_touch"]
    send_dates = [first_touch + timedelta(days=d) for d in SEQUENCE_OFFSETS_DAYS]

    if local:
        build_point = (
            f"2. A local build. {COMPANY_NAME} designs, fabricates, installs and dismantles in Las Vegas, which cuts "
            f"cross-country freight, drayage friction and the last-minute on-site emergencies that come with an "
            f"out-of-town exhibit house."
        )
    else:
        build_point = (
            f"2. A build that travels. {COMPANY_NAME} designs and fabricates in Las Vegas and ships show-ready, "
            f"engineered so the same exhibit re-deploys across your calendar instead of being rebuilt for every city."
        )

    if big_fish:
        render_dims = "20x20"
        touch1 = (
            f"{greeting}\n\n"
            f"{hook} -- and I noticed {company} is taking a {dims} ({sqft:,} sq ft) at {show}, Booth {booth}.\n\n"
            f"For a company your size that is usually a 'we need to be in the room' booth rather than the presence "
            f"you'd bring to a flagship show. Nothing wrong with that. But the bigger build is coming at some point, "
            f"at this show or the next one, and that is the conversation we'd like to be on the shortlist for. "
            f"{angle}\n\n"
            f"Two things we'd like to put on the table up front:\n\n"
            f"1. A complimentary 3D LED rendering of what a {render_dims} could look like for {company}: LED wall, "
            f"lighting and demo stations designed around your product line, so the next budget conversation starts "
            f"from a picture rather than a line item. Yours to keep either way.\n"
            f"{build_point}\n\n"
            f"You can see our custom fabrication and turnkey rental work at {COMPANY_SITE}.\n\n"
            f"Would a 15-minute call next week make sense to talk through where {show} sits in your 2027 plans?\n\n"
            f"{signature}"
        )
        subject1 = f"{company} at {show}: beyond the {dims} (Booth {booth})"
        offer_line = f"a free 3D LED rendering of a {render_dims} build for {company}"
    else:
        touch1 = (
            f"{greeting}\n\n"
            f"{hook} -- and I noticed {company} has a {dims} ({sqft:,} sq ft) space at {show}, Booth {booth}.\n\n"
            f"That footprint is the sweet spot where a custom exhibit starts paying for itself in foot traffic, "
            f"and it's the size we build most for {industry.lower()} exhibitors. {angle}\n\n"
            f"Two things we'd like to put on the table up front:\n\n"
            f"1. A complimentary 3D LED rendering of your {dims} at {show}: LED wall, lighting and demo stations "
            f"designed around your product line, so you can see how it lands on the floor before you commit to "
            f"anything. Yours to keep either way.\n"
            f"{build_point}\n\n"
            f"You can see our custom fabrication and turnkey rental work at {COMPANY_SITE}.\n\n"
            f"Would a 15-minute call next week make sense to talk through your {show} plans?\n\n"
            f"{signature}"
        )
        subject1 = f"{company} at {show}: your {dims} space (Booth {booth})"
        offer_line = f"a free 3D LED rendering of your {dims} at {show} (Booth {booth})"

    touch2 = (
        f"{greeting}\n\n"
        f"Quick bump in case this got buried. The offer stands: {offer_line}, no strings attached.\n\n"
        f"If someone else owns the {show} program on your side, could you point me their way?\n\n"
        f"{signature}"
    )
    if local:
        math_label, math_subject = "the Las Vegas math", f"The Las Vegas math on Booth {booth} at {show}"
        touch3 = (
            f"{greeting}\n\n"
            f"One more angle that's worth a minute. Exhibitors shipping a {dims} build into Las Vegas from out of "
            f"state typically pay freight both ways, drayage on every crate, and a labor crew they've never met. "
            f"Because we fabricate and store locally, your {show} exhibit gets built a few miles from {venue_txt} "
            f"and installed by the same team that built it.\n\n"
            f"That usually means fewer surprises on the show floor and a lower all-in number than a design-only "
            f"quote suggests.\n\n"
            f"Want me to put together a rough all-in estimate for a {dims} custom build versus a turnkey rental? "
            f"One short call gets the inputs right.\n\n"
            f"{signature}"
        )
    else:
        math_label, math_subject = "the multi-show math", f"The multi-show math on {company}'s exhibit program"
        touch3 = (
            f"{greeting}\n\n"
            f"One more angle that's worth a minute. A build commissioned for a single show leaves its most "
            f"expensive part, design and fabrication, on the table after four days. A build engineered to "
            f"re-deploy across your calendar spreads that cost over every show you do, and when that calendar "
            f"lands in Las Vegas, the busiest show city in the country, the crew that built it is the crew that "
            f"installs it.\n\n"
            f"That usually means a lower all-in number per show than a design-only quote suggests, and fewer "
            f"surprises on the floor.\n\n"
            f"Want me to put together a rough all-in estimate for a custom build versus a turnkey rental across "
            f"your 2027 shows? One short call gets the inputs right.\n\n"
            f"{signature}"
        )
    touch4 = (
        f"{greeting}\n\n"
        f"I'll close the loop here so I'm not cluttering your inbox. If a custom build or a turnkey rental "
        f"for {show}{' or the shows after it' if big_fish else ''} comes up on your side, the 3D rendering offer "
        f"stands -- just reply and we'll get it moving.\n\n"
        f"Best of luck with the show.\n\n"
        f"{signature}"
    )

    return [
        {"step": 1, "label": "Touch 1: timeline + render offer" + (" (big fish)" if big_fish else ""), "send_date": send_dates[0],
         "subject": subject1, "body": touch1},
        {"step": 2, "label": "Touch 2: bump (+4 days)", "send_date": send_dates[1],
         "subject": f"Re: {subject1}", "body": touch2},
        {"step": 3, "label": f"Touch 3: {math_label} (+10 days)", "send_date": send_dates[2],
         "subject": math_subject, "body": touch3},
        {"step": 4, "label": "Touch 4: close the loop (+18 days)", "send_date": send_dates[3],
         "subject": f"Closing the loop on {show}", "body": touch4},
    ]


def build_campaign_export(leads: pd.DataFrame, show: str, show_start: date | None, venue: str, sender: dict) -> pd.DataFrame:
    """
    One row per lead with the full 4-touch sequence, ready to import into
    Apollo Sequences, Instantly, Lemlist or a Clay table.
    """
    records = []
    for _, lead in leads.iterrows():
        seq = generate_sequence(lead, show, show_start, venue, sender)
        rec = {
            "Company": lead["Company"],
            "Domain": urlparse(lead["Website"] or "").netloc.replace("www.", ""),
            "Website": lead["Website"],
            "Show": show,
            "Show Start": show_start.isoformat() if show_start else "",
            "Booth": lead["Booth"],
            "Booth Size": lead["Booth Size"],
            "Sq Ft": lead["Sq Ft"],
            "Hall": lead.get("Hall", ""),
            "Industry": lead["Industry"],
            "Description": lead.get("Description", ""),
            "Revenue ($M)": lead["Revenue ($M)"],
            "Headcount": lead["Headcount"],
            "Score": lead["Score"],
            "Tier": lead["Tier"],
            "Est. Deal ($)": lead["Est. Deal ($)"],
            "Target Titles": lead["Target Titles"],
            "Contact": lead.get("Contact", ""),
            "Contact Title": lead.get("Contact Title", ""),
            "Contact Email": lead.get("Contact Email", ""),
            "Contact LinkedIn": lead.get("Contact LinkedIn", ""),
            "Data Source": lead.get("Enrichment", ""),
        }
        for step in seq:
            n = step["step"]
            rec[f"Send Date {n}"] = step["send_date"].isoformat()
            rec[f"Subject {n}"] = step["subject"]
            rec[f"Body {n}"] = step["body"]
        records.append(rec)
    return pd.DataFrame(records)


def range_note(value, lo, hi, unit: str, fmt: str = "{:,.0f}") -> str:
    v = fmt.format(value)
    if lo <= value <= hi:
        return f"{v}{unit} -- in range ({fmt.format(lo)}-{fmt.format(hi)}{unit})"
    if value < lo:
        return f"{v}{unit} -- below range (min {fmt.format(lo)}{unit})"
    return f"{v}{unit} -- above range (max {fmt.format(hi)}{unit})"


# =============================================================================
# 7. UI HELPERS
# =============================================================================


def _wide(fn) -> dict:
    """
    Version-safe 'fill the container' kwargs. Newer Streamlit uses width="stretch",
    older releases use use_container_width=True.
    """
    params = inspect.signature(fn).parameters
    if "width" in params:
        return {"width": "stretch"}
    if "use_container_width" in params:
        return {"use_container_width": True}
    return {}


def _supports(fn, kwarg: str) -> bool:
    return kwarg in inspect.signature(fn).parameters


def fmt_money(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.2f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:,.0f}"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def md(text: str) -> str:
    """Escape dollar signs so Streamlit's markdown doesn't treat '$15M-$100M' as LaTeX."""
    return text.replace("$", "\\$")


def inject_css() -> None:
    """Small amount of CSS. Colours are translucent so they work in light and dark themes."""
    st.markdown(
        """
        <style>
        .ax-header {
            background: linear-gradient(135deg, #0b1f3a 0%, #123c6e 55%, #1f6fb5 100%);
            color: #ffffff; padding: 1.4rem 1.6rem; border-radius: 12px; margin-bottom: 1rem;
        }
        .ax-header h1 { color:#fff; font-size: 1.7rem; margin: 0 0 .25rem 0; line-height: 1.2; }
        .ax-header p  { color: rgba(255,255,255,.82); margin: 0; font-size: .95rem; }
        .ax-badge {
            display:inline-block; padding: .18rem .6rem; border-radius: 999px;
            font-size: .75rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
            margin-right: .4rem;
        }
        .ax-live     { background: rgba(34,197,94,.18);  color: #16a34a; border: 1px solid rgba(34,197,94,.45); }
        .ax-neutral  { background: rgba(59,130,246,.15); color: #3b82f6; border: 1px solid rgba(59,130,246,.4); }
        .ax-muted { opacity: .7; font-size: .85rem; }
        .ax-legend { display:inline-block; width: 12px; height: 12px; border-radius: 3px;
                     background: rgba(34,197,94,.35); border: 1px solid rgba(34,197,94,.7); vertical-align: middle; }
        .ax-legend-big { background: rgba(245,158,11,.35); border-color: rgba(245,158,11,.7); }
        .ax-step { font-size: .8rem; opacity: .75; margin-bottom: .2rem; }
        div[data-testid="stMetric"] { padding: .2rem .4rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header() -> None:
    st.markdown(
        f"""
        <div class="ax-header">
            <h1>{COMPANY_NAME} &middot; {TAGLINE}</h1>
            <p>Any convention, any city: pull the exhibitor list, find the mid-market booths and the big fish
               in small booths worth a custom 3D LED build, and generate a timed outreach sequence for each one.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def init_state() -> None:
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("leads", None)
    st.session_state.setdefault("meta", None)


def reset_filters() -> None:
    for key, value in DEFAULTS.items():
        st.session_state[key] = value


def apply_preset(booth_range: tuple[int, int]) -> None:
    st.session_state["booth_range"] = booth_range


def render_sidebar() -> dict:
    with st.sidebar:
        st.markdown(f"### {COMPANY_NAME}")
        st.caption("Goldilocks filter -- tune the target profile live.")

        p1, p2 = st.columns(2)
        preset_names = list(PRESETS)
        p1.button("Mid-market", on_click=apply_preset, args=(PRESETS[preset_names[0]],), help=preset_names[0])
        p2.button("Islands", on_click=apply_preset, args=(PRESETS[preset_names[1]],), help=preset_names[1])

        st.slider(
            "Booth footprint (sq ft)", min_value=100, max_value=2500, step=50, key="booth_range",
            help="10x20 = 200, 20x20 = 400, 20x30 = 600. Default excludes 10x10s and islands.",
        )
        st.slider(
            "Annual revenue ($M)", min_value=0, max_value=500, step=5, key="revenue_range",
            help="Estimated annual revenue in millions of USD.",
        )
        st.slider("Employee count", min_value=10, max_value=2000, step=10, key="headcount_range")
        st.checkbox("Also qualify big fish (large company, small booth)", key="big_fish",
                    help="A company above the revenue floor below in any booth up to the Goldilocks ceiling. "
                         "They are at the show because they have to be; the flagship build comes later.")
        st.number_input("Big-fish revenue floor ($M)", min_value=10, max_value=5000, step=10, key="big_fish_min_revenue")
        st.checkbox("Show only qualified leads", key="only_goldilocks")
        st.button("Reset to defaults", on_click=reset_filters)

        st.divider()
        st.markdown("**Deal assumptions**")
        st.number_input(
            "Exhibit value per sq ft ($)", min_value=25, max_value=1000, step=25, key="price_per_sqft",
            help="Pipeline estimate = booth sq ft x $/sq ft for every qualified lead.",
        )
        website_cap = st.number_input(
            "Websites to look up for top leads", min_value=0, max_value=300, value=25, step=25,
            help="MapYourShow publishes each exhibitor's website on its detail page. The app fetches them for the "
                 "highest-scoring leads after a scrape (one request per company); use the button under the grid "
                 "to fetch more.",
        )

        st.divider()
        st.markdown("**Sender details** (for the email sequence)")
        sender = {
            "name": st.text_input("Your name", value="", placeholder="Jordan Lee"),
            "title": st.text_input("Title", value="Account Executive"),
            "phone": st.text_input("Phone / email (optional)", value=""),
        }

        st.divider()
        secret_key, secret_tavily = "", ""
        try:
            secret_key = st.secrets.get("APOLLO_API_KEY", "")  # .streamlit/secrets.toml or Cloud secrets
            secret_tavily = st.secrets.get("TAVILY_API_KEY", "")
        except Exception:
            pass
        with st.expander("Enrichment & search keys (optional)", expanded=False):
            st.caption(
                "Without a key, revenue and headcount are modelled. With an Apollo.io master API key the top "
                "leads (the number above) go through Apollo after each scrape. Free steps first: domain lookup "
                "and people search (name, title, LinkedIn, employer record). The 1-credit organisation "
                "enrichment runs only where revenue is still unknown, in-band booths first, up to the budget below."
            )
            apollo_key = st.text_input("Apollo API key", value=secret_key, type="password",
                                       help="Apollo -> Settings -> Integrations -> API -> Create new key "
                                            "(tick 'master API key'). On Streamlit Cloud, store it under "
                                            "App settings -> Secrets as APOLLO_API_KEY.")
            apollo_budget = st.number_input("Apollo credits per scrape", min_value=0, max_value=500, value=25, step=5,
                                            help=f"Free plan: {APOLLO_FREE_MONTHLY_CREDITS} credits a month, one per "
                                                 "organisation enrichment. 25 a scrape = three shows a month.")
            tavily_key = st.text_input("Tavily API key", value=secret_tavily, type="password",
                                       help="Web search: finds any convention's exhibitor directory by name and "
                                            "backs up the website lookup. Store it as TAVILY_API_KEY.")
            st.caption("Target titles, in order: " + ", ".join(TARGET_TITLES))
        loaded = [n for n, v in (("Apollo", secret_key), ("Tavily", secret_tavily)) if v]
        if loaded:
            st.caption(" and ".join(loaded) + " key" + ("s" if len(loaded) > 1 else "") + " loaded from secrets.")
        if APOLLO_CHARGED:
            st.caption(f"Apollo credits spent since the app last started: {len(APOLLO_CHARGED)} "
                       f"(Free plan: {APOLLO_FREE_MONTHLY_CREDITS}/month).")

    return {
        "booth_range": st.session_state["booth_range"],
        "revenue_range": st.session_state["revenue_range"],
        "headcount_range": st.session_state["headcount_range"],
        "only_goldilocks": st.session_state["only_goldilocks"],
        "price_per_sqft": st.session_state["price_per_sqft"],
        "big_fish": bool(st.session_state["big_fish"]),
        "big_fish_min_revenue": float(st.session_state["big_fish_min_revenue"]),
        "website_cap": int(website_cap),
        "sender": sender,
        "apollo_key": apollo_key.strip() or None,
        "apollo_budget": int(apollo_budget),
        "tavily_key": tavily_key.strip() or None,
    }


def render_show_calendar() -> None:
    cal = pd.DataFrame(TARGET_SHOWS)
    cal["Dates"] = [
        f"{datetime.fromisoformat(s['start']).strftime('%b %d')} - {datetime.fromisoformat(s['end']).strftime('%b %d, %Y')}"
        for s in TARGET_SHOWS
    ]
    today = date.today()
    cal["Outreach window"] = [outreach_timeline(date.fromisoformat(s["start"]), today)["label"] for s in TARGET_SHOWS]
    cal = cal[["name", "Dates", "venue", "industry", "potential", "Outreach window", "site"]].rename(
        columns={"name": "Show", "venue": "Venue", "industry": "Industry", "potential": "Build potential", "site": "Site"}
    )
    st.dataframe(
        cal,
        hide_index=True,
        column_config={"Site": st.column_config.LinkColumn("Site", display_text=r"https?://(?:www\.)?([^/]+)")},
        **_wide(st.dataframe),
    )
    st.caption(
        "Custom-build RFPs are sourced 5-7 months before the floor opens, so the window for these shows runs "
        "September 2026 through January 2027. Pick a show here, then paste its exhibitor list URL above."
    )


# =============================================================================
# 8. PIPELINE ORCHESTRATION
# =============================================================================


def build_meta(url: str, convention: str, platform: str, source: str, reason: str, chosen_show: str) -> dict:
    """Attach show date / venue from the target calendar when we can."""
    show = SHOW_BY_NAME.get(chosen_show) or match_target_show(convention)
    if show and chosen_show in SHOW_BY_NAME:
        convention = show["name"]
    return {
        "convention": convention,
        "platform": platform,
        "source": source,
        "url": url,
        "reason": reason,
        "show_date": date.fromisoformat(show["start"]) if show else None,
        "venue": show["venue"] if show else "",
    }


def run_pipeline(url: str, apollo_key: str | None, chosen_show: str, params: dict) -> None:
    """Scrape, enrich, score, fetch websites for the top leads, stash in session state."""
    with st.status("Working...", expanded=True) as status:
        st.write(f"Fetching directory: `{url}`")
        try:
            rows, live_meta = scrape_directory(url)
        except ScrapeError as exc:
            status.update(label="Scrape failed", state="error", expanded=True)
            st.error(
                f"Could not read that directory: {exc}\n\n"
                "Fully supported: MapYourShow directories (CES, NAB Show, World of Concrete, PACK EXPO, AHR, TISE "
                "and most large Las Vegas shows). For other platforms, paste a page that lists exhibitors in a "
                "plain HTML table or card grid."
            )
            return
        except Exception as exc:  # noqa: BLE001 - surface the real reason, never fake data
            status.update(label="Scrape failed", state="error", expanded=True)
            st.error(f"Unexpected error while scraping: {exc.__class__.__name__}: {exc}")
            return

        meta = build_meta(url, live_meta["convention"], live_meta["platform"], "live", "", chosen_show)
        meta.update({k: live_meta.get(k, 0) for k in ("halls", "hall_errors", "sized")})
        if meta["platform"] == "MapYourShow":
            st.write(f"Exhibitor gallery: **{len(rows)}** companies. Floor plan: **{meta['sized']}** with exact booth "
                     f"footprints across {meta['halls']} halls"
                     + (f" ({meta['hall_errors']} halls could not be read)" if meta["hall_errors"] else "") + ".")
            if not meta["sized"] and live_meta.get("diag"):
                st.write(f"Floor-plan diagnostics (show id `{live_meta.get('showid')}`, app version `{live_meta.get('fpver')}`):")
                st.code("\n".join(str(d) for d in live_meta["diag"]), language="text")
        else:
            st.write(f"Parsed **{len(rows)}** exhibitors from {meta['platform']} "
                     f"({meta['sized']} with booth dimensions on the page).")

        # ---- Enrichment ----------------------------------------------------
        st.write("Modelling revenue and headcount for every exhibitor (baseline)...")
        bar = st.progress(0.0, text="Starting enrichment")

        def on_progress(done: int, total: int, company: str) -> None:
            bar.progress(done / total, text=f"{done}/{total}  {company}")

        leads = enrich_companies(rows, progress=on_progress)
        bar.progress(1.0, text=f"Modelled {len(leads)} companies")

        # ---- Websites for the top leads (MapYourShow detail pages) --------
        cap = int(params.get("website_cap") or 0)
        if cap and "Detail URL" in leads.columns and leads["Detail URL"].astype(bool).any():
            scored = score_leads(leads, params)
            top = scored[(scored["Website"] == "") & (scored["Detail URL"] != "")].head(cap)
            if len(top):
                st.write(f"Looking up websites for the top {len(top)} leads...")
                sites = fetch_websites(list(top["Detail URL"]))
                site_map = dict(zip(top["Company"], sites))
                leads["Website"] = [site_map.get(c, w) or w for c, w in zip(leads["Company"], leads["Website"])]
                st.write(f"Found {sum(1 for s in sites if s)} websites.")

        # ---- Live Apollo pass for the top leads ----------------------------
        if apollo_key and cap:
            top = score_leads(leads, params).head(cap)
            st.write(f"Apollo: domain lookup, organisation enrichment and people search for the top {len(top)} leads...")
            abar = st.progress(0.0, text="Apollo")
            stats = apply_apollo(leads, apollo_key, list(top["Company"]),
                                 progress=lambda d, t, c: abar.progress(d / t, text=f"{d}/{t}  {c}"),
                                 tavily_key=params.get("tavily_key"), budget=params.get("apollo_budget"), params=params)
            abar.progress(1.0, text="Apollo pass complete")
            st.write(f"Apollo matched {stats['orgs']} of {stats['tried']} companies "
                     f"({stats['domains']} domains found by name, {stats['contacts']} decision-makers found). "
                     f"Credits spent: {stats['credits']}"
                     + (f"; {stats['skipped_budget']} enrichments held back by the per-scrape budget." if stats["skipped_budget"] else "."))
            if stats["tried"] and not stats["orgs"] and not stats["contacts"]:
                st.warning("Apollo returned nothing for those domains. Check that the key is a master API key, "
                           "that the plan still has credits, and that the key was saved as APOLLO_API_KEY.")
            elif not stats["tried"]:
                st.warning("No domains could be found for the top leads, so Apollo had nothing to look up.")

        meta["scraped_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
        st.session_state["leads"] = leads
        st.session_state["meta"] = meta
        status.update(label=f"Done: {len(leads)} exhibitors from {meta['convention']} ({meta['platform']})",
                      state="complete", expanded=False)


def fetch_more_websites(cap: int, params: dict) -> None:
    """Button handler: websites (and, with a key, Apollo data) for the next batch of top leads."""
    leads = st.session_state.get("leads")
    if leads is None or "Detail URL" not in leads.columns:
        return
    scored = score_leads(leads, params)
    if params.get("apollo_key"):
        top = scored[scored["Enrichment"] != "Apollo"].head(cap)
    else:
        top = scored[(scored["Website"] == "") & (scored["Detail URL"] != "")].head(cap)
    if not len(top):
        st.toast("Every ranked lead already has a website.")
        return
    with st.spinner(f"Looking up websites for {len(top)} leads..."):
        need_site = top[(top["Website"] == "") & (top["Detail URL"] != "")]
        if len(need_site):
            sites = fetch_websites(list(need_site["Detail URL"]))
            site_map = dict(zip(need_site["Company"], sites))
            leads["Website"] = [site_map.get(c, w) or w for c, w in zip(leads["Company"], leads["Website"])]
    if params.get("apollo_key"):
        with st.spinner(f"Apollo: enriching {len(top)} leads..."):
            apply_apollo(leads, params["apollo_key"], list(top["Company"]), tavily_key=params.get("tavily_key"),
                         budget=params.get("apollo_budget"), params=params)
    st.session_state["leads"] = leads


# =============================================================================
# 9. MAIN
# =============================================================================


def render_empty_state() -> None:
    with st.container(border=True):
        st.markdown("**How to use this**")
        st.markdown(
            """
            1. Pick a target show (NAB Show 2027 fills its directory in automatically), paste any exhibitor-list
               URL, or type a show name into the finder above to locate its directory anywhere in the country.
               MapYourShow directories (`https://<show>.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm`)
               return the full exhibitor list plus exact booth footprints from the floor plan.
            2. Click **Scrape & Analyze**. Every row is a real exhibitor; revenue and headcount are modelled until
               an Apollo key is added in the sidebar.
            3. Two ways to qualify: Goldilocks (mid-market booth, revenue and headcount all in range) and big fish
               (a large company in a small booth -- the flagship build comes later). Click a row and the 4-touch
               sequence is ready to send.
            """
        )
    st.markdown("#### Target shows on the calendar: Las Vegas, March - June 2027")
    render_show_calendar()
    with st.expander("Other directories already live on MapYourShow"):
        st.markdown("\n".join(f"- {name}: `{link}`" for name, link in KNOWN_DIRECTORIES))


def render_directory_finder(tavily_key: str | None) -> None:
    """Show name -> exhibitor directory URL, for any convention in the country (Tavily web search)."""
    with st.expander("Find a show's exhibitor directory by name (any city)", expanded=False):
        if not tavily_key:
            st.caption("Add a Tavily API key in the sidebar (or TAVILY_API_KEY in secrets) to search for "
                       "directories by show name. Until then, paste the directory URL below.")
            return
        c1, c2 = st.columns([4, 1.2])
        query = c1.text_input("Show name", placeholder="IMTS 2026 Chicago", label_visibility="collapsed")
        c2.markdown("<div style='height:.1rem'></div>", unsafe_allow_html=True)
        if c2.button("Find directory", **_wide(st.button)) and query.strip():
            with st.spinner("Searching..."):
                st.session_state["dir_candidates"] = find_directories_tavily(query.strip(), tavily_key)
                st.session_state["dir_query"] = query.strip()
        candidates = st.session_state.get("dir_candidates") or []
        if st.session_state.get("dir_query") and not candidates:
            st.info("No exhibitor directory found for that name. Try the show's full name and year, or paste its URL below.")
        if candidates:
            labels = [f"{c['platform']}  |  {c['title'][:70]}  |  {c['host']}" for c in candidates]
            pick = st.radio("Directories found (MapYourShow first: exact booth sizes)", labels, index=0)
            chosen = candidates[labels.index(pick)]
            st.code(chosen["url"], language="text")
            if st.button("Use this directory"):
                st.session_state["url_prefill"] = chosen["url"]
                st.rerun()


def main() -> None:
    st.set_page_config(page_title=f"{COMPANY_NAME} | Lead Intelligence", layout="wide", initial_sidebar_state="expanded")
    init_state()
    inject_css()
    render_header()
    params = render_sidebar()

    # ---- Module 1: URL input -------------------------------------------------
    render_directory_finder(params.get("tavily_key"))
    with st.form("scrape_form", clear_on_submit=False):
        c_url, c_show = st.columns([4, 2])
        url = c_url.text_input(
            "Enter Trade Show Directory URL (e.g., MapYourShow / A2Z Events)",
            value=st.session_state.get("url_prefill", ""),
            placeholder="https://nab27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm?featured=false",
            help="Leave blank after picking a target show that has a published directory (NAB Show 2027) "
                 "and the app uses that directory.",
        )
        chosen_show = c_show.selectbox(
            "Target show", ["Auto-detect from URL"] + [s["name"] for s in TARGET_SHOWS], index=0,
            help="Sets the show name, date and venue used in the emails and the outreach timeline.",
        )
        b1, _ = st.columns([1.6, 5])
        scrape_clicked = b1.form_submit_button("Scrape & Analyze", type="primary", **_wide(st.form_submit_button))

    url = (url or "").strip()
    if not url and chosen_show in SHOW_BY_NAME and SHOW_BY_NAME[chosen_show].get("directory"):
        url = SHOW_BY_NAME[chosen_show]["directory"]
    if scrape_clicked:
        if not url:
            st.warning("Paste an exhibitor directory URL, or pick NAB Show 2027 (its 2027 directory is already live).")
        else:
            if not url.startswith("http"):
                url = "https://" + url
            run_pipeline(url, params["apollo_key"], chosen_show, params)

    leads: pd.DataFrame | None = st.session_state.get("leads")
    meta: dict | None = st.session_state.get("meta")
    if leads is None or meta is None:
        render_empty_state()
        return
    for col in ("Hall", "Description", "Detail URL", "Website", "Enrichment", *CONTACT_COLUMNS):
        if col not in leads.columns:  # session data from an older build of the app
            leads[col] = ""

    with st.expander("Target shows: Las Vegas, March - June 2027", expanded=False):
        render_show_calendar()

    # ---- Result header: source badge, convention name, venue, date, export --
    h1, h2, h3, h4, h5 = st.columns([2.3, 1.7, 1.4, 1.3, 1.0])
    with h1:
        badge = ("<span class='ax-badge ax-live'>Live scrape</span>"
                 f"<span class='ax-badge ax-neutral'>{meta['platform']}</span>")
        st.markdown(badge, unsafe_allow_html=True)
        sized = int(meta.get("sized") or 0)
        note = f"{len(leads)} exhibitors &middot; {sized} with exact booth footprints"
        if meta["platform"] == "MapYourShow":
            note += f" from {meta.get('halls', 0)} halls"
        note += f" &middot; {meta['scraped_at']}"
        st.markdown(f"<div class='ax-muted'>{note}</div>", unsafe_allow_html=True)
    state_key = slug(f"{meta['url']}_{meta['convention']}")
    with h2:
        convention = st.text_input("Convention name (used in emails)", value=meta["convention"], key=f"conv_{state_key}").strip() or meta["convention"]
    with h3:
        default_venue = meta.get("venue") or SHOW_BY_NAME.get(convention, {}).get("venue", "")
        venue = st.text_input("Venue / city", value=default_venue, key=f"venue_{state_key}",
                              placeholder="McCormick Place, Chicago",
                              help="Las Vegas venues get the home-turf pitch; any other city gets the build-that-travels pitch.").strip()
    with h4:
        show_date = st.date_input("Show start date", value=meta.get("show_date"), key=f"date_{state_key}",
                                  help="Drives the outreach window and the send dates in the sequence.")
        if isinstance(show_date, (list, tuple)):
            show_date = show_date[0] if show_date else None
    timeline = outreach_timeline(show_date)

    # ---- Module 2: scoring ---------------------------------------------------
    scored = score_leads(leads, params)
    qualified = scored[scored["Qualified"]]
    n_gold = int(scored["Goldilocks"].sum())
    n_big = int(scored["Big Fish"].sum())
    pipeline_value = int(qualified["Est. Deal ($)"].sum())

    with h5:
        st.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
        st.download_button(
            "Export leads CSV",
            data=scored.drop(columns=["Booth Pts", "Revenue Pts", "Headcount Pts"]).to_csv(index=False).encode("utf-8"),
            file_name=f"{slug(convention)}_leads.csv", mime="text/csv", **_wide(st.download_button),
        )

    # ---- Module 3: KPIs ------------------------------------------------------
    k1, k2, k3, k4 = st.columns(4)
    with k1, st.container(border=True):
        st.metric("Total companies scraped", f"{len(scored):,}")
    with k2, st.container(border=True):
        st.metric("Qualified leads", f"{len(qualified):,}",
                  delta=(f"{n_gold} Goldilocks + {n_big} big fish" if params.get("big_fish")
                         else f"{len(qualified) / len(scored):.0%} of list") if len(scored) else None,
                  delta_color="off",
                  help="Goldilocks: booth, revenue and headcount all in range. Big fish: above the revenue floor "
                       "in a booth no bigger than the Goldilocks ceiling (the flagship build comes later).")
    with k3, st.container(border=True):
        st.metric("Estimated pipeline value", fmt_money(pipeline_value),
                  help=md(f"Sum of booth sq ft x ${params['price_per_sqft']}/sq ft across qualified leads; "
                          f"big fish are valued at the mid-market floor ({params['booth_range'][0]} sq ft)."))
    with k4, st.container(border=True):
        if timeline["days_out"] is not None:
            st.metric("Outreach window", timeline["label"], delta=f"{timeline['days_out']} days to {convention}", delta_color="off",
                      help=f"Prime window: {WINDOW_CLOSE_MONTHS}-{WINDOW_OPEN_MONTHS} months before the show floor opens "
                           f"({timeline['window_open'].strftime('%b %d')} - {timeline['window_close'].strftime('%b %d, %Y')}).")
        else:
            st.metric("Outreach window", "Set show date", help="Pick a target show or enter the show start date above.")

    # ---- Module 3: data grid -------------------------------------------------
    st.markdown("#### Lead grid")
    f1, f2 = st.columns([3, 1.5])
    search = f1.text_input("Search company, industry or contact", value="", placeholder="e.g. audio, streaming, Lumen")
    tier_options = ["Goldilocks", "Big fish", "Near miss", "Out of range", "No booth data"]
    tiers = f2.multiselect("Tier", tier_options, default=tier_options[:3])

    view = scored.copy()
    if params["only_goldilocks"]:
        view = view[view["Qualified"]]
    if tiers:
        view = view[view["Tier"].isin(tiers)]
    if search:
        mask = (
            view["Company"].str.contains(search, case=False, na=False)
            | view["Industry"].str.contains(search, case=False, na=False)
            | view["Hall"].astype(str).str.contains(search, case=False, na=False)
            | view["Contact"].astype(str).str.contains(search, case=False, na=False)
        )
        view = view[mask]
    view = view.reset_index(drop=True)

    near = int((scored["Tier"] == "Near miss").sum())
    nodata = int((scored["Tier"] == "No booth data").sum())
    st.markdown(
        f"<span class='ax-legend'></span> <span class='ax-muted'>Green rows meet all three Goldilocks criteria; </span>"
        f"<span class='ax-legend ax-legend-big'></span> <span class='ax-muted'>amber rows are big fish "
        f"({near} near-miss leads also worth a call; {nodata} exhibitors have no booth on the floor plan yet). "
        f"Click a row to open the outreach sequence.</span>",
        unsafe_allow_html=True,
    )

    has_contacts = bool(view["Contact"].astype(str).str.len().gt(0).any())
    display_cols = ["Company", "Booth", "Booth Size", "Sq Ft", "Hall", "Revenue ($M)", "Headcount", "Industry",
                    "Score", "Tier"] + (["Contact", "Contact Title"] if has_contacts else []) + ["Est. Deal ($)", "Website"]
    grid_df = view[display_cols].copy()
    grid_df["Sq Ft"] = grid_df["Sq Ft"].where(grid_df["Sq Ft"] > 0)  # blank, not zero, when unknown
    grid_df["Est. Deal ($)"] = grid_df["Est. Deal ($)"].where(grid_df["Est. Deal ($)"] > 0)

    def highlight(row: pd.Series) -> list[str]:
        if bool(view.loc[row.name, "Goldilocks"]):
            return ["background-color: rgba(34,197,94,0.20)"] * len(row)
        if bool(view.loc[row.name, "Big Fish"]):
            return ["background-color: rgba(245,158,11,0.18)"] * len(row)
        return [""] * len(row)

    styler = grid_df.style.apply(highlight, axis=1).format({"Revenue ($M)": "{:,.1f}", "Est. Deal ($)": "${:,.0f}"}, na_rep="")

    column_config = {
        "Company": st.column_config.TextColumn("Company", width="medium"),
        "Booth": st.column_config.TextColumn("Booth #", width="small"),
        "Booth Size": st.column_config.TextColumn("Size (ft)", width="small", help="Largest booth; (+n) = additional booths."),
        "Sq Ft": st.column_config.NumberColumn("Sq Ft", width="small", format="%d", help="Total footprint from the floor plan."),
        "Hall": st.column_config.TextColumn("Hall", width="medium"),
        "Revenue ($M)": st.column_config.NumberColumn("Revenue ($M)", width="small"),
        "Headcount": st.column_config.NumberColumn("Headcount", width="small", format="%d"),
        "Industry": st.column_config.TextColumn("Industry", width="medium"),
        "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d", width="small"),
        "Tier": st.column_config.TextColumn("Tier", width="small"),
        "Contact": st.column_config.TextColumn("Contact", width="small", help="Filled by Apollo People Search when a key is set."),
        "Contact Title": st.column_config.TextColumn("Title", width="medium"),
        "Est. Deal ($)": st.column_config.NumberColumn("Est. deal", width="small"),
        "Website": st.column_config.LinkColumn("Website", display_text=r"https?://(?:www\.)?([^/]+)", width="medium"),
    }

    grid_kwargs = dict(column_config=column_config, hide_index=True,
                       height=min(60 + 35 * max(len(grid_df), 1), 520), **_wide(st.dataframe))
    selected_rows: list[int] = []
    if _supports(st.dataframe, "on_select"):
        event = st.dataframe(styler, on_select="rerun", selection_mode="single-row", key="lead_grid", **grid_kwargs)
        try:
            selected_rows = list(event.selection.rows)
        except Exception:
            selected_rows = []
    else:  # very old Streamlit: no row selection, fall back to the selectbox only
        st.dataframe(styler, **grid_kwargs)

    if params.get("apollo_key"):
        missing = int(((scored["Enrichment"] != "Apollo") & scored["Qualified"]).sum())
        verb, note = "Run Apollo on next", "qualified leads have not been through Apollo yet (1-2 credits each)."
    else:
        missing = int(((scored["Website"] == "") & (scored["Detail URL"] != "") & scored["Qualified"]).sum()) if "Detail URL" in scored.columns else 0
        verb, note = "Look up websites for next", "qualified leads still need a website (one detail-page request each)."
    if missing:
        w1, w2 = st.columns([1.8, 5])
        if w1.button(f"{verb} {min(params['website_cap'] or 25, missing)} leads", **_wide(st.button)):
            fetch_more_websites(params["website_cap"] or 25, params)
            st.rerun()
        w2.caption(f"{missing} {note}")

    if view.empty:
        st.info("No leads match the current filters. Widen the Goldilocks ranges in the sidebar.")
        return

    # ---- Module 4: outreach system ------------------------------------------
    st.markdown("#### Outreach sequence")
    options = list(view["Company"])
    default_idx = selected_rows[0] if selected_rows and selected_rows[0] < len(options) else 0
    o1, o2 = st.columns([3, 1.6])
    company = o1.selectbox("Selected company (click a row above or pick here)", options, index=default_idx)
    with o2:
        st.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
        export_pool = qualified if len(qualified) else scored
        st.download_button(
            f"Export campaign CSV ({len(export_pool)} leads)",
            data=build_campaign_export(export_pool, convention, show_date, venue, params["sender"]).to_csv(index=False).encode("utf-8"),
            file_name=f"{slug(convention)}_campaign.csv", mime="text/csv",
            help="One row per Goldilocks lead with all four touches, send dates and target titles. "
                 "Import into Apollo Sequences, Instantly, Lemlist or Clay.",
            **_wide(st.download_button),
        )

    lead = view[view["Company"] == company].iloc[0]
    sequence = generate_sequence(lead, convention, show_date, venue, params["sender"])

    with st.expander(f"{company}  |  Booth {lead['Booth']}  |  {lead['Booth Size']} at {convention}", expanded=True):
        left, right = st.columns([1.05, 2])
        with left:
            st.markdown(f"**Why this lead** -- score {int(lead['Score'])}/100, tier: {lead['Tier']}")
            b_lo, b_hi = params["booth_range"]
            r_lo, r_hi = params["revenue_range"]
            h_lo, h_hi = params["headcount_range"]
            booth_line = (f"{range_note(lead['Sq Ft'], b_lo, b_hi, ' sq ft')} ({lead['Booth Size']})"
                          if lead["Sq Ft"] else "not on the floor plan yet (no booth data)")
            hall_line = f"\n- Hall: {lead['Hall']}" if str(lead.get("Hall") or "") else ""
            if lead["Tier"] == "Big fish":
                st.markdown(md(f"Big fish: ${lead['Revenue ($M)']:,.0f}M of revenue in a {lead['Booth Size']}. "
                               f"They are here because they have to be; the flagship build is the pitch."))
            st.markdown(md(
                f"- Booth: {booth_line}{hall_line}\n"
                f"- Revenue: {range_note(lead['Revenue ($M)'], r_lo, r_hi, 'M', '${:,.1f}')}\n"
                f"- Headcount: {range_note(lead['Headcount'], h_lo, h_hi, ' employees')}\n"
                f"- Industry: {lead['Industry']}\n"
                f"- Est. deal: {fmt_money(lead['Est. Deal ($)'])} "
                f"({int(lead['Est. Deal ($)']) // max(int(params['price_per_sqft']), 1)} sq ft x ${params['price_per_sqft']}/sq ft"
                f"{', mid-market floor' if lead['Tier'] == 'Big fish' else ''})\n"
                f"- Data: booth size from {lead['Size Source']}, revenue/headcount {lead['Enrichment']}"
            ))
            if str(lead.get("Description") or ""):
                st.caption(str(lead["Description"])[:260] + ("..." if len(str(lead["Description"])) > 260 else ""))
            if str(lead.get("Detail URL") or ""):
                st.markdown(f"[Directory listing]({lead['Detail URL']})")
            st.markdown("**Who to reach**")
            if lead.get("Contact"):
                lines = [f"- {lead['Contact']}, {lead['Contact Title']}"]
                if lead.get("Contact Email"):
                    lines.append(f"- {lead['Contact Email']}")
                if lead.get("Contact LinkedIn"):
                    lines.append(f"- [LinkedIn profile]({lead['Contact LinkedIn']})")
                st.markdown("\n".join(lines))
            else:
                st.markdown("- No contact yet. Add an Apollo key in the sidebar to run People Search, or look up the titles below on LinkedIn.")
            st.markdown(f"- Target titles: {lead['Target Titles']}")
            if lead["Website"]:
                st.markdown(f"- {lead['Website']}")

            st.markdown("**Timing**")
            if timeline["days_out"] is not None:
                st.markdown(
                    f"- {convention}: {show_date.strftime('%b %d, %Y')} ({timeline['days_out']} days out)\n"
                    f"- RFP window: {timeline['window_open'].strftime('%b %d')} - {timeline['window_close'].strftime('%b %d, %Y')} "
                    f"-- {timeline['label']}\n"
                    f"- First touch: {sequence[0]['send_date'].strftime('%b %d, %Y')}"
                )
            else:
                st.markdown("- Set the show start date above to get send dates.")

        with right:
            tabs = st.tabs([f"Touch {s['step']}" for s in sequence])
            for tab, step in zip(tabs, sequence):
                with tab:
                    st.markdown(f"<div class='ax-step'>{step['label']} &middot; send {step['send_date'].strftime('%a %b %d, %Y')}</div>",
                                unsafe_allow_html=True)
                    key_base = slug(f"{company}_{convention}_{step['step']}")
                    subject_edit = st.text_input("Subject", value=step["subject"], key=f"subj_{key_base}")
                    body_edit = st.text_area("Body", value=step["body"], height=330, key=f"body_{key_base}")
                    st.download_button(
                        "Download this email (.txt)",
                        data=f"Send: {step['send_date'].isoformat()}\nSubject: {subject_edit}\n\n{body_edit}".encode("utf-8"),
                        file_name=f"{slug(company)}_touch{step['step']}.txt", mime="text/plain", key=f"dl_{key_base}",
                    )
            full = "\n\n" + ("=" * 70 + "\n\n").join(
                f"TOUCH {s['step']}  |  send {s['send_date'].strftime('%b %d, %Y')}\nSubject: {s['subject']}\n\n{s['body']}" for s in sequence
            )
            st.download_button(
                "Download full 4-touch sequence (.txt)",
                data=full.strip().encode("utf-8"), file_name=f"{slug(company)}_sequence.txt", mime="text/plain",
                key=f"dl_full_{slug(company)}",
            )


if __name__ == "__main__":
    main()
