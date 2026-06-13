#!/usr/bin/env python3
"""Merlin Cinemas Calendar Scraper.

Scrapes upcoming film releases from Merlin Cinemas and generates per-cinema
iCalendar feeds plus an index page for GitHub Pages.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import os
import re
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

warnings.filterwarnings("ignore", message=".*OpenSSL.*", category=UserWarning)

import requests
from bs4 import BeautifulSoup

from html_templates import (
    _SHARED_CSS, CSS, FILM_CSS,
    _esc, _format_runtime_display, _stars_from_rating,
    build_index_html, build_cinema_page, build_film_page,
    _cert_span, _youtube_embed_url, _extract_bbfc,
    _cert_class_name, _preferred_display_title,
    write_style_css,
)

# ── Constants ──────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_RETRY_DELAY = 1
HTTP_RETRY_MULTIPLIER = 2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
ICAL_LINE_LENGTH = 75
ICAL_NEWLINE = "\r\n"
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Europe/London")
OUTPUT_DIR = "docs"
MERLIN_BASE_URL = "https://www.merlincinemas.co.uk"
RELEASE_HISTORY_PATH = ".release_history.json"
RELEASE_HISTORY_MAX_DAYS = 730
CACHE_FILE = ".film_cache.json"
CACHE_EXPIRY_DAYS = 7
TMDB_CACHE_FILE = ".tmdb_cache.json"
TMDB_CACHE_DAYS = 30
TMDB_DELAY_SEC = 0.2
POSTERS_DIR = "docs/posters"
CERTS_DIR = "docs/certs"
RATINGS_DIR = "docs/ratings"
FINGERPRINT_FILE = ".scrape_fingerprint"
MIN_SYNOPSIS_LENGTH = 50
MAX_SYNOPSIS_LENGTH = 500
SYNOPSIS_SKIP_TERMS = ["cookie", "privacy", "terms", "wheelchair", "audio description"]
MAX_WORKERS = min(4, os.cpu_count() or 4)

DATE_PATTERN = re.compile(r"(?:Released|Showing)\s+(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)")
ALT_DATE_PATTERN = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)")
RUNTIME_RE = re.compile(r"(\d+)\s*(?:minutes?|mins?)", re.IGNORECASE)
FILM_LINK_RE = re.compile(r"/film/")
TITLE_CLEAN_RE = re.compile(r"\s*\([^)]*\)$")

# Merlin special screening suffixes - stripped before TMDb search
MERLIN_TITLE_CLEAN = [
    (re.compile(r"\s+Toddler Cinema$", re.IGNORECASE), ""),
    (re.compile(r"\s+Double Bill$", re.IGNORECASE), ""),
    (re.compile(r"\s+Triple Bill$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Toddler Cinema$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Kids Club$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Silver Screen$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Mini Movie Deal$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Parent & Baby$", re.IGNORECASE), ""),
    (re.compile(r"\s+Parent & Baby$", re.IGNORECASE), ""),
    (re.compile(r"\s+Parent And Baby$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Autism Friendly$", re.IGNORECASE), ""),
    (re.compile(r"\s+Autism Friendly$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Event Cinema$", re.IGNORECASE), ""),
    (re.compile(r"\s+Event Cinema$", re.IGNORECASE), ""),
    (re.compile(r"\s+with Q&A$", re.IGNORECASE), ""),
    (re.compile(r"\s+with Q and A$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*with Q&A$", re.IGNORECASE), ""),
    (re.compile(r"\s+Silver Screen$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-–]\s*Super Saver$", re.IGNORECASE), ""),
    (re.compile(r"\s+Super Saver$", re.IGNORECASE), ""),
    (re.compile(r"^NT Live:\s*", re.IGNORECASE), ""),
    (re.compile(r"^RBO \d{4}-\d{2}:\s*", re.IGNORECASE), ""),
]

# Screening labels for display - derived from pattern names
_SCREENING_LABEL_MAP = {
    "Toddler Cinema": "Toddler Cinema", "Kids Club": "Kids Club",
    "Silver Screen": "Silver Screen", "Mini Movie Deal": "Mini Movie Deal",
    "Parent & Baby": "Parent & Baby", "Parent And Baby": "Parent & Baby",
    "Autism Friendly": "Autism Friendly", "Event Cinema": "Event Cinema",
    "Super Saver": "Super Saver", "Double Bill": "Double Bill",
    "Triple Bill": "Triple Bill", "NT Live": "NT Live", "RBO": "RBO",
    "with Q&A": "Q&A", "with Q and A": "Q&A",
}

def _clean_display_title(title: str) -> str:
    """Normalise display titles from Merlin/TMDb and trim dangling separators."""
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip()
    cleaned = re.sub(r"^([^&]{2,40}?)\s*&\s*\1\b", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[-–:|/]+\s*$", "", cleaned).strip()
    return cleaned


def _preferred_display_title(raw_title: str, details: Optional[Dict[str, Any]] = None) -> str:
    """Prefer canonical TMDb title when available, otherwise tidy the scraped title."""
    canonical = ""
    if details:
        canonical = str(details.get("title") or "").strip()
    return _clean_display_title(canonical or raw_title)


def extract_screening_label(title: str):
    """Return (cleaned_title, screening_label) for a Merlin film title.
    Matches against MERLIN_TITLE_CLEAN patterns and returns the
    corresponding friendly label for UI display."""
    for pattern, _ in MERLIN_TITLE_CLEAN:
        m = pattern.search(title)
        if m:
            cleaned = _clean_display_title(pattern.sub("", title))
            # Derive label from the pattern name
            for key, label in _SCREENING_LABEL_MAP.items():
                if key.lower() in m.group(0).lower():
                    return cleaned, label
            return cleaned, ""
    return _clean_display_title(title), ""

# Non-film events to skip TMDb enrichment entirely
MERLIN_SKIP_TMDB = [
    "live nation", "tribute", "comedy club", "showcase", "panto",
    "psychic", "candlelit", "mania", "mania 202", "tour", "on tour",
    "presented by", "choir", "orchestra", "big band", "tribute show",
    "theatre company", "pride", "adults only", "p*ssed", "music show",
    "reunion", "dance show", "cheerleading", "boyband", "girlband",
    "roadshow", "motown", "beatles", "elvis", "frankie valli",
    "candlelight", "forbidden nights", "showaddywaddy", "dazzling",
    "illegal eagles", "drifters", "dolly show", "monkees tale",
    "80's mania", "seriously collins", "lipstick on your collar",
    "elvis blue", "johnny cash", "oh what a night", "step into christmas",
    "twist & shout", "beyond the barricade", "ultimate boyband",
    "elo again", "t.rextasy", "queen of the desert", "cabaret extreme",
    "stuart michael", "vincent simone", "nick cope", "scott bennett",
    "marcus brigstocke", "russell kane", "flo & joan", "choose george",
    
    "redruth comedy", "ritz penzance", "movie magic card",
    "cornish cheerleading", "santa's best", "good old fashioned",
    "ritz on screen", "kizamba at the ritz", "willow hill at the ritz",
    "a splann night", "ta-daaa", "spice girls the theatre",
    "kpop live", "celtic nights", "another kind of magic at",
    "a summer princess ball", "geddon pz", "golowan launch",
    "moments & movies", "cinema craft night", "baga chipz",
    "tom meighan", "revive 45", "swing into summer", "albi & the wolves",
    "met opera", "ritz on screen:",
    "the hollies story",
    "michael: starring", "dom joly", "penzance comedy", "penzance musical",
    "mark steel", "biglove presents",
    "a night to remember motown", "the abba reunion",
    "oh what a night at the",
    "a night to remember", "the cavern club", "the fm songbook",
    "frozen presented by", "we will rock you presented by",
    "pennoweth presents", "jack and the beanstalk presented",
    "the addams family presented", 
]

# Strip anniversary / special-edition suffixes before TMDb search so
# "Shrek - 25th Anniversary" resolves to the original film
TMDB_SEARCH_STRIP_RE = re.compile(
    r"\s*[-–]\s*\d+(?:st|nd|rd|th)?\s*[Aa]nniversary\b.*$"
)

CINEMAS: Dict[str, dict] = {
    "bodmin": {
        "enabled": True, "name": "Capitol Cinema", "location": "Bodmin",
        "subdomain": "bodmin",
        "url": "https://bodmin.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://bodmin.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinbodmin",
    },
    "helston": {
        "enabled": True, "name": "Flora Cinema", "location": "Helston",
        "subdomain": "helston",
        "url": "https://helston.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://helston.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinhelston",
    },
    "falmouth": {
        "enabled": True, "name": "Phoenix Cinema", "location": "Falmouth",
        "subdomain": "falmouth",
        "url": "https://falmouth.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://falmouth.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinfalmouth",
    },
    "redruth": {
        "enabled": True, "name": "Regal Cinema & Theatre", "location": "Redruth",
        "subdomain": "redruth",
        "url": "https://redruth.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://redruth.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinredruth",
    },
    "st-ives": {
        "enabled": True, "name": "Royal Cinema", "location": "St Ives",
        "subdomain": "st-ives",
        "url": "https://st-ives.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://st-ives.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinstives",
    },
    "penzance-savoy": {
        "enabled": True, "name": "Savoy Cinema", "location": "Penzance",
        "subdomain": "penzance",
        "url": "https://penzance.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://penzance.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinpenzance",
    },
    "penzance-ritz": {
        "enabled": True, "name": "The Ritz", "location": "Penzance",
        "subdomain": "ritz",
        "url": "https://ritz.merlincinemas.co.uk/coming-soon",
        "whats_on_url": "https://ritz.merlincinemas.co.uk/whats-on",
        "booking_domain": "merlinpenzance",
    },
}

NOTIFICATION_TIME = "09:00"
NOTIFICATIONS: Dict[str, Any] = {"enabled": False, "alarms": []}

# BBFC age rating: extracted from film titles like "Film Name (15)" or "Film Name (12A)"
BBFC_PATTERN = re.compile(r"\((\d{1,2}A?|U|PG|R18)\)", re.IGNORECASE)
CERT_IMAGES = {
    "U": "cert-u.png",
    "PG": "cert-pg.png",
    "12": "cert-12.png",
    "12A": "cert-12a.png",
    "15": "cert-15.png",
    "18": "cert-18.png",
}
CERT_BASE = ""  # Merlin: cert images served locally from docs/certs/
RATING_LOGOS = {
    "imdb": {
        "filename": "imdb.svg",
        "url": "https://cdn.jsdelivr.net/npm/simple-icons@v13/icons/imdb.svg",
        "color": "#f5c518",
        "label": "IMDb",
    },
    "rottentomatoes": {
        "filename": "rottentomatoes.svg",
        "url": "https://cdn.jsdelivr.net/npm/simple-icons@v13/icons/rottentomatoes.svg",
        "color": "#fa320a",
        "label": "Rotten Tomatoes",
    },
    "trakt": {
        "filename": "trakt.svg",
        "url": "https://cdn.jsdelivr.net/npm/simple-icons@v13/icons/trakt.svg",
        "color": "#ed1c24",
        "label": "Trakt",
    },
}

# Cinema addresses for map links
CINEMA_ADDRESSES = {
    "bodmin": "Capitol+Cinema+Bodmin",
    "helston": "Flora+Cinema+Helston",
    "falmouth": "Phoenix+Cinema+Falmouth",
    "redruth": "Regal+Cinema+Redruth",
    "st-ives": "Royal+Cinema+St+Ives",
    "penzance-savoy": "Savoy+Cinema+Penzance",
    "penzance-ritz": "The+Ritz+Penzance",
}


# Cinema name to location for display
CINEMA_LOCATION_MAP = {
    "Capitol Cinema": "Bodmin", "Flora Cinema": "Helston",
    "Phoenix Cinema": "Falmouth", "Regal Cinema & Theatre": "Redruth",
    "Royal Cinema": "St Ives", "Savoy Cinema": "Penzance", "The Ritz": "Penzance",
}

# Cinema coordinates for geolocation
CINEMA_COORDS = {
    "bodmin": (50.466, -4.718),
    "helston": (50.102, -5.274),
    "falmouth": (50.155, -5.067),
    "redruth": (50.233, -5.226),
    "st-ives": (50.210, -5.490),
    "penzance-savoy": (50.118, -5.538),
    "penzance-ritz": (50.118, -5.536),
}

# Health check minimums (env-configurable)
HEALTH_MIN_FILMS = int(os.getenv("HEALTH_MIN_FILMS", "1"))
HEALTH_MIN_CINEMAS = int(os.getenv("HEALTH_MIN_CINEMAS", "1"))

TMDB_GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
err_handler = logging.FileHandler("cinema_log.txt")
err_handler.setLevel(logging.WARNING)
logger.addHandler(err_handler)
import atexit as _atexit
@_atexit.register
def _close_log_handler():
    err_handler.close()

try:
    UK_TZ = ZoneInfo(CALENDAR_TIMEZONE)
except ZoneInfoNotFoundError:
    logger.warning("Unknown CALENDAR_TIMEZONE %r; falling back to Europe/London", CALENDAR_TIMEZONE)
    UK_TZ = ZoneInfo("Europe/London")
TMDB_FIELDS = (
    "overview",
    "genres",
    "vote_average",
    "director",
    "cast",
    "poster_url",
    "poster_large_url",
    "backdrop_url",
    "runtime",
    "trailer_url",
    "imdb_id",
    "title",
)


# ── HTTP ───────────────────────────────────────────────────────────────────────
def _session() -> requests.Session:
    """Return a requests Session with retry-compatible settings."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_with_retries(
    url: str,
    retries: int = HTTP_RETRIES,
    timeout: int = HTTP_TIMEOUT,
    session: Optional[requests.Session] = None,
) -> requests.Response:
    """Return HTTP response with exponential-backoff retries."""
    s = session or _session()
    delay = HTTP_RETRY_DELAY
    for attempt in range(retries):
        try:
            resp = s.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("Attempt %d/%d failed for %s: %s", attempt + 1, retries, url, exc)
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= HTTP_RETRY_MULTIPLIER


def get_base_film_url(url: str) -> str:
    """Strip query string, return canonical film URL."""
    return url.split("?")[0] if "?" in url else url


# ── Caches ─────────────────────────────────────────────────────────────────────
def _cutoff(expiry_days: int) -> str:
    return (_utc_now() - timedelta(days=expiry_days)).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso_now() -> str:
    return _utc_now().isoformat()


def _site_timestamp(dt: Optional[datetime] = None) -> str:
    current = dt or _utc_now()
    return current.astimezone(UK_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _cert_class_name(rating: str) -> str:
    return rating.strip().lower()


def _load_json_cache(path: str, ttl_days: int, label: str = "") -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        limit = _cutoff(ttl_days)
        fresh = {k: v for k, v in data.items() if v.get("cached_at", "") > limit}
        logger.info("Loaded %s: %d entries (%d expired)", label, len(fresh), len(data) - len(fresh))
        return fresh
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("%s load failed: %s", label, e)
        return {}


def _save_json_cache(path: str, cache: Dict[str, dict], label: str = "") -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        logger.info("Saved %s: %d entries", label, len(cache))
    except OSError as e:
        logger.warning("%s save failed: %s", label, e)


def load_cache() -> Dict[str, dict]:
    return _load_json_cache(CACHE_FILE, CACHE_EXPIRY_DAYS, "film cache")


def save_cache(cache: Dict[str, dict]) -> None:
    _save_json_cache(CACHE_FILE, cache, "film cache")


def load_tmdb_cache() -> Dict[str, dict]:
    return _load_json_cache(TMDB_CACHE_FILE, TMDB_CACHE_DAYS, "TMDb cache")


def save_tmdb_cache(cache: Dict[str, dict]) -> None:
    _save_json_cache(TMDB_CACHE_FILE, cache, "TMDb cache")


def load_release_history() -> set:
    path = Path(RELEASE_HISTORY_PATH)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = set()
        for item in data:
            if not (isinstance(item, (list, tuple)) and len(item) >= 2):
                continue
            try:
                out.add((date.fromisoformat(item[0]), item[1]))
            except (ValueError, TypeError):
                pass
        return out
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Release history load failed: %s", e)
        return set()


def save_release_history(releases: set) -> None:
    today = date.today()
    cutoff = today - timedelta(days=RELEASE_HISTORY_MAX_DAYS)
    kept = [(d.isoformat(), t) for (d, t) in releases if d >= cutoff]
    try:
        Path(RELEASE_HISTORY_PATH).write_text(
            json.dumps(kept, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Saved release history: %d entries", len(kept))
    except OSError as e:
        logger.warning("Release history save failed: %s", e)


def _tmdb_cache_key(film_title: str) -> str:
    t = TITLE_CLEAN_RE.sub("", film_title).strip()
    t = re.sub(r"[\s\-:]+", " ", t.lower()).strip()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "unknown"


# ── Date parsing ───────────────────────────────────────────────────────────────
def parse_date(text: str) -> Optional[date]:
    """Parse Merlin release date: 'Released 3rd Jun' or 'Showing on 4th Jun'."""
    m = DATE_PATTERN.search(text)
    if m:
        day, month_str = int(m.group(1)), m.group(2)
    else:
        return None

    for fmt in ("%B", "%b"):
        try:
            month = datetime.strptime(month_str, fmt).month
            break
        except ValueError:
            continue
    else:
        return None

    year = date.today().year
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None

    if parsed < date.today():
        try:
            parsed = date(year + 1, month, day)
        except ValueError:
            pass
    return parsed


def parse_uk_date(text: str, scrape_date: date) -> Optional[date]:
    """Parse UK showtime dates: 'Today 8 February', 'Tomorrow 9 February', 'Tuesday 10 February 2026'."""
    text = text.strip()
    today = scrape_date
    if "today" in text.lower():
        return today
    if "tomorrow" in text.lower():
        return today + timedelta(days=1)
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        try:
            return datetime.strptime(f"{day} {month_str} {year}", "%d %B %Y").date()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)(?:\s|$)", text)
    if m:
        day, month_str = int(m.group(1)), m.group(2)
        year = scrape_date.year
        try:
            dt = datetime.strptime(f"{day} {month_str} {year}", "%d %B %Y").date()
            if dt < today:
                dt = datetime.strptime(f"{day} {month_str} {year + 1}", "%d %B %Y").date()
            return dt
        except ValueError:
            pass
    return None


# ── Film detail scraping ───────────────────────────────────────────────────────
_film_cache_lock = threading.Lock()
_tmdb_cache_lock = threading.Lock()


def fetch_film_details(
    film_url: str, cache: Dict[str, dict], session: Optional[requests.Session] = None
) -> Dict[str, str]:
    """Fetch runtime, cast, synopsis from a film page. Uses cache when available."""
    details: Dict[str, str] = {"runtime": "", "cast": "", "synopsis": "", "director": ""}
    if not film_url:
        return details

    base_url = get_base_film_url(film_url)

    # Thread-safe cache check
    with _film_cache_lock:
        if base_url in cache:
            c = cache[base_url].copy()
            c.pop("cached_at", None)
            return c

    try:
        logger.info("Fetching film: %s", film_url)
        resp = fetch_with_retries(film_url, session=session)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Runtime: match "119 minutes" etc.
        for text in soup.stripped_strings:
            m = RUNTIME_RE.search(text)
            if m:
                details["runtime"] = f"{m.group(1)} min"
                break

        # Cast: find text after "Starring:"
        for text in soup.stripped_strings:
            if "starring" in text.lower():
                rest = text.split(":", 1)[-1].strip()
                if len(rest) > 3:
                    details["cast"] = rest
                break

        # Synopsis
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            if len(text) > MIN_SYNOPSIS_LENGTH and not any(
                t in text.lower() for t in SYNOPSIS_SKIP_TERMS
            ):
                details["synopsis"] = text
                break

        if not details["synopsis"]:
            for div in soup.find_all("div"):
                text = div.get_text(strip=True)
                if MIN_SYNOPSIS_LENGTH < len(text) < MAX_SYNOPSIS_LENGTH and not any(
                    t in text.lower() for t in SYNOPSIS_SKIP_TERMS
                ):
                    details["synopsis"] = text
                    break

        logger.info(
            "Film details: runtime=%s cast=%s synopsis_len=%d",
            details["runtime"],
            bool(details["cast"]),
            len(details["synopsis"]),
        )

        with _film_cache_lock:
            cache[base_url] = {**details, "cached_at": _utc_iso_now()}

    except requests.RequestException as e:
        logger.warning("Network error for %s: %s", film_url, e)
    except Exception as e:
        logger.warning("Error fetching %s: %s", film_url, e)

    return details


def extract_films(
    url: str,
    cinema_name: str,
    cache: Dict[str, dict],
    session: Optional[requests.Session] = None,
) -> List[Tuple[date, str, str, str, Dict[str, str]]]:
    """Scrape film listing from a Merlin cinema's coming-soon page."""
    logger.info("Scraping: %s (%s)", url, cinema_name)
    resp = fetch_with_retries(url, session=session)
    soup = BeautifulSoup(resp.text, "html.parser")

    films: List[Tuple[date, str, str, str, Dict[str, str]]] = []
    seen: set = set()

    for card in soup.select(".filmCard"):
        data_film = card.get("data-film", "")
        if not data_film:
            continue

        parts = data_film.split("-", 1)
        film_slug = parts[1] if len(parts) > 1 else data_film
        film_url = f"/film/{film_slug}"

        h2 = card.find("h2")
        if not h2:
            continue
        title = TITLE_CLEAN_RE.sub("", h2.get_text(strip=True))

        release_div = card.select_one(".release_date, [class*=release]")
        release_text = release_div.get_text(strip=True) if release_div else ""
        release_date = parse_date(release_text) if release_text else None

        # Extract screening label BEFORE appending cert
        clean_title, screening = extract_screening_label(title)

        # Get cert from card (strip TBC / AS LIVE suffixes)
        cert_rating = ""
        cert_div = card.select_one(".cert, [data-cert]")
        if cert_div:
            cert_rating = (cert_div.get("data-cert", "") or "").replace(" TBC", "").replace(" AS LIVE", "").strip()
            if cert_rating.upper() == "TBC":
                cert_rating = ""

        if release_date and clean_title:
            key = (release_date, clean_title, cinema_name, film_url)
            if key not in seen:
                details = {"runtime": "", "cast": "", "synopsis": "", "director": "", "screening": screening, "bbfc": cert_rating}
                films.append((release_date, clean_title, cinema_name, film_url, details))
                seen.add(key)
                logger.info("  %s - %s (%s)", clean_title, release_date, cinema_name)

    return films


def scrape_cinema_whats_on(
    cinema_id: str,
    cinema_name: str,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Scrape a Merlin cinema film page for all films with showtime schedules."""
    url = CINEMAS[cinema_id]["whats_on_url"]
    info = CINEMAS[cinema_id]
    logger.info("Scraping whats-on: %s (%s)", url, cinema_name)
    resp = fetch_with_retries(url, session=session)
    soup = BeautifulSoup(resp.text, "html.parser")

    films: Dict[str, Dict[str, Any]] = {}
    today_scrape = date.today()

    icon_map = {
        "access": ("WA", "Wheelchair Access"), "licensed": ("LIC", "Licensed venue"),
        "subtitled": ("CC", "Subtitled"), "saver": ("SAV", "Super Saver"),
        "mm": ("MM", "Mini Movie Deal"), "minimer": ("MM", "Mini Movie Deal"),
        "audio": ("AD", "Audio Description"), "autism": ("AF", "Autism Friendly"),
    }

    frames = soup.select(".films_on_day [data-frame]")
    if not frames:
        return list(films.values())

    for frame in frames:
        frame_date_str = frame.get("data-frame", "")
        frame_date = None
        if frame_date_str and frame_date_str != "full":
            try:
                frame_date = date.fromisoformat(frame_date_str)
            except ValueError:
                pass

        for card in frame.select(".filmCard"):
            data_film = card.get("data-film", "")
            if not data_film:
                continue
            parts = data_film.split("-", 1)
            film_slug = parts[1] if len(parts) > 1 else data_film
            film_url = f"/film/{film_slug}"

            h2 = card.find("h2")
            if not h2:
                continue
            title = TITLE_CLEAN_RE.sub("", h2.get_text(strip=True))
            if not title:
                continue

            cert_div = card.select_one(".cert, [data-cert]")
            cert_rating = ""
            if cert_div:
                cert_rating = (cert_div.get("data-cert", "") or "").replace(" TBC", "").replace(" AS LIVE", "").strip()
                if cert_rating.upper() == "TBC":
                    cert_rating = ""

            img_div = card.select_one(".img.lazy, .img")
            poster_url = img_div.get("data-src", "") if img_div else ""

            if film_slug not in films:
                clean_title, clean_screening = extract_screening_label(TITLE_CLEAN_RE.sub("", title).strip())
                films[film_slug] = {
                    "title": clean_title,
                    "film_url": film_url,
                    "cinema_id": cinema_id,
                    "cinema_name": cinema_name,
                    "poster_url": poster_url,
                    "screening": clean_screening,
                    "bbfc": cert_rating,
                    "showtimes": [],
                }

            listings = card.select_one(".listings")
            if not listings:
                continue

            for row in listings.select("tr"):
                h5 = row.find("h5")
                date_text = h5.get_text(strip=True) if h5 else ""
                parsed_date = parse_uk_date(date_text, today_scrape) if date_text else frame_date
                if not parsed_date:
                    continue

                for a_tag in row.find_all("a", href=True):
                    href = a_tag.get("href", "")
                    if "admit-one" not in href and "ticket" not in href.lower():
                        continue
                    time_m = re.search(r"(\d{1,2}:\d{2})", a_tag.get_text(strip=True))
                    if not time_m:
                        continue
                    time_str = time_m.group(1)

                    tags = []
                    for icon in a_tag.select("img[data-key]"):
                        key = icon.get("data-key", "")
                        if key in icon_map:
                            tags.append(icon_map[key][0])

                    films[film_slug]["showtimes"].append({
                        "date": parsed_date, "time": time_str, "screen": 1,
                        "booking_url": href, "tags": tags,
                        "cinema_name": cinema_name,
                    })

    # Deduplicate and sort
    for slug in films:
        seen_st = set()
        unique_st = []
        for st in films[slug]["showtimes"]:
            key = (st["date"], st["time"])
            if key not in seen_st:
                seen_st.add(key)
                unique_st.append(st)
        unique_st.sort(key=lambda s: (s["date"], s["time"]))
        films[slug]["showtimes"] = unique_st

    result = list(films.values())
    logger.info("  whats-on %s: %d films with showtimes", cinema_name, len(result))
    return result

def _normalize_title_for_match(title: str) -> str:
    if not title:
        return ""
    return re.sub(r"[\s\-:]+", " ", title.lower()).strip()


def _pick_best_tmdb_result(results: List[Dict], search_title: str, release_year: Optional[int] = None) -> Optional[Dict]:
    if not results or not search_title:
        return results[0] if results else None
    norm_search = _normalize_title_for_match(search_title)
    if not norm_search:
        return results[0]

    # Exact match on normalized title
    for r in results:
        norm = _normalize_title_for_match(r.get("title") or "")
        if norm == norm_search:
            # Prefer result matching the expected release year
            if release_year and (r.get("release_date") or "").startswith(str(release_year)):
                return r
            return r

    # No exact match - score by substring + release year proximity
    best, best_score = None, -1
    for r in results:
        title = (r.get("title") or "").strip()
        norm = _normalize_title_for_match(title)
        # Substring match
        score = 90 if norm_search in norm else (50 if norm in norm_search else 0)
        # Bonus for matching release year
        if release_year and (r.get("release_date") or "").startswith(str(release_year)):
            score += 30
        if score == 0:
            try:
                y = int((r.get("release_date") or "")[:4] or 0)
                score = 60 if y >= 2024 else (40 if y >= 2020 else 10)
            except ValueError:
                score = 10
        if score > best_score:
            best_score, best = score, r
    return best if best_score >= 30 else None


def enrich_film_tmdb(
    film_title: str,
    film_url: str,
    api_key: str,
    cache: Dict[str, dict],
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Fetch TMDb metadata for a film. Returns genres, rating, director, cast, poster_url, trailer_url."""
    search_title = TMDB_SEARCH_STRIP_RE.sub("", TITLE_CLEAN_RE.sub("", film_title)).strip()
    # Apply Merlin-specific title cleaning
    for pattern, replacement in MERLIN_TITLE_CLEAN:
        search_title = pattern.sub(replacement, search_title).strip()
    if not search_title:
        return {}
    # Skip TMDb enrichment for non-film live events
    tl = search_title.lower()
    if any(skip in tl for skip in MERLIN_SKIP_TMDB):
        return {}
    key = _tmdb_cache_key(film_title)

    with _tmdb_cache_lock:
        if key in cache:
            entry = cache[key]
            va = entry.get("vote_average")
            if va is not None and float(va) == 0.0:
                va = None
            return {
                "title": entry.get("title") or "",
                "overview": entry.get("overview") or "",
                "genres": entry.get("genres") or [],
                "vote_average": va,
                "director": entry.get("director") or "",
                "cast": entry.get("cast") or "",
                "poster_url": entry.get("poster_url") or "",
                "trailer_url": entry.get("trailer_url") or "",
                "imdb_id": entry.get("imdb_id") or "",
            }

    s = session or _session()
    time.sleep(TMDB_DELAY_SEC)
    # Rate-limit-aware GET helper for TMDb
    def _tmdb_get(url: str, params: dict, max_tries: int = 3) -> dict:
        for attempt in range(max_tries):
            resp = s.get(url, params=params, timeout=10)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                logger.warning("TMDb rate-limited, waiting %.1fs", retry_after)
                time.sleep(retry_after)
                continue
            if 500 <= resp.status_code < 600 and attempt < max_tries - 1:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"TMDb request failed: {url}")
    empty_result = {
        "title": "",
        "overview": "", "genres": [], "vote_average": None,
        "director": "", "cast": "",
        "poster_url": "", "poster_large_url": "", "backdrop_url": "",
        "runtime": "", "trailer_url": "", "imdb_id": "",
            "cached_at": _utc_iso_now(),
    }

    try:
        sr = _tmdb_get(
            "https://api.themoviedb.org/3/search/movie",
            {"api_key": api_key, "query": search_title, "language": "en-GB"},
        )
        results = (sr.get("results") or [])

        chosen = _pick_best_tmdb_result(results, search_title)

        # Progressive fallback: strip unknown screening suffixes word by word.
        # Merlin may add "Toddler Cinema", "Kids Club", "Special Event" etc.
        # that our known-pattern list doesn't catch. Drop up to 3 trailing
        # words and re-search until TMDb gives a solid match.
        if not chosen:
            words = search_title.split()
            max_drop = min(6, len(words) - 1)
            for drop in range(1, max_drop + 1):
                shorter = " ".join(words[:-drop]).rstrip(":,-.&")
                if not shorter or len(shorter) < 2:
                    continue
                logger.info("TMDb fallback: trying %r → %r", search_title, shorter)
                time.sleep(TMDB_DELAY_SEC)
                sr2 = _tmdb_get(
                    "https://api.themoviedb.org/3/search/movie",
                    {"api_key": api_key, "query": shorter, "language": "en-GB"},
                )
                r2 = (sr2.get("results") or [])
                candidate = _pick_best_tmdb_result(r2, shorter)
                if candidate and candidate.get("id"):
                    chosen = candidate
                    break

        if not chosen or not chosen.get("id"):
            with _tmdb_cache_lock:
                cache[key] = empty_result
            return {}

        time.sleep(TMDB_DELAY_SEC)
        movie = _tmdb_get(
            f"https://api.themoviedb.org/3/movie/{chosen['id']}",
            {"api_key": api_key, "append_to_response": "videos,credits", "language": "en-GB"},
        )

        genres = [g["name"].strip() for g in (movie.get("genres") or []) if g.get("name")]
        if not genres:
            gids = chosen.get("genre_ids") or []
            genres = [TMDB_GENRE_MAP[g] for g in gids if g in TMDB_GENRE_MAP]

        overview = (movie.get("overview") or "").strip()
        vote_average = movie.get("vote_average")
        vote_count = movie.get("vote_count") or 0

        credits = movie.get("credits") or {}
        directors = [
            c["name"].strip()
            for c in (credits.get("crew") or [])
            if (c.get("job") or "").strip() == "Director"
        ]
        director_str = ", ".join(list(dict.fromkeys(directors))[:3])
        cast_names = [
            c["name"].strip()
            for c in (credits.get("cast") or [])[:6]
            if c.get("name")
        ]
        cast_str = ", ".join(cast_names)

        poster_path = (movie.get("poster_path") or "").lstrip("/")
        poster_url = f"https://image.tmdb.org/t/p/w500/{poster_path}" if poster_path else ""
        poster_large_url = f"https://image.tmdb.org/t/p/w780/{poster_path}" if poster_path else ""

        backdrop_path = (movie.get("backdrop_path") or "").lstrip("/")
        backdrop_url = f"https://image.tmdb.org/t/p/w780/{backdrop_path}" if backdrop_path else ""

        runtime_tmdb = movie.get("runtime") or 0

        trailer_key = None
        for v in (movie.get("videos", {}).get("results") or []):
            if v.get("site") == "YouTube":
                vtype = v.get("type", "").lower()
                yt_key = v.get("key")
                if not yt_key:
                    continue
                if vtype == "trailer":
                    trailer_key = yt_key
                    break
                if vtype == "teaser" and trailer_key is None:
                    trailer_key = yt_key
        trailer_url = f"https://www.youtube.com/watch?v={trailer_key}" if trailer_key else ""

        imdb_id = movie.get("imdb_id") or ""

        result = {
            "title": _clean_display_title(movie.get("title") or chosen.get("title") or search_title),
            "overview": overview,
            "genres": genres,
            "vote_average": vote_average if vote_count > 0 else None,
            "director": director_str,
            "cast": cast_str,
            "poster_url": poster_url,
            "poster_large_url": poster_large_url,
            "backdrop_url": backdrop_url,
            "runtime": f"{runtime_tmdb} min" if runtime_tmdb > 1 else "",
            "trailer_url": trailer_url,
            "imdb_id": imdb_id,
        }
        with _tmdb_cache_lock:
            cache[key] = {**result, "cached_at": _utc_iso_now()}
        return result
    except Exception as e:
        logger.warning("TMDb enrich failed for '%s': %s", search_title, e)
        with _tmdb_cache_lock:
            cache[key] = empty_result
        return {}


# ── iCalendar output ───────────────────────────────────────────────────────────
def _format_runtime_display(runtime_str: str) -> str:
    """'119 min' -> '2h 1min'; '45 min' -> '45 min'; skip known placeholders."""
    if not runtime_str or not isinstance(runtime_str, str):
        return ""
    m = re.search(r"(\d+)", runtime_str)
    if not m:
        return runtime_str.strip()
    minutes = int(m.group(1))
    # Skip obvious placeholders (1 min is not a real runtime)
    if minutes <= 1:
        return ""
    if minutes >= 60:
        h, mins = divmod(minutes, 60)
        return f"{h}h {mins}min" if mins else f"{h}h"
    return f"{minutes} min"


def _stars_from_rating(vote_average: Any) -> str:
    """Convert 0–10 TMDb rating to 5-star unicode string."""
    if vote_average is None:
        return ""
    try:
        v = float(vote_average)
    except (TypeError, ValueError):
        return ""
    if v <= 0 or v > 10:
        return ""
    full = min(5, round(v * 0.5))
    return "★" * full + "☆" * (5 - full)


def _cast_clean(cast_str: str) -> str:
    """Reduce cast string to comma-separated names, max 6."""
    if not cast_str or not isinstance(cast_str, str):
        return ""
    # If it's already clean TMDb format (just names, no parens), return as-is
    if "(" not in cast_str:
        return cast_str if cast_str.count(",") < 6 else ", ".join(cast_str.split(",")[:6])

    names = []
    for part in cast_str.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([^(]+)(?:\s*\([^)]*\))?\s*$", part)
        if m:
            name = m.group(1).strip()
            if name and name not in names:
                names.append(name)
        elif part not in names:
            names.append(part)
        if len(names) >= 6:
            break
    return ", ".join(names[:6])


def escape_and_fold_ical_text(text: str, prefix: str = "") -> str:
    """Escape and RFC 5545 line-fold iCalendar text values."""
    escaped = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    line = prefix + escaped
    if len(line) <= ICAL_LINE_LENGTH:
        return line
    parts = [line[:ICAL_LINE_LENGTH]]
    remain = line[ICAL_LINE_LENGTH:]
    while remain:
        parts.append(" " + remain[:ICAL_LINE_LENGTH - 1])
        remain = remain[ICAL_LINE_LENGTH - 1:]
    return ICAL_NEWLINE.join(parts)


def _make_alarm(alarm: Dict[str, Any], release_date: date) -> str:
    """Generate a VALARM component. Supports days_before (absolute) and hours_before (relative)."""
    if "hours_before" in alarm:
        hours = alarm["hours_before"]
        trigger = f"PT{abs(hours)}H" if hours < 0 else f"-PT{hours}H"
        trigger_line = f"TRIGGER:{trigger}"
    else:
        t = alarm.get("time", NOTIFICATION_TIME)
        try:
            hh, mm = map(int, t.split(":"))
        except (ValueError, AttributeError):
            hh, mm = 9, 0
        days = alarm.get("days_before", 1)
        trigger_dt = datetime.combine(release_date, datetime.min.time().replace(hour=hh, minute=mm))
        trigger_dt -= timedelta(days=days)
        trigger = trigger_dt.strftime("%Y%m%dT%H%M%S")
        trigger_line = f"TRIGGER;VALUE=DATE-TIME:{trigger}"
    return ICAL_NEWLINE.join([
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{alarm.get('description', 'Film Release Reminder')}",
        trigger_line,
        "END:VALARM",
        "",
    ])


def make_ics_event(
    release_date: date,
    film_title: str,
    cinema_name: str,
    film_url: str = "",
    film_details: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a single VEVENT iCalendar block."""
    dtend = release_date + timedelta(days=1)
    uid_seed = f"{release_date.isoformat()}|{film_title}|{cinema_name}|{film_url or ''}"
    uid = f"{hashlib.sha1(uid_seed.encode()).hexdigest()}@merlin-cinemas"
    dtstamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    details = film_details or {}

    runtime_display = _format_runtime_display(details.get("runtime") or "")

    # Layout B if TMDb data present
    v = details.get("vote_average")
    has_tmdb = bool(
        details.get("genres") or details.get("overview")
        or (v is not None and isinstance(v, (int, float)))
    )

    parts = [f"{film_title} ({runtime_display})" if runtime_display else film_title]

    if has_tmdb:
        stars = _stars_from_rating(v) if v is not None else ""
        if stars:
            parts.append(stars)
        genres = details.get("genres")
        if genres:
            gs = ", ".join(str(g).strip() for g in (genres if isinstance(genres, list) else [genres]) if g)
            if gs:
                parts.append(gs)

    # Synopsis / overview
    desc_text = (details.get("overview") or details.get("synopsis") or "").strip()
    if desc_text:
        parts.append(f"\n{desc_text}")

    # Cast
    cast_raw = details.get("cast") or ""
    cast_line = _cast_clean(cast_raw)
    if cast_line:
        parts.append(f"\nStarring: {cast_line}")

    parts.append(f"\nFilm release at Merlin Cinemas {cinema_name}")
    if film_url:
        parts.append("Book tickets: " + film_url)
    film_slug = _tmdb_cache_key(film_title)
    parts.append(f"\nFilm details: https://evenwebb.github.io/merlin-cinemas/films/{film_slug}.html")

    description = "\n".join(parts)

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{release_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{dtend.strftime('%Y%m%d')}",
        escape_and_fold_ical_text(f"{film_title} @ Merlin {cinema_name}", "SUMMARY:"),
        escape_and_fold_ical_text(description, "DESCRIPTION:"),
        escape_and_fold_ical_text(f"Merlin Cinemas {cinema_name}", "LOCATION:"),
    ]
    if film_url:
        lines.append(escape_and_fold_ical_text(film_url, "URL:"))
    if NOTIFICATIONS.get("enabled") and NOTIFICATIONS.get("alarms"):
        for alarm in NOTIFICATIONS["alarms"]:
            lines.append(_make_alarm(alarm, release_date).rstrip(ICAL_NEWLINE))
    sequence = _get_ics_sequence(film_title, cinema_name, release_date, film_url)
    lines.append(f"SEQUENCE:{sequence}")
    lines.append("END:VEVENT")
    return ICAL_NEWLINE.join(lines) + ICAL_NEWLINE


# ── Dynamic SEQUENCE management (#3) ──────────────────────────────────────────
_SEQ_STATE_FILE = ".ics_sequence.json"
_seq_state_cache: Optional[Dict[str, dict]] = None


def _load_sequence_state() -> Dict[str, dict]:
    global _seq_state_cache
    if _seq_state_cache is not None:
        return _seq_state_cache
    if os.path.exists(_SEQ_STATE_FILE):
        try:
            with open(_SEQ_STATE_FILE, "r") as f:
                _seq_state_cache = json.load(f)
            return _seq_state_cache
        except (json.JSONDecodeError, OSError):
            pass
    _seq_state_cache = {}
    return _seq_state_cache


def _save_sequence_state() -> None:
    if _seq_state_cache is not None:
        Path(_SEQ_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(_SEQ_STATE_FILE, "w") as f:
            json.dump(_seq_state_cache, f, indent=2)


def _get_ics_sequence(title: str, cinema: str, release_date: date, url: str = "") -> int:
    fp = hashlib.sha256(f"{title}|{cinema}|{release_date}|{url}".encode()).hexdigest()[:16]
    state = _load_sequence_state()
    key = f"{title}|{cinema}"
    prev = state.get(key)
    if prev and prev.get("fp") == fp:
        return prev.get("seq", 0)
    seq = (prev.get("seq", 0) + 1) if prev else 0
    state[key] = {"fp": fp, "seq": seq}
    _seq_state_cache = state
    return seq


def validate_configuration() -> None:
    if NOTIFICATIONS.get("enabled") and NOTIFICATIONS.get("alarms"):
        tp = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
        if not tp.match(NOTIFICATION_TIME):
            raise ValueError(
                f"Invalid NOTIFICATION_TIME '{NOTIFICATION_TIME}'. Must be HH:MM."
            )
        for alarm in NOTIFICATIONS["alarms"]:
            if "time" in alarm and not tp.match(alarm["time"]):
                raise ValueError(
                    f"Invalid alarm time '{alarm['time']}'. Must be HH:MM."
                )
            if "days_before" not in alarm and "hours_before" not in alarm:
                raise ValueError(
                    "Each alarm must have 'days_before' or 'hours_before'."
                )

    if CACHE_EXPIRY_DAYS < 1:
        raise ValueError(f"CACHE_EXPIRY_DAYS must be >= 1, got {CACHE_EXPIRY_DAYS}")
    if not any(c["enabled"] for c in CINEMAS.values()):
        raise ValueError("At least one cinema must be enabled.")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    try:
        validate_configuration()
    except ValueError as e:
        logger.error("Config error: %s", e)
        print(f"Configuration Error: {e}")
        sys.exit(1)

    enabled_cinemas = {k: v for k, v in CINEMAS.items() if v["enabled"]}
    if not enabled_cinemas:
        print("Error: No cinemas enabled.")
        sys.exit(1)

    start_time = _utc_now()
    print(f"Scraping {len(enabled_cinemas)} cinema(s): "
          f"{', '.join(c['name'] for c in enabled_cinemas.values())}\n")

    # Load film-detail cache (shared across threads)
    cache = load_cache()
    all_films: List[Tuple] = []

    # ── Parallel cinema scraping ──────────────────────────────────────────
    def scrape_cinema(cid: str, info: dict) -> List[Tuple]:
        sess = _session()
        results = []
        try:
            films = extract_films(info["url"], info["location"], cache, session=sess)
            for f in films:
                results.append((*f, cid))
        except Exception as e:
            logger.error("Error scraping %s: %s", info["location"], e)
            print(f"✗ {info['name']}: Error - {e}")
        finally:
            sess.close()
        return results

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(scrape_cinema, cid, info): cid for cid, info in enabled_cinemas.items()}
        for fut in as_completed(futures, timeout=HTTP_TIMEOUT * 3):
            cid = futures[fut]
            try:
                films = fut.result(timeout=HTTP_TIMEOUT)
                all_films.extend(films)
                print(f"✓ {enabled_cinemas[cid]['name']}: Found {len(films)} film(s)")
            except Exception as e:
                logger.error("Thread failed for %s: %s", enabled_cinemas[cid]["name"], e)
                print(f"✗ {enabled_cinemas[cid]['name']}: Error - {e}")

    save_cache(cache)

    # ── Parallel whats-on scraping ──────────────────────────────────────────
    def scrape_whats_on(cid: str, info: dict) -> List[Dict]:
        sess = _session()
        try:
            return scrape_cinema_whats_on(cid, info["location"], session=sess)
        except Exception as e:
            logger.error("Error scraping whats-on %s: %s", info["location"], e)
            print(f"✗ {info['name']} whats-on: Error - {e}")
            return []
        finally:
            sess.close()

    whats_on_data: Dict[str, List[Dict]] = {}  # normalized_title -> [showtime dicts]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as wex:
        wfutures = {wex.submit(scrape_whats_on, cid, info): cid for cid, info in enabled_cinemas.items()}
        for fut in as_completed(wfutures, timeout=HTTP_TIMEOUT * 3):
            cid = wfutures[fut]
            try:
                wfilms = fut.result(timeout=HTTP_TIMEOUT)
                for wf in wfilms:
                    key = _normalize_title_for_match(wf["title"])
                    if key not in whats_on_data:
                        whats_on_data[key] = []
                    whats_on_data[key].append(wf)
                print(f"✓ {enabled_cinemas[cid]['name']} whats-on: Found {len(wfilms)} film(s)")
            except Exception as e:
                logger.error("Whats-on thread failed for %s: %s", enabled_cinemas[cid]["name"], e)
                print(f"✗ {enabled_cinemas[cid]['name']} whats-on: Error - {e}")

    # ── TMDb enrichment ───────────────────────────────────────────────────
    api_key = (os.environ.get("TMDB_API_KEY") or "").strip()
    tmdb_cache: Dict[str, dict] = load_tmdb_cache()
    if not api_key:
        raise RuntimeError(
            "TMDB_API_KEY is required. Movie ratings must always come from TMDb, "
            "so cache-only mode is disabled."
        )

    # Preload cached TMDb fields before refreshing them from the live API.
    for i, (rd, title, cname, furl, fdetails, cid) in enumerate(all_films):
        k = _tmdb_cache_key(title)
        if k in tmdb_cache:
            tc = tmdb_cache[k]
            fdetails = dict(fdetails)
            for field in TMDB_FIELDS:
                val = tc.get(field)
                if val and not fdetails.get(field):
                    if field == "vote_average" and float(val) == 0.0:
                        continue
                    fdetails[field] = val
            all_films[i] = (rd, title, cname, furl, fdetails, cid)

    sess = _session()
    unique_by_key: Dict[str, Tuple[str, str, List[int]]] = {}
    for i, (rd, title, cname, furl, fdetails, cid) in enumerate(all_films):
        k = _tmdb_cache_key(title)
        if k not in unique_by_key:
            unique_by_key[k] = (title, furl, [i])
        else:
            unique_by_key[k][2].append(i)

    # TMDb lookups in parallel for unique films
    def _tmdb_enrich(key: str, title: str, url: str) -> Dict[str, Any]:
        return enrich_film_tmdb(title, url, api_key, tmdb_cache, session=sess)

    enrich_futures: Dict[Any, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, MAX_WORKERS * 2)) as tex:
        for k, (title, furl, indices) in unique_by_key.items():
            enrich_futures[tex.submit(_tmdb_enrich, k, title, furl)] = k
        for fut in as_completed(enrich_futures):
            k = enrich_futures[fut]
            try:
                extra = fut.result()
            except Exception:
                extra = {}
            if not extra:
                continue
            for i in unique_by_key[k][2]:
                rd, t, cname, furl, fdetails, cid = all_films[i]
                fdetails = dict(fdetails)
                for field in TMDB_FIELDS:
                    val = extra.get(field)
                    if val or (field == "vote_average" and val is not None):
                        fdetails[field] = val
                all_films[i] = (rd, t, cname, furl, fdetails, cid)
    # Also enrich unique whats-on films not already in unique_by_key
    whats_on_unique: Dict[str, Tuple[str, str]] = {}
    for norm_title, wf_list in whats_on_data.items():
        for wf in wf_list:
            k = _tmdb_cache_key(wf["title"])
            if k not in unique_by_key and k not in whats_on_unique:
                whats_on_unique[k] = (wf["title"], wf["film_url"])

    if whats_on_unique:
        wo_enrich_futures: Dict[Any, str] = {}
        with ThreadPoolExecutor(max_workers=min(8, MAX_WORKERS * 2)) as tex:
            for k, (title, furl) in whats_on_unique.items():
                wo_enrich_futures[tex.submit(_tmdb_enrich, k, title, furl)] = k
            for fut in as_completed(wo_enrich_futures):
                k = wo_enrich_futures[fut]
                try:
                    extra = fut.result()
                except Exception:
                    extra = {}
                if extra:
                    with _tmdb_cache_lock:
                        tmdb_cache.setdefault(k, {}).update({**extra, "cached_at": _utc_iso_now()})
    sess.close()
    save_tmdb_cache(tmdb_cache)
    logger.info("TMDb enrichment done: %d coming-soon + %d whats-on unique films",
                 len(unique_by_key), len(whats_on_unique))
    if not all_films:
        logger.warning("No films found across any cinema")
        print("\nWarning: No films found across any cinema")
        sys.exit(1)

    # ── Health check (run before fingerprint so broken scrapes are always caught) ─
    if not _health_check(all_films, enabled_cinemas):
        logger.error("Health check failed - exiting before generating output")
        print("Error: Health check failed. Check cinema_log.txt for details.")
        sys.exit(1)

    # ── Fingerprint check ────────────────────────────────────────────────────
    fp = _compute_fingerprint(all_films)
    prev_fp = _load_fingerprint()
    if fp == prev_fp and not os.environ.get("FORCE_REBUILD"):
        elapsed = (_utc_now() - start_time).total_seconds()
        print(f"\nFingerprint unchanged - nothing new. ({elapsed:.1f}s)")
        return

    # Sort by date then cinema
    all_films.sort(key=lambda x: (x[0], x[2]))

    # Group by cinema
    films_by_cinema: Dict[str, List] = {}
    for f in all_films:
        films_by_cinema.setdefault(f[5], []).append(f)

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_style_css(out_dir)

    # Remove legacy .ics files without merlin- prefix
    for old in out_dir.glob("*.ics"):
        if not old.name.startswith("merlin-"):
            old.unlink()
            logger.info("Removed legacy %s", old.name)

    # Write per-cinema .ics files
    for cid in enabled_cinemas:
        cname = enabled_cinemas[cid]["name"]
        cf = films_by_cinema.get(cid, [])
        events = []
        for rd, title, cn, furl, fdetails, _ in cf:
            events.append(make_ics_event(rd, title, cn, furl, fdetails))
        header = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Merlin Cinemas//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
            "X-PUBLISHED-TTL:PT12H",
            f"X-WR-CALNAME:Merlin {cname} Movie Premieres",
            f"X-WR-CALDESC:Upcoming movie premieres at Merlin Cinemas {cname}",
        ]
        if CALENDAR_TIMEZONE.strip():
            header.append(f"X-WR-TIMEZONE:{CALENDAR_TIMEZONE.strip()}")
        ics = ICAL_NEWLINE.join(header) + ICAL_NEWLINE + "".join(events) + f"END:VCALENDAR{ICAL_NEWLINE}"
        (out_dir / f"merlin-{cid}.ics").write_text(ics, encoding="utf-8")
        logger.info("Wrote %s (%d events)", f"merlin-{cid}.ics", len(events))

    _save_sequence_state()

    # ── Release stats ─────────────────────────────────────────────────────
    today = date.today()
    unique_releases = set((f[0], f[1]) for f in all_films)
    prev_history = load_release_history()
    new_releases = unique_releases - prev_history
    # Films first seen in the last 7 days get a "New" badge
    week_ago = today - timedelta(days=7)
    new_slugs: set = set()
    for rd, title in new_releases:
        if rd >= week_ago:
            new_slugs.add(_tmdb_cache_key(title))
    release_history = prev_history | unique_releases
    save_release_history(release_history)

    past_30 = today - timedelta(days=30)
    stats = {
        "past_30_days": sum(1 for d, _ in release_history if past_30 <= d <= today),
        "ytd_past": sum(1 for d, _ in release_history if d.year == today.year and d < today),
        "this_month": sum(1 for d, _ in unique_releases if d.year == today.year and d.month == today.month and d >= today),
        "this_year": sum(1 for d, _ in unique_releases if d.year == today.year and d >= today),
        "total_upcoming": sum(1 for d, _ in unique_releases if d >= today),
    }

    # Build now-showing list from whats-on data for index page
    today = date.today()
    now_showing_films: List[Dict[str, Any]] = []
    for norm_title, wf_list in whats_on_data.items():
        all_st = []
        for wf in wf_list:
            all_st.extend(wf.get("showtimes", []))
        if not all_st:
            continue
        # Film is "now showing" if it has any showtimes this week
        min_date = min(st["date"] for st in all_st)
        max_date = max(st["date"] for st in all_st)
        has_screening = bool(wf_list[0].get("screening", ""))
        if min_date > today + timedelta(days=7) and not has_screening:
            continue
        cinemas_set = sorted(set(st.get("cinema_name", "") for st in all_st))
        slug = _tmdb_cache_key(wf_list[0]["title"])
        poster = ""
        # Check TMDb cache first (now includes whats-on enrichments)
        if slug in tmdb_cache:
            poster = tmdb_cache[slug].get("poster_url", "") or ""
        # Also check all_films detail
        if not poster:
            for rd, t, cname, furl, fdetails, cid in all_films:
                if _tmdb_cache_key(t) == slug:
                    poster = fdetails.get("poster_url", "")
                    if poster:
                        break
        # Fallback to Merlin poster from whats-on data
        if not poster:
            poster = wf_list[0].get("poster_url", "") or ""
        now_showing_films.append({
            "title": wf_list[0]["title"],
            "display_title": _clean_display_title((tmdb_cache.get(slug) or {}).get("title") or wf_list[0]["title"]),
            "slug": slug,
            "cinemas": cinemas_set,
            "showtimes": all_st,
            "min_date": min_date,
            "poster": poster,
            "screening": wf_list[0].get("screening", ""),
        })
    now_showing_films.sort(key=lambda f: (f["min_date"], f["title"]))

    # ── Special Events ─────────────────────────────────────────────────────
    special_events = [f for f in now_showing_films if f.get("screening")]
    special_events.sort(key=lambda f: (f.get("screening", ""), f["min_date"]))

    # Write index.html
    html = build_index_html(enabled_cinemas, films_by_cinema, stats=stats,
                            now_showing_live=now_showing_films,
                            special_events=special_events,
                            new_slugs=new_slugs)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    logger.info("Wrote %s/index.html", OUTPUT_DIR)

    # ── Per-film detail pages ────────────────────────────────────────────────
    films_dir = out_dir / "films"
    films_dir.mkdir(parents=True, exist_ok=True)
    # Deduplicate films by slug; collect cinemas showing each film
    film_pages: Dict[str, Dict[str, Any]] = {}
    for rd, title, cname, furl, fdetails, cid in all_films:
        slug = _tmdb_cache_key(title)
        if slug not in film_pages:
            # Copy to avoid mutating all_films entries
            merged = dict(fdetails)
            film_pages[slug] = {
                "title": title, "details": merged, "cinemas": [],
                "release_date": rd,
            }
        film_pages[slug]["cinemas"].append((cname, furl, rd, cid))
        # Merge details from entries with more data (operate on our copy)
        existing = film_pages[slug]["details"]
        for key in ("title", "overview", "poster_url", "poster_large_url", "backdrop_url", "trailer_url", "director", "cast", "runtime"):
            if not existing.get(key) and fdetails.get(key):
                existing[key] = fdetails[key]

    # Merge whats-on films into film_pages (they have showtimes but not coming-soon details)
    now_showing_entries: Dict[str, Dict[str, Any]] = {}
    for norm_title, wf_list in whats_on_data.items():
        for wf in wf_list:
            slug = _tmdb_cache_key(wf["title"])
            if slug not in now_showing_entries:
                now_showing_entries[slug] = {
                    "title": wf["title"], "slug": slug,
                    "details": {
                        "screening": wf.get("screening", ""),
                        "screening_feature": wf.get("screening_feature", ""),
                        "poster_url": wf.get("poster_url", ""),
                    },
                    "cinemas": [],
                    "release_date": date.today(),
                }
            now_showing_entries[slug]["cinemas"].append(
                (wf["cinema_name"], wf["film_url"], date.today(), wf["cinema_id"])
            )
    # Deduplicate showtimes per slug for detail pages
    showtimes_by_slug: Dict[str, List[Dict]] = {}
    for norm_title, wf_list in whats_on_data.items():
        for wf in wf_list:
            slug = _tmdb_cache_key(wf["title"])
            if slug not in showtimes_by_slug:
                showtimes_by_slug[slug] = []
            showtimes_by_slug[slug].extend(wf.get("showtimes", []))

    # Add now-showing films not already in film_pages
    for slug, entry in now_showing_entries.items():
        if slug not in film_pages:
            film_pages[slug] = entry

    # Enrich whats-on films with TMDb data from cache
    for slug, page in film_pages.items():
        if slug in tmdb_cache and not page["details"].get("overview"):
            tc = tmdb_cache[slug]
            for field in TMDB_FIELDS:
                val = tc.get(field)
                if val and not page["details"].get(field):
                    if field == "vote_average" and float(val) == 0.0:
                        continue
                    page["details"][field] = val

    for slug, page in film_pages.items():
        film_showtimes = sorted(
            showtimes_by_slug.get(slug, []),
            key=lambda s: (s["date"], s["time"], s.get("cinema_name", ""))
        )
        page_html = build_film_page(
            page["title"], slug, page["details"], page["cinemas"],
            showtimes=film_showtimes or None
        )
        (films_dir / f"{slug}.html").write_text(page_html, encoding="utf-8")
    current_film_slugs = set(film_pages.keys())
    for stale_page in films_dir.glob("*.html"):
        if stale_page.stem not in current_film_slugs:
            stale_page.unlink()
    logger.info("Wrote %d film detail pages to %s/films/", len(film_pages), OUTPUT_DIR)

    # ── Poster downloads ─────────────────────────────────────────────────────
    poster_sess = _session()
    try:
        # Build slug→pages map once for O(1) lookup
        slug_to_page: Dict[str, tuple] = {}
        for slug, page in film_pages.items():
            poster_url = page["details"].get("poster_url", "")
            if poster_url.startswith("http"):
                slug_to_page[slug] = (poster_url, page)
        if slug_to_page:
            with ThreadPoolExecutor(max_workers=min(4, MAX_WORKERS)) as pex:
                futures = {pex.submit(_download_poster, url, slug, poster_sess): slug for slug, (url, _) in slug_to_page.items()}
                updated_slugs: set = set()
                for fut in as_completed(futures):
                    slug = futures[fut]
                    try:
                        local = fut.result() or ""
                    except Exception:
                        local = ""
                    if local:
                        slug_to_page[slug][1]["details"]["poster_url"] = local
                        updated_slugs.add(slug)
            # Only rewrite film pages that got poster updates
            for slug in updated_slugs:
                page = film_pages[slug]
                film_showtimes = showtimes_by_slug.get(slug, [])
                page_html = build_film_page(page["title"], slug, page["details"], page["cinemas"],
                    showtimes=film_showtimes or None)
                (films_dir / f"{slug}.html").write_text(page_html, encoding="utf-8")
    finally:
        poster_sess.close()

    # ── Cert images ──────────────────────────────────────────────────────────
    _download_cert_images()
    _download_rating_logos()

    # ── Cinema pages ─────────────────────────────────────────────────────────
    # Build all_films_list for cinema pages (same as in build_index_html)
    _all_films_list = []
    _film_entries: Dict[str, Dict[str, Any]] = {}
    for cf in films_by_cinema.values():
        for rd, title, cname, furl, fdetails, cid in cf:
            slug = _tmdb_cache_key(title)
            if slug not in _film_entries:
                _film_entries[slug] = {
                    "title": title, "release_date": rd, "slug": slug,
                    "details": fdetails, "cinemas": {},
                }
            _film_entries[slug]["cinemas"][cname] = (furl, rd)
            if rd < _film_entries[slug]["release_date"]:
                _film_entries[slug]["release_date"] = rd
    _all_films_list = sorted(_film_entries.values(), key=lambda f: f["release_date"])
    cs_films_sorted = sorted(
        [f for f in _all_films_list if f["release_date"] > today],
        key=lambda f: f["release_date"]
    )
    for cid, info in enabled_cinemas.items():
        page_html = build_cinema_page(cid, info, now_showing_films, cs_films_sorted)
        (out_dir / f"{cid}.html").write_text(page_html, encoding="utf-8")
    logger.info("Wrote %d cinema pages", len(enabled_cinemas))

    # ── Sitemap ───────────────────────────────────────────────────────────────
    film_slugs = sorted(film_pages.keys())
    sitemap = generate_sitemap(film_slugs, list(enabled_cinemas.keys()))
    (out_dir / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    logger.info("Wrote sitemap.xml with %d URLs", len(film_slugs) + len(enabled_cinemas) + 1)

    # ── Save fingerprint ─────────────────────────────────────────────────────
    _save_fingerprint(fp)

    elapsed = (_utc_now() - start_time).total_seconds()
    print(f"\n✓ Created {OUTPUT_DIR}/ with {len(films_by_cinema)} calendar(s), {len(film_pages)} film page(s), {len(enabled_cinemas)} cinema page(s), sitemap.xml, and index page ({elapsed:.1f}s)\n")

    for d, group in groupby(all_films, key=lambda x: x[0]):
        print(f"{d.strftime('%d %B %Y')}:")
        for _, title, cname, _, _, _ in group:
            print(f"  • {title} @ {cname}")


if __name__ == "__main__":
    main()
