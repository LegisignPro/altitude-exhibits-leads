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
    URL  ->  scrape (MapYourShow / A2Z Events / generic HTML)
         ->  fallback sample dataset if the site blocks us or renders via JS
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
import random
import re
import time
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
}
PRESETS = {
    "Mid-market (200-600 sq ft)": (200, 600),
    "Islands (400+ sq ft)": (400, 2500),
}

# How the 100 points are split. Booth size is the strongest signal we can
# actually observe on the show floor, so it carries the most weight.
SCORE_WEIGHTS = {"booth": 40, "revenue": 30, "headcount": 30}
NEAR_MISS_THRESHOLD = 65  # misses one criterion but still worth a call

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
     "industry": "Tech & Comms", "potential": "High", "site": "https://www.iwceexpo.com"},
    {"name": "Shoptalk 2027", "start": "2027-03-22", "end": "2027-03-24", "venue": "Mandalay Bay",
     "industry": "Retail Tech & E-commerce", "potential": "Very High", "site": "https://shoptalk.com"},
    {"name": "Bar & Restaurant Expo 2027", "start": "2027-03-22", "end": "2027-03-24", "venue": "Las Vegas Convention Center",
     "industry": "Food & Beverage", "potential": "Medium", "site": "https://www.barandrestaurantexpo.com"},
    {"name": "Indoor Ag-Con 2027", "start": "2027-03-24", "end": "2027-03-25", "venue": "Las Vegas Convention Center",
     "industry": "Agriculture Tech", "potential": "Medium", "site": "https://indoor.ag"},
    {"name": "NAB Show 2027", "start": "2027-04-04", "end": "2027-04-07", "venue": "Las Vegas Convention Center",
     "industry": "Broadcast, Media & Tech", "potential": "Massive", "site": "https://nabshow.com"},
    {"name": "Pizza Expo 2027", "start": "2027-04-13", "end": "2027-04-15", "venue": "Las Vegas Convention Center",
     "industry": "Food & Beverage", "potential": "Medium", "site": "https://www.pizzaexpo.com"},
    {"name": "WasteExpo 2027", "start": "2027-05-03", "end": "2027-05-06", "venue": "Las Vegas Convention Center",
     "industry": "Industrial & Heavy Machinery", "potential": "High", "site": "https://www.wasteexpo.com"},
    {"name": "HD Expo 2027", "start": "2027-05-04", "end": "2027-05-05", "venue": "Mandalay Bay",
     "industry": "Commercial Design", "potential": "Very High", "site": "https://www.hdexpo.com"},
    {"name": "HR in Hospitality 2027", "start": "2027-06-09", "end": "2027-06-10", "venue": "Las Vegas",
     "industry": "HR & Hospitality Tech", "potential": "Medium", "site": "https://www.hrinhospitality.com"},
]
SHOW_BY_NAME = {s["name"]: s for s in TARGET_SHOWS}

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

# Fictional contact names attached to the sample dataset only. Live scrapes
# never invent people -- they get contacts from Apollo or stay blank.
SAMPLE_CONTACT_NAMES = [
    "Dana Whitfield", "Marcus Bell", "Priya Raman", "Tom Okafor", "Elena Marsh", "Victor Huang",
    "Sofia Delgado", "Owen Kaplan", "Nadia Petrova", "Luis Herrera", "Grace Lindqvist", "Isaac Moreau",
    "Hannah Osei", "Ravi Menon", "Claire Dubois", "Jamal Carter", "Mei Tanaka", "Ben Sorensen",
    "Aisha Rahman", "Noah Fitzgerald",
]

# =============================================================================
# 1. FALLBACK SCRAPE DATASET
# =============================================================================
# Twenty realistic (fictional) exhibitors in the shape a directory scrape
# produces: company, website, booth number, booth dimensions. Broadcast/media
# flavoured to match NAB Show, the biggest show in the target window. The
# booth mix is deliberately varied so the Goldilocks filter has something to
# reject: a handful of 10x10s, a couple of islands, and a core of 10x20 /
# 20x20 / 20x30 spaces.
FALLBACK_EXHIBITORS = [
    ("Lumen Audio Labs", "https://www.lumenaudiolabs.com", "C5831", "20x20"),
    ("Vantage Robotics Systems", "https://www.vantagerobotics.io", "C8437", "10x20"),
    ("Kestrel Aerial Cinema", "https://www.kestrelaerial.com", "N1210", "20x30"),
    ("Northbridge Signal Systems", "https://www.northbridgesignal.com", "W2118", "10x10"),
    ("Helios Studio Lighting", "https://www.heliosstudiolighting.com", "C9033", "10x30"),
    ("Orbit Wireless Video", "https://www.orbitwirelessvideo.com", "C6502", "20x20"),
    ("Clearwave Networking", "https://www.clearwavenet.com", "W1419", "30x30"),
    ("Summit Streaming Platforms", "https://www.summitstreaming.com", "W4701", "20x30"),
    ("Aurora Display Technologies", "https://www.auroradisplays.com", "C7114", "50x50"),
    ("Pinnacle Newsroom Software", "https://www.pinnaclenewsroom.com", "W3309", "10x20"),
    ("Redrock Podcast Gear", "https://www.redrockpodcast.com", "N5720", "20x20"),
    ("Tidewater Satellite Uplink", "https://www.tidewateruplink.com", "N6118", "10x10"),
    ("Beacon Intercom Systems", "https://www.beaconintercom.com", "C10240", "10x20"),
    ("Stratos Media AI", "https://www.stratosmedia.ai", "W3316", "40x40"),
    ("Copperline Audio", "https://www.copperlineaudio.com", "C5605", "10x10"),
    ("Evergreen Battery Co.", "https://www.evergreenbattery.com", "C9407", "20x20"),
    ("Nimbus Cloud Cameras", "https://www.nimbuscams.com", "N4022", "10x20"),
    ("Ironwood Rugged Computing", "https://www.ironwoodrugged.com", "W2811", "20x30"),
    ("Skyline Virtual Production", "https://www.skylinevp.com", "C8830", "20x20"),
    ("Meridian Captioning", "https://www.meridiancaptioning.com", "W5107", "10x30"),
]
FALLBACK_CONVENTION = "NAB Show 2027"


def load_fallback_dataset() -> list[dict]:
    """Return the bundled sample in exactly the shape `parse_exhibitors` returns."""
    rows = []
    for name, site, booth, dims in FALLBACK_EXHIBITORS:
        rows.append(
            {
                "Company": name,
                "Website": site,
                "Booth": booth,
                "Booth Size": dims,
                "Sq Ft": booth_dims_to_sqft(dims),
                "Size Source": "sample",
            }
        )
    return rows


# =============================================================================
# 2. BOOTH-SIZE HELPERS
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


def estimate_booth_size(company: str, booth_no: str) -> tuple[str, int]:
    """
    Deterministic estimate used when the directory does not publish dimensions
    (most public exhibitor lists show the booth number only).

    PRODUCTION NOTE: both MapYourShow and A2Z expose booth geometry through
    their floor-plan endpoints (MYS: /floorplan/ JSON, A2Z: eBooth.aspx and the
    booth-detail API). Pull the polygon width/depth from there and this
    estimate becomes unnecessary. The distribution below mirrors a typical
    Vegas show floor: mostly 10x10s, a healthy middle, a few islands.
    """
    seed = int(hashlib.md5(f"{company}|{booth_no}".lower().encode()).hexdigest(), 16)
    rng = random.Random(seed)
    sizes = ["10x10", "10x20", "20x20", "10x30", "20x30", "30x30", "40x40", "50x50"]
    weights = [42, 20, 15, 6, 9, 4, 3, 1]
    dims = rng.choices(sizes, weights=weights, k=1)[0]
    return dims, booth_dims_to_sqft(dims)


# =============================================================================
# 3. LIVE URL SCRAPER
# =============================================================================


class ScrapeError(Exception):
    """Raised for any condition that should trigger the fallback dataset."""


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
            dims, sqft = estimate_booth_size(row["Company"], row["Booth"])
            row["Booth Size"], row["Sq Ft"], row["Size Source"] = dims, sqft, "estimated"
        rows.append(row)

    if len(rows) < MIN_ROWS_FOR_VALID_SCRAPE:
        raise ScrapeError(
            "No exhibitor rows found in the HTML. The directory is most likely "
            "rendered client-side (JavaScript) or behind a bot challenge."
        )
    return rows


@st.cache_data(ttl=3600, show_spinner=False)
def scrape_directory(url: str) -> tuple[list[dict], dict]:
    """
    Fetch + parse a directory URL. Returns (rows, meta). Raises ScrapeError on
    anything that should trigger the fallback. Cached for an hour so tweaking
    the sidebar never re-hits the show's servers.
    """
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    rows = parse_exhibitors(html, url)
    meta = {
        "convention": infer_convention_name(url, soup),
        "platform": detect_platform(url),
        "source": "live",
        "url": url,
    }
    return rows, meta


# =============================================================================
# 4. ENRICHMENT ENGINE  (simulated Apollo / Clearbit + real Apollo slot-in)
# =============================================================================


def infer_industry(company: str, rng: random.Random) -> str:
    name = company.lower()
    for pattern, industry in INDUSTRY_PATTERNS:
        if re.search(pattern, name):
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


def simulate_enrichment(company: str, website: str, sqft: int) -> dict:
    """
    Stand-in for an Apollo / Clearbit organisation-enrichment call.

    Deterministic: the same company always gets the same numbers, so the demo
    is stable across reruns and the sidebar filters behave predictably. Booth
    footprint is used as a prior because it correlates with company scale on
    a real show floor (a 50x50 island is not a 12-person startup).
    """
    seed = int(hashlib.md5(f"{company}|{website}".lower().encode()).hexdigest(), 16) % (2**32)
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
        "Industry": infer_industry(company, rng),
        "Enrichment": "simulated",
    }


def simulate_contact(company: str, website: str, headcount: int, index: int) -> dict:
    """Fictional decision-maker for SAMPLE rows only (the companies are fictional too)."""
    name = SAMPLE_CONTACT_NAMES[index % len(SAMPLE_CONTACT_NAMES)]
    title = recommend_titles(headcount)[0]
    domain = urlparse(website).netloc.replace("www.", "") or "example.com"
    first, last = name.lower().split(" ", 1)
    return {"Contact": name, "Contact Title": title, "Contact Email": f"{first}.{last}@{domain}"}


@st.cache_data(ttl=86400, show_spinner=False)
def enrich_company_apollo(domain: str, api_key: str) -> dict | None:
    """
    PRODUCTION PATH -- real Apollo.io organisation enrichment.

    How to wire it up:
      1. Get a key at https://app.apollo.io/#/settings/integrations/api
      2. Put it in .streamlit/secrets.toml:
             APOLLO_API_KEY = "xxxxxxxx"
         (or paste it in the sidebar for a one-off session)
      3. That's it -- enrich_companies() calls this function automatically
         whenever a key is present and a website/domain was scraped.

    Endpoint: GET https://api.apollo.io/api/v1/organizations/enrich?domain=acme.com
    Header:   x-api-key: <key>
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
    Results are cached for 24h so re-runs don't burn credits.
    """
    if not domain or not api_key:
        return None
    try:
        resp = requests.get(
            "https://api.apollo.io/api/v1/organizations/enrich",
            params={"domain": domain},
            headers={"x-api-key": api_key, "Cache-Control": "no-cache", "Content-Type": "application/json"},
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


@st.cache_data(ttl=86400, show_spinner=False)
def find_contact_apollo(domain: str, titles: tuple[str, ...], api_key: str) -> dict | None:
    """
    PRODUCTION PATH -- Apollo People Search, mapping a domain to the person
    who actually owns the trade-show budget.

    Endpoint: POST https://api.apollo.io/api/v1/mixed_people/search
    Body:     {"q_organization_domains": "acme.com",
               "person_titles": ["Trade Show Manager", "Event Marketing Manager", ...],
               "page": 1, "per_page": 3}
    Header:   x-api-key: <key>

    The search result includes name + title + LinkedIn URL. Verified email
    addresses cost a credit each and come from a second call:
        POST https://api.apollo.io/api/v1/people/match  {"id": <person id>, "reveal_personal_emails": false}
    Clay users: the same two steps are the "Find people at company" and
    "Enrich person" columns.
    """
    if not domain or not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_people/search",
            json={"q_organization_domains": domain, "person_titles": list(titles), "page": 1, "per_page": 3},
            headers={"x-api-key": api_key, "Cache-Control": "no-cache", "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        people = resp.json().get("people") or []
        if not people:
            return None
        person = people[0]
        return {
            "Contact": person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
            "Contact Title": person.get("title") or "",
            "Contact Email": person.get("email") or "",
        }
    except Exception:
        return None


def enrich_companies(rows: list[dict], api_key: str | None = None, progress=None) -> pd.DataFrame:
    """
    Append Revenue / Headcount / Industry / decision-maker targeting to every
    scraped row.

    With no API key every row is simulated. With a key, Apollo is tried first
    and simulation only fills gaps (missing domain, no match, API error).
    Contacts: sample rows get fictional contacts; live rows get Apollo people
    (if a key is present) or stay blank -- the app never invents a real person.
    """
    enriched = []
    for i, row in enumerate(rows):
        domain = urlparse(row.get("Website") or "").netloc.replace("www.", "")
        sim = simulate_enrichment(row["Company"], row.get("Website", ""), int(row.get("Sq Ft") or 0))
        result = enrich_company_apollo(domain, api_key) if (api_key and domain) else None
        if result:
            for k, v in sim.items():  # fill any blanks Apollo left
                if result.get(k) in (None, ""):
                    result[k] = v
        else:
            result = sim

        titles = recommend_titles(int(result["Headcount"]))
        contact = {"Contact": "", "Contact Title": "", "Contact Email": ""}
        if row.get("Size Source") == "sample":
            contact = simulate_contact(row["Company"], row.get("Website", ""), int(result["Headcount"]), i)
        elif api_key and domain:
            contact = find_contact_apollo(domain, tuple(titles), api_key) or contact

        enriched.append({**row, **result, **contact, "Target Titles": " > ".join(titles)})
        if progress is not None:
            progress(i + 1, len(rows), row["Company"])
        time.sleep(0.02)  # purely cosmetic -- makes the status log feel like real API calls
    return pd.DataFrame(enriched)


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

    def tier(row):
        if row["Goldilocks"]:
            return "Goldilocks"
        if row["Score"] >= NEAR_MISS_THRESHOLD:
            return "Near miss"
        return "Out of range"

    out["Tier"] = out.apply(tier, axis=1)
    out["Est. Deal ($)"] = (out["Sq Ft"] * params["price_per_sqft"]).astype(int)
    return out.sort_values(["Goldilocks", "Score"], ascending=[False, False]).reset_index(drop=True)


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

    timeline = outreach_timeline(show_start)
    hook = _timeline_line(show, show_start, timeline)
    angle = PERSONA_ANGLES.get(contact_title, PERSONA_ANGLES["Event Marketing Manager"]).format(industry=industry.lower(), show=show)
    venue_txt = venue or "the convention center"

    name = sender.get("name") or "[Your name]"
    title = sender.get("title") or "Account Executive"
    phone = sender.get("phone") or ""
    signature = f"{name}\n{title}, {COMPANY_NAME} -- Las Vegas, NV\n{COMPANY_SITE}" + (f"\n{phone}" if phone else "")

    first_touch = timeline["first_touch"]
    send_dates = [first_touch + timedelta(days=d) for d in SEQUENCE_OFFSETS_DAYS]

    touch1 = (
        f"{greeting}\n\n"
        f"{hook} -- and I noticed {company} has a {dims} ({sqft:,} sq ft) space at {show}, Booth {booth}.\n\n"
        f"That footprint is the sweet spot where a custom exhibit starts paying for itself in foot traffic, "
        f"and it's the size we build most for {industry.lower()} exhibitors. {angle}\n\n"
        f"Two things we'd like to put on the table up front:\n\n"
        f"1. A complimentary 3D LED rendering of your {dims} at {show}: LED wall, lighting and demo stations "
        f"designed around your product line, so you can see how it lands on the floor before you commit to "
        f"anything. Yours to keep either way.\n"
        f"2. A local build. {COMPANY_NAME} designs, fabricates, installs and dismantles in Las Vegas, which cuts "
        f"cross-country freight, drayage friction and the last-minute on-site emergencies that come with an "
        f"out-of-town exhibit house.\n\n"
        f"You can see our custom fabrication and turnkey rental work at {COMPANY_SITE}.\n\n"
        f"Would a 15-minute call next week make sense to talk through your {show} plans?\n\n"
        f"{signature}"
    )
    touch2 = (
        f"{greeting}\n\n"
        f"Quick bump in case this got buried. The offer stands: a free 3D LED rendering of your {dims} at "
        f"{show} (Booth {booth}), no strings attached.\n\n"
        f"If someone else owns the {show} program on your side, could you point me their way?\n\n"
        f"{signature}"
    )
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
    touch4 = (
        f"{greeting}\n\n"
        f"I'll close the loop here so I'm not cluttering your inbox. If a custom {dims} build or a turnkey "
        f"rental for {show} comes up on your side, the 3D rendering offer stands -- just reply and we'll get "
        f"it moving.\n\n"
        f"Best of luck with the show.\n\n"
        f"{signature}"
    )

    return [
        {"step": 1, "label": "Touch 1: timeline + render offer", "send_date": send_dates[0],
         "subject": f"{company} at {show}: your {dims} space (Booth {booth})", "body": touch1},
        {"step": 2, "label": "Touch 2: bump (+4 days)", "send_date": send_dates[1],
         "subject": f"Re: {company} at {show}: your {dims} space (Booth {booth})", "body": touch2},
        {"step": 3, "label": "Touch 3: the Las Vegas math (+10 days)", "send_date": send_dates[2],
         "subject": f"The Las Vegas math on Booth {booth} at {show}", "body": touch3},
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
            "Industry": lead["Industry"],
            "Revenue ($M)": lead["Revenue ($M)"],
            "Headcount": lead["Headcount"],
            "Score": lead["Score"],
            "Tier": lead["Tier"],
            "Est. Deal ($)": lead["Est. Deal ($)"],
            "Target Titles": lead["Target Titles"],
            "Contact": lead.get("Contact", ""),
            "Contact Title": lead.get("Contact Title", ""),
            "Contact Email": lead.get("Contact Email", ""),
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
        .ax-fallback { background: rgba(245,158,11,.18); color: #d97706; border: 1px solid rgba(245,158,11,.45); }
        .ax-neutral  { background: rgba(59,130,246,.15); color: #3b82f6; border: 1px solid rgba(59,130,246,.4); }
        .ax-muted { opacity: .7; font-size: .85rem; }
        .ax-legend { display:inline-block; width: 12px; height: 12px; border-radius: 3px;
                     background: rgba(34,197,94,.35); border: 1px solid rgba(34,197,94,.7); vertical-align: middle; }
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
            <p>Paste an exhibitor directory, find the mid-market booths worth a custom 3D LED build,
               and generate a timed outreach sequence for each one.</p>
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
        st.checkbox("Show only Goldilocks matches", key="only_goldilocks")
        st.button("Reset to defaults", on_click=reset_filters)

        st.divider()
        st.markdown("**Deal assumptions**")
        st.number_input(
            "Exhibit value per sq ft ($)", min_value=25, max_value=1000, step=25, key="price_per_sqft",
            help="Pipeline estimate = booth sq ft x $/sq ft for every qualified lead.",
        )

        st.divider()
        st.markdown("**Sender details** (for the email sequence)")
        sender = {
            "name": st.text_input("Your name", value="", placeholder="Jordan Lee"),
            "title": st.text_input("Title", value="Account Executive"),
            "phone": st.text_input("Phone / email (optional)", value=""),
        }

        st.divider()
        with st.expander("Enrichment settings (optional)"):
            st.caption(
                "Without a key, enrichment is simulated. Paste an Apollo.io API key to replace the "
                "simulation with live organisation lookups and people search for the target titles."
            )
            secret_key = ""
            try:
                secret_key = st.secrets.get("APOLLO_API_KEY", "")  # .streamlit/secrets.toml
            except Exception:
                secret_key = ""
            apollo_key = st.text_input("Apollo API key", value=secret_key, type="password")
            st.caption("Target titles, in order: " + ", ".join(TARGET_TITLES))

    return {
        "booth_range": st.session_state["booth_range"],
        "revenue_range": st.session_state["revenue_range"],
        "headcount_range": st.session_state["headcount_range"],
        "only_goldilocks": st.session_state["only_goldilocks"],
        "price_per_sqft": st.session_state["price_per_sqft"],
        "sender": sender,
        "apollo_key": apollo_key.strip() or None,
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


def run_pipeline(url: str, apollo_key: str | None, chosen_show: str, force_sample: bool = False) -> None:
    """Scrape (or fall back), enrich, and stash the result in session state."""
    with st.status("Working...", expanded=True) as status:
        rows, meta, warning = None, None, None

        if force_sample:
            rows = load_fallback_dataset()
            convention = infer_convention_name(url) if url else FALLBACK_CONVENTION
            if chosen_show == "Auto-detect from URL" and not url:
                convention = FALLBACK_CONVENTION
            meta = build_meta(url, convention, detect_platform(url) if url else "Sample", "fallback",
                              "Sample dataset loaded on request.", chosen_show)
            st.write("Loaded the bundled sample dataset (20 exhibitors).")
        else:
            st.write(f"Fetching directory: `{url}`")
            try:
                rows, live_meta = scrape_directory(url)
                meta = build_meta(url, live_meta["convention"], live_meta["platform"], "live", "", chosen_show)
                st.write(f"Parsed **{len(rows)}** exhibitors from {meta['platform']}.")
            except ScrapeError as exc:
                warning = str(exc)
            except Exception as exc:  # belt and braces -- the demo must not break
                warning = f"Unexpected error: {exc.__class__.__name__}"

            if warning:
                # ---- Graceful fallback ---------------------------------------
                # Convention sites routinely sit behind Cloudflare or render the
                # exhibitor list with JavaScript, so a plain HTTP scrape often
                # comes back empty. Rather than surfacing a stack trace, load a
                # dataset with the exact same columns so every downstream
                # module still works.
                st.write(f"Live scrape failed: {warning}")
                st.write("Loading the fallback scrape dataset so the pipeline continues.")
                rows = load_fallback_dataset()
                meta = build_meta(url, infer_convention_name(url), detect_platform(url), "fallback", warning, chosen_show)

        # ---- Enrichment ----------------------------------------------------
        st.write("Enriching via Apollo API..." if apollo_key else "Enriching (simulated Apollo/Clearbit lookup)...")
        bar = st.progress(0.0, text="Starting enrichment")

        def on_progress(done: int, total: int, company: str) -> None:
            bar.progress(done / total, text=f"{done}/{total}  {company}")

        leads = enrich_companies(rows, api_key=apollo_key, progress=on_progress)
        bar.progress(1.0, text=f"Enriched {len(leads)} companies")

        meta["scraped_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
        st.session_state["leads"] = leads
        st.session_state["meta"] = meta

        status.update(
            label=(f"Done: {len(leads)} exhibitors from {meta['convention']} "
                   f"({'live scrape' if meta['source'] == 'live' else 'fallback dataset'})"),
            state="complete", expanded=False,
        )


# =============================================================================
# 9. MAIN
# =============================================================================


def render_empty_state() -> None:
    with st.container(border=True):
        st.markdown("**How to use this**")
        st.markdown(
            """
            1. Pick a target show below (or leave auto-detect) and paste its exhibitor-list URL. Typical formats:
               `https://<show>.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm`
               or `https://<show>.a2zinc.net/<Event>/Public/Exhibitors.aspx`
            2. Click **Scrape & Analyze**. If the site blocks the request or renders with JavaScript,
               the app loads a sample dataset automatically so the workflow still runs end-to-end.
            3. Tune the Goldilocks filter in the sidebar, click a row, and the 4-touch sequence is ready to send.
            """
        )
    st.markdown("#### Target shows: Las Vegas, March - June 2027")
    render_show_calendar()


def main() -> None:
    st.set_page_config(page_title=f"{COMPANY_NAME} | Lead Intelligence", layout="wide", initial_sidebar_state="expanded")
    init_state()
    inject_css()
    render_header()
    params = render_sidebar()

    # ---- Module 1: URL input -------------------------------------------------
    with st.form("scrape_form", clear_on_submit=False):
        c_url, c_show = st.columns([4, 2])
        url = c_url.text_input(
            "Enter Trade Show Directory URL (e.g., MapYourShow / A2Z Events)",
            placeholder="https://nab2027.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm",
        )
        chosen_show = c_show.selectbox(
            "Target show", ["Auto-detect from URL"] + [s["name"] for s in TARGET_SHOWS], index=0,
            help="Sets the show name, date and venue used in the emails and the outreach timeline.",
        )
        b1, b2, _ = st.columns([1.4, 1.4, 4])
        scrape_clicked = b1.form_submit_button("Scrape & Analyze", type="primary", **_wide(st.form_submit_button))
        sample_clicked = b2.form_submit_button("Load sample data", **_wide(st.form_submit_button))

    url = (url or "").strip()
    if scrape_clicked:
        if not url:
            st.warning("Paste a directory URL first, or use **Load sample data** to see the workflow.")
        else:
            if not url.startswith("http"):
                url = "https://" + url
            run_pipeline(url, params["apollo_key"], chosen_show)
    elif sample_clicked:
        run_pipeline(url, params["apollo_key"], chosen_show, force_sample=True)

    leads: pd.DataFrame | None = st.session_state.get("leads")
    meta: dict | None = st.session_state.get("meta")
    if leads is None or meta is None:
        render_empty_state()
        return

    with st.expander("Target shows: Las Vegas, March - June 2027", expanded=False):
        render_show_calendar()

    # ---- Result header: source badge, convention name + date, export --------
    h1, h2, h3, h4 = st.columns([2.8, 1.8, 1.3, 1.1])
    with h1:
        badge = ("<span class='ax-badge ax-live'>Live scrape</span>" if meta["source"] == "live"
                 else "<span class='ax-badge ax-fallback'>Fallback dataset</span>")
        badge += f"<span class='ax-badge ax-neutral'>{meta['platform']}</span>"
        st.markdown(badge, unsafe_allow_html=True)
        note = f"{len(leads)} exhibitors &middot; {meta['scraped_at']}"
        if meta["source"] != "live" and meta.get("reason"):
            note += f" &middot; {meta['reason']}"
        st.markdown(f"<div class='ax-muted'>{note}</div>", unsafe_allow_html=True)
    state_key = slug(f"{meta['url']}_{meta['convention']}")
    with h2:
        convention = st.text_input("Convention name (used in emails)", value=meta["convention"], key=f"conv_{state_key}").strip() or meta["convention"]
    with h3:
        show_date = st.date_input("Show start date", value=meta.get("show_date"), key=f"date_{state_key}",
                                  help="Drives the outreach window and the send dates in the sequence.")
        if isinstance(show_date, (list, tuple)):
            show_date = show_date[0] if show_date else None
    venue = meta.get("venue") or (SHOW_BY_NAME.get(convention, {}).get("venue", ""))
    timeline = outreach_timeline(show_date)

    # ---- Module 2: scoring ---------------------------------------------------
    scored = score_leads(leads, params)
    qualified = scored[scored["Goldilocks"]]
    pipeline_value = int(qualified["Est. Deal ($)"].sum())

    with h4:
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
        st.metric("Goldilocks qualified leads", f"{len(qualified):,}",
                  delta=f"{len(qualified) / len(scored):.0%} of list" if len(scored) else None, delta_color="off")
    with k3, st.container(border=True):
        st.metric("Estimated pipeline value", fmt_money(pipeline_value),
                  help=f"Sum of booth sq ft x ${params['price_per_sqft']}/sq ft across qualified leads.")
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
    tiers = f2.multiselect("Tier", ["Goldilocks", "Near miss", "Out of range"], default=["Goldilocks", "Near miss", "Out of range"])

    view = scored.copy()
    if params["only_goldilocks"]:
        view = view[view["Goldilocks"]]
    if tiers:
        view = view[view["Tier"].isin(tiers)]
    if search:
        mask = (
            view["Company"].str.contains(search, case=False, na=False)
            | view["Industry"].str.contains(search, case=False, na=False)
            | view["Contact"].astype(str).str.contains(search, case=False, na=False)
        )
        view = view[mask]
    view = view.reset_index(drop=True)

    near = int((scored["Tier"] == "Near miss").sum())
    st.markdown(
        f"<span class='ax-legend'></span> <span class='ax-muted'>Green rows meet all three Goldilocks criteria "
        f"({near} near-miss leads also worth a call). Click a row to open the outreach sequence.</span>",
        unsafe_allow_html=True,
    )

    display_cols = ["Company", "Booth", "Booth Size", "Sq Ft", "Revenue ($M)", "Headcount", "Industry",
                    "Score", "Tier", "Contact", "Contact Title", "Est. Deal ($)", "Website"]
    grid_df = view[display_cols]

    def highlight(row: pd.Series) -> list[str]:
        is_match = bool(view.loc[row.name, "Goldilocks"])
        return ["background-color: rgba(34,197,94,0.20)" if is_match else ""] * len(row)

    styler = grid_df.style.apply(highlight, axis=1).format({"Revenue ($M)": "{:,.1f}", "Est. Deal ($)": "${:,.0f}"})

    column_config = {
        "Company": st.column_config.TextColumn("Company", width="medium"),
        "Booth": st.column_config.TextColumn("Booth #", width="small"),
        "Booth Size": st.column_config.TextColumn("Size", width="small"),
        "Sq Ft": st.column_config.NumberColumn("Sq Ft", width="small", format="%d"),
        "Revenue ($M)": st.column_config.NumberColumn("Revenue ($M)", width="small"),
        "Headcount": st.column_config.NumberColumn("Headcount", width="small", format="%d"),
        "Industry": st.column_config.TextColumn("Industry", width="medium"),
        "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d", width="small"),
        "Tier": st.column_config.TextColumn("Tier", width="small"),
        "Contact": st.column_config.TextColumn("Contact", width="small", help="Sample rows carry fictional contacts; live rows fill from Apollo."),
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
            st.markdown(
                f"- Booth: {range_note(lead['Sq Ft'], b_lo, b_hi, ' sq ft')} ({lead['Booth Size']})\n"
                f"- Revenue: {range_note(lead['Revenue ($M)'], r_lo, r_hi, 'M', '${:,.1f}')}\n"
                f"- Headcount: {range_note(lead['Headcount'], h_lo, h_hi, ' employees')}\n"
                f"- Industry: {lead['Industry']}\n"
                f"- Est. deal: {fmt_money(lead['Est. Deal ($)'])} ({int(lead['Sq Ft'])} sq ft x ${params['price_per_sqft']}/sq ft)\n"
                f"- Data: booth size {lead['Size Source']}, enrichment {lead['Enrichment']}"
            )
            st.markdown("**Who to reach**")
            if lead.get("Contact"):
                st.markdown(f"- {lead['Contact']}, {lead['Contact Title']}" + (f"\n- {lead['Contact Email']}" if lead.get("Contact Email") else ""))
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
