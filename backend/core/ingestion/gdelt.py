"""
core/ingestion/gdelt.py
========================
GDELT 2.0 Full-Text Search — historical article ingestion.

Why GDELT for history:
  - Covers 2015-01-01 to NOW with no gaps
  - Monitors 100 languages across 150+ countries
  - Free, no API key, no rate-limit (be polite: 1 req/sec)
  - startdatetime / enddatetime params let you query any window

GDELT DOC API docs:
  https://blog.gdeltproject.org/gdelt-2-0-our-global-data-feed/

Typical usage (via management command):
  backfill_gdelt_for_event(event_id=1, start="2020-01-01", end="2024-12-31")
  → slices into monthly windows, queues one Celery task per month
"""

import hashlib
import logging
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import requests
from django.db import IntegrityError

logger = logging.getLogger(__name__)

GDELT_API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT uses FIPS 10-4 country codes internally.
# This map covers the countries most relevant to geopolitical analysis.
GDELT_FIPS_TO_ISO3: dict[str, str] = {
    "AF": "AFG", "AL": "ALB", "AG": "DZA", "AN": "AGO", "AR": "ARG",
    "AS": "AUS", "AU": "AUT", "AJ": "AZE", "BF": "BHS", "BA": "BHR",
    "BG": "BGD", "BB": "BRB", "BO": "BLR", "BE": "BEL", "BH": "BLZ",
    "BN": "BEN", "BT": "BTN", "BL": "BOL", "BK": "BIH", "BC": "BWA",
    "BR": "BRA", "BX": "BRN", "BU": "BGR", "UV": "BFA", "BY": "BDI",
    "CB": "KHM", "CM": "CMR", "CA": "CAN", "CV": "CPV", "CT": "CAF",
    "CD": "TCD", "CI": "CHL", "CH": "CHN", "CO": "COL", "CN": "COM",
    "CG": "COD", "CF": "COG", "CS": "CRI", "IV": "CIV", "HR": "HRV",
    "CU": "CUB", "CY": "CYP", "EZ": "CZE", "DA": "DNK", "DJ": "DJI",
    "DR": "DOM", "EC": "ECU", "EG": "EGY", "ES": "SLV", "EK": "GNQ",
    "ER": "ERI", "EN": "EST", "ET": "ETH", "FJ": "FJI", "FI": "FIN",
    "FR": "FRA", "GB": "GAB", "GA": "GMB", "GG": "GEO", "GM": "DEU",
    "GH": "GHA", "GR": "GRC", "GT": "GTM", "GV": "GIN", "PU": "GNB",
    "GY": "GUY", "HA": "HTI", "HO": "HND", "HU": "HUN", "IC": "ISL",
    "IN": "IND", "ID": "IDN", "IR": "IRN", "IZ": "IRQ", "EI": "IRL",
    "IS": "ISR", "IT": "ITA", "JM": "JAM", "JA": "JPN", "JO": "JOR",
    "KZ": "KAZ", "KE": "KEN", "KN": "PRK", "KS": "KOR", "KU": "KWT",
    "KG": "KGZ", "LA": "LAO", "LG": "LVA", "LE": "LBN", "LT": "LSO",
    "LI": "LBR", "LY": "LBY", "LH": "LTU", "LU": "LUX", "MA": "MDG",
    "MI": "MWI", "MY": "MYS", "MV": "MDV", "ML": "MLI", "MT": "MLT",
    "RM": "MHL", "MR": "MRT", "MP": "MUS", "MX": "MEX", "FM": "FSM",
    "MD": "MDA", "MG": "MNG", "MK": "MNE", "MO": "MAR", "MZ": "MOZ",
    "BM": "MMR", "WA": "NAM", "NP": "NPL", "NL": "NLD", "NZ": "NZL",
    "NU": "NIC", "NG": "NER", "NI": "NGA", "NO": "NOR", "MU": "OMN",
    "PK": "PAK", "PM": "PAN", "PP": "PNG", "PA": "PRY", "PE": "PER",
    "RP": "PHL", "PL": "POL", "PO": "PRT", "QA": "QAT", "RO": "ROU",
    "RS": "RUS", "RW": "RWA", "SC": "SAU", "SG": "SEN", "SL": "SLE",
    "SE": "SRB", "SN": "SGP", "LO": "SVK", "SI": "SVN", "BP": "SLB",
    "SO": "SOM", "SF": "ZAF", "OD": "SSD", "CE": "LKA", "SU": "SDN",
    "NS": "SUR", "SW": "SWE", "SZ": "CHE", "SY": "SYR", "TW": "TWN",
    "TI": "TJK", "TZ": "TZA", "TH": "THA", "TT": "TLS", "TO": "TGO",
    "TN": "TON", "TD": "TTO", "TS": "TUN", "TU": "TUR", "TX": "TKM",
    "UG": "UGA", "UP": "UKR", "AE": "ARE", "UK": "GBR", "US": "USA",
    "UY": "URY", "UZ": "UZB", "NH": "VUT", "VE": "VEN", "VM": "VNM",
    "YM": "YEM", "ZA": "ZMB", "ZI": "ZWE",
}


def months_between(start: date, end: date) -> list[tuple[datetime, datetime]]:
    """
    Return a list of (start_of_month, end_of_month) datetime pairs
    covering the full range from start to end, inclusive.
    """
    windows = []
    current = start.replace(day=1)

    while current <= end:
        # Start of month
        window_start = datetime(current.year, current.month, 1, 0, 0, 0)
        # End of month (first day of next month minus 1 second)
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)
        window_end = datetime(next_month.year, next_month.month, 1, 23, 59, 59) - timedelta(days=1)
        # Cap at end date
        window_end = min(window_end, datetime(end.year, end.month, end.day, 23, 59, 59))

        windows.append((window_start, window_end))
        current = next_month

    return windows


def build_event_query(event) -> str:
    """
    Build a GDELT query string from an Event.
    Combines key terms from title + description.
    Strips common stopwords; keeps named entities and geopolitical terms.
    """
    stopwords = {
        "the", "a", "an", "of", "in", "on", "at", "to", "for",
        "and", "or", "with", "by", "from", "is", "are", "was",
        "be", "its", "their", "this", "that", "about", "over",
    }
    text = f"{event.title} {event.description}"
    words = [
        w.strip('.,;:()[]"\'')
        for w in text.split()
        if w.lower().strip('.,;:()[]"\' ') not in stopwords
        and len(w) > 2
    ]
    # GDELT handles quoted phrases; wrap multi-word title in quotes
    title_words = event.title.split()
    if len(title_words) >= 2:
        query = f'"{event.title}" ' + " ".join(words[:4])
    else:
        query = " ".join(words[:6])
    return query.strip()


def fetch_gdelt_window(
    query: str,
    window_start: datetime,
    window_end: datetime,
    max_records: int = 250,
    retry: int = 2,
) -> list[dict]:
    """
    Query GDELT DOC API for one time window.
    Returns list of article dicts (url, title, seendate, sourcecountry, …).
    Retries on failure with a 5s backoff.
    """
    params = {
        "query":         query,
        "mode":          "artlist",
        "maxrecords":    min(max_records, 250),
        "startdatetime": window_start.strftime("%Y%m%d%H%M%S"),
        "enddatetime":   window_end.strftime("%Y%m%d%H%M%S"),
        "format":        "json",
        "sourcelang":    "english",
    }
    url = f"{GDELT_API_BASE}?{urlencode(params)}"
    logger.debug("[GDELT] %s → %s | %s", window_start.date(), window_end.date(), query[:60])

    for attempt in range(retry + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("articles", [])
        except Exception as exc:
            if attempt < retry:
                logger.warning("[GDELT] Attempt %d failed (%s), retrying…", attempt + 1, exc)
                time.sleep(5)
            else:
                logger.error("[GDELT] All attempts failed: %s | %s", url, exc)
                return []
    return []


def save_articles_as_rawposts(
    articles: list[dict],
    country_lookup: dict,   # iso3 → Country
    extract_text: bool = True,
) -> tuple[int, int]:
    """
    Persist GDELT article dicts as RawPost rows.
    Returns (created, skipped) counts.

    Duplicate handling — two layers:
      1. URL normalisation: strips http/https, www., UTM params before hashing
         so the same article reached via different URLs gets the same post_id.
      2. Atomic insert: uses try/except IntegrityError instead of
         exists()+create() so concurrent Celery workers cannot both insert
         the same row — the DB unique constraint is the final arbiter.
    """
    from core.models import RawPost
    from core.utils.text import url_to_post_id

    try:
        import trafilatura
        _trafilatura_available = True
    except ImportError:
        _trafilatura_available = False
        logger.warning("trafilatura not installed; full-text extraction disabled.")

    created = skipped = 0

    for article in articles:
        url = (article.get("url") or "").strip()
        if not url:
            skipped += 1
            continue

        # Normalise URL before hashing — catches http/https, www, UTM variants
        post_id = url_to_post_id(url)

        # Resolve country
        fips = (article.get("sourcecountry") or "").strip()
        iso3 = GDELT_FIPS_TO_ISO3.get(fips)
        country = country_lookup.get(iso3) if iso3 else None
        if not country:
            skipped += 1
            continue

        # Parse date
        seendate = article.get("seendate", "")  # e.g. "20220224T120000Z"
        try:
            from django.utils import timezone
            posted_at = timezone.make_aware(
                datetime.strptime(seendate, "%Y%m%dT%H%M%SZ"),
                timezone.utc,
            )
        except Exception:
            from django.utils import timezone
            posted_at = timezone.now()

        title     = (article.get("title") or "").strip()
        full_text = ""

        # Extract full article text
        if extract_text and _trafilatura_available:
            try:
                downloaded = trafilatura.fetch_url(url)
                if downloaded:
                    full_text = trafilatura.extract(
                        downloaded,
                        include_comments=False,
                        include_tables=False,
                        no_fallback=False,
                    ) or ""
            except Exception:
                pass  # silent — title alone is still useful

        combined = f"{title}\n\n{full_text}".strip()

        try:
            RawPost.objects.create(
                country=country,
                platform="gdelt",
                account_handle="",
                post_id=post_id,
                post_text=full_text,
                combined_text=combined,
                posted_at=posted_at,
                post_url=url,
                source_url=url,
                title=title,
                language="en",
                content_type="gdelt",
                classify_ai_processed=False,
            )
            created += 1
        except IntegrityError:
            # Concurrent worker already inserted this post_id — safe to skip
            skipped += 1
        except Exception as exc:
            logger.error("[GDELT] RawPost save error: %s — %s", url, exc)
            skipped += 1

    return created, skipped
