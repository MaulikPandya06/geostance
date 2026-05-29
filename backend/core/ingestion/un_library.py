"""
core/ingestion/un_library.py
============================
Client for the UN Digital Library (UNDL) — the official UN documentation
repository, updated within days of every General Assembly vote.

Fetching strategy (two-level, tried in order)
----------------------------------------------
  1. OAI-PMH  (primary — synchronous, standard, reliable)
       URL: https://digitallibrary.un.org/oai2d
       Protocol: OAI-PMH 2.0, metadataPrefix=marcxml
       Pagination: resumptionToken (server-managed cursor)
       Chunking: monthly date windows to avoid 503/server-error on large ranges
       Retry: exponential back-off (up to 5 attempts) for 5xx / HTML error responses
       Why: The UNDL search API (/search?cc=Voting+Data) is behind AWS WAF
            and always returns HTTP 202 with a JS challenge page for Python
            requests.  OAI-PMH is synchronous and not WAF-guarded.

  2. Search API with 202-retry  (fallback — kept for completeness)
       URL: https://digitallibrary.un.org/search?of=xm&cc=Voting+Data
       Note: This will almost certainly keep returning 202 (WAF-blocked).

Data flow
---------
  search_resolutions_by_year(year)
      → OAI-PMH harvest (date-filtered)
      → parse MARC21 XML per record
      → filter to adopted resolutions only (089$b ∈ {B01, B04, B06, B08} and "RES/" in symbol)
      → yield ResolutionRecord

  fetch_country_votes(undl_id)
      → HTML scrape of https://digitallibrary.un.org/record/{id}
      → parse "In favour / Against / Abstaining / Non-participating" sections
      → return {iso3_code: vote_str}

MARC21 fields (verified against live UNDL records)
--------------------------------------------------
  001   — UNDL control number (undl_id)
  089$b — Record-type code.  VERIFIED against the live OAI feed (2026 probe):
           B01 = ADOPTED RESOLUTION for *every* body — GA, SC and HRC final
                 resolutions all carry B01 (e.g. "S/RES/2812 (2026)" → B01,
                 "A/HRC/RES/61/6" → B01).  The body (UNGA/UNSC/UNHRC) is derived
                 from the 191$a symbol prefix, NOT from the b-code.
           B02 = draft (e.g. "S/2026/23")          ✗ skipped
           B03 = decision / verbatim meeting record ✗ skipped
           B15/B16/B18 = letters, reports, notes    ✗ skipped
           B04/B06/B08 may appear on report/letter documents (NOT resolutions),
                 so the b-code whitelist is only a coarse pre-filter — the real
                 gate is _is_resolution_record(), which requires "RES/" in 191$a.
  191$a — UN document symbol  e.g. "A/RES/79/1", "S/RES/2728", "A/RES/ES-11/1"
           ← THE authoritative resolution test: must contain "RES/".
  191$c — GA session number
  245$a — Resolution title
  992$a — Actual vote date  YYYY-MM-DD  (use this, NOT 269$a which is pub date)
  269$a — Document publication date  (fallback only)
  520$a — UNDL abstract: concise summary → short_description
  995$a — Resolution action summary / explanation of the vote  ← resolution_text
  650$a — Subject keywords (topic tags)  (NOT 610$a!)
  996$a — Vote result string e.g. "Adopted 109-48-7, 83rd meeting"
  967   — NOT present in UNDL OAI records; vote totals come from 996$a

Vote codes produced
-------------------
  yes | no | abstain | absent | not_member
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Generator
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── UNDL endpoints ──────────────────────────────────────────────────────────
# Primary: OAI-PMH — the standard library metadata harvesting protocol
OAI_BASE   = "https://digitallibrary.un.org/oai2d"
OAI_NS     = "http://www.openarchives.org/OAI/2.0/"

# Fallback: direct search with MARC21 XML output
UNDL_SEARCH = "https://digitallibrary.un.org/search"

# Individual record HTML page (for country vote scraping)
UNDL_RECORD = "https://digitallibrary.un.org/record/{id}"

MARC21_NS   = "http://www.loc.gov/MARC21/slim"

# OAI-PMH: candidate set names for the "Voting Data" collection.
# Tried in order until one works; result is cached in _VOTING_SET_CACHE.
_OAI_SET_CANDIDATES = [
    "col:Voting+Data",
    "Voting+Data",
    "voting_data",
    "VotingData",
]
_VOTING_SET_CACHE: str | None = None   # stores confirmed set name after discovery

# Search-API fallback settings
PAGE_SIZE        = 25
MAX_POLL_RETRIES = 8      # max retries when server returns 202
POLL_DELAY       = 4.0    # seconds between 202-retry attempts

# OAI-PMH request settings
REQUEST_DELAY    = 3.0    # polite delay between OAI-PMH pagination page fetches
OAI_MAX_RETRIES  = 5      # retries for transient 5xx / connection errors
OAI_RETRY_BASE   = 15.0   # minimum base seconds for back-off (15, 30, 60, 120, 240)
# NOTE: the actual wait on 503 is max(server Retry-After, OAI_RETRY_BASE * 2^(attempt-1))
# UNDL often returns Retry-After: 1 or 2, which is too short — we always enforce our minimum.

HEADERS = {
    "User-Agent": (
        "GeoStance-Research-Bot/1.0 "
        "(geopolitical analysis research; contact: geostance@gmail.com)"
    ),
    "Accept": "application/xml, text/xml, */*",
}

# Shared session — persists cookies across requests (important for search fallback)
_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """
    Return (or lazily create) a shared requests.Session.
    On first call, warms up UNDL session cookies with a lightweight GET.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(HEADERS)
        try:
            _SESSION.get("https://digitallibrary.un.org/", timeout=15)
        except Exception:
            pass   # warmup failure is non-fatal
    return _SESSION

# ── MARC21 967$d vote code → internal vote string ────────────────────────────
# Per-country votes live in UNDL "Voting Data" records (MARC type B23), NOT on
# the resolution record page. Field 967 subfields:
#   $a = sequence number   $b = member type (P=permanent, R=non-permanent, E=elected)
#   $c = ISO-3 country code   $d = vote code   $e = country name (English)
VOTE_D_MAP: dict[str, str] = {
    "Y": "yes",
    "N": "no",
    "A": "abstain",
    "X": "absent",   # non-participating / non-voting member
    "0": "absent",   # some older records use "0" for absent
}

# URL for UNDL Voting Data collection search (year-faceted, WAF-bypassed via Playwright)
UNDL_VOTING_DATA_YEAR_URL = (
    "https://digitallibrary.un.org/search?"
    "ln=en&p=&f=&rm=&sf=year&so=d&rg=100"
    "&c=Voting+Data&of=hb&fti=0"
    "&fct__3={year}&fti=0"
)

# ── Legacy HTML scraping map (kept for reference — no longer used) ────────────
_VOTE_HEADING_MAP = {
    "in favour":       "yes",
    "for":             "yes",
    "yes":             "yes",
    "against":         "no",
    "no":              "no",
    "abstaining":      "abstain",
    "abstentions":     "abstain",
    "abstain":         "abstain",
    "non-participating": "absent",
    "non-participants":  "absent",
    "absent":            "absent",
    "did not participate": "absent",
    "not member":      "not_member",
    "non-members":     "not_member",
}

# ── Country name → ISO-A3 mapping ───────────────────────────────────────────
# Covers all current UN member states plus common historical name variants.
COUNTRY_NAME_TO_ISO3: dict[str, str] = {
    "Afghanistan": "AFG", "Albania": "ALB", "Algeria": "DZA",
    "Andorra": "AND", "Angola": "AGO", "Antigua and Barbuda": "ATG",
    "Argentina": "ARG", "Armenia": "ARM", "Australia": "AUS",
    "Austria": "AUT", "Azerbaijan": "AZE", "Bahamas": "BHS",
    "Bahrain": "BHR", "Bangladesh": "BGD", "Barbados": "BRB",
    "Belarus": "BLR", "Belgium": "BEL", "Belize": "BLZ",
    "Benin": "BEN", "Bhutan": "BTN", "Bolivia": "BOL",
    "Bolivia (Plurinational State of)": "BOL",
    "Bosnia and Herzegovina": "BIH", "Botswana": "BWA", "Brazil": "BRA",
    "Brunei Darussalam": "BRN", "Brunei": "BRN",
    "Bulgaria": "BGR", "Burkina Faso": "BFA", "Burundi": "BDI",
    "Cabo Verde": "CPV", "Cape Verde": "CPV",
    "Cambodia": "KHM", "Cameroon": "CMR", "Canada": "CAN",
    "Central African Republic": "CAF", "Chad": "TCD", "Chile": "CHL",
    "China": "CHN", "Colombia": "COL", "Comoros": "COM",
    "Congo": "COG",
    "Democratic Republic of the Congo": "COD",
    "Democratic Republic of Congo": "COD",
    "Cook Islands": "COK", "Costa Rica": "CRI",
    "Côte d'Ivoire": "CIV", "Cote d'Ivoire": "CIV", "Ivory Coast": "CIV",
    "Croatia": "HRV", "Cuba": "CUB", "Cyprus": "CYP",
    "Czechia": "CZE", "Czech Republic": "CZE",
    "Denmark": "DNK", "Djibouti": "DJI", "Dominica": "DMA",
    "Dominican Republic": "DOM", "Ecuador": "ECU", "Egypt": "EGY",
    "El Salvador": "SLV", "Equatorial Guinea": "GNQ", "Eritrea": "ERI",
    "Estonia": "EST", "Eswatini": "SWZ", "Swaziland": "SWZ",
    "Ethiopia": "ETH", "Fiji": "FJI", "Finland": "FIN",
    "France": "FRA", "Gabon": "GAB", "Gambia": "GMB",
    "Georgia": "GEO", "Germany": "DEU", "Ghana": "GHA",
    "Greece": "GRC", "Grenada": "GRD", "Guatemala": "GTM",
    "Guinea": "GIN", "Guinea-Bissau": "GNB", "Guyana": "GUY",
    "Haiti": "HTI", "Honduras": "HND", "Hungary": "HUN",
    "Iceland": "ISL", "India": "IND", "Indonesia": "IDN",
    "Iran": "IRN", "Iran (Islamic Republic of)": "IRN",
    "Islamic Republic of Iran": "IRN",
    "Iraq": "IRQ", "Ireland": "IRL", "Israel": "ISR",
    "Italy": "ITA", "Jamaica": "JAM", "Japan": "JPN",
    "Jordan": "JOR", "Kazakhstan": "KAZ", "Kenya": "KEN",
    "Kiribati": "KIR",
    "Democratic People's Republic of Korea": "PRK", "DPRK": "PRK", "North Korea": "PRK",
    "Republic of Korea": "KOR", "South Korea": "KOR", "Korea": "KOR",
    "Kuwait": "KWT", "Kyrgyzstan": "KGZ",
    "Lao People's Democratic Republic": "LAO", "Laos": "LAO",
    "Latvia": "LVA", "Lebanon": "LBN", "Lesotho": "LSO",
    "Liberia": "LBR", "Libya": "LBY",
    "Libyan Arab Jamahiriya": "LBY",
    "Liechtenstein": "LIE", "Lithuania": "LTU", "Luxembourg": "LUX",
    "Madagascar": "MDG", "Malawi": "MWI", "Malaysia": "MYS",
    "Maldives": "MDV", "Mali": "MLI", "Malta": "MLT",
    "Marshall Islands": "MHL", "Mauritania": "MRT", "Mauritius": "MUS",
    "Mexico": "MEX",
    "Micronesia (Federated States of)": "FSM", "Micronesia": "FSM",
    "Moldova": "MDA", "Republic of Moldova": "MDA",
    "Monaco": "MCO", "Mongolia": "MNG", "Montenegro": "MNE",
    "Morocco": "MAR", "Mozambique": "MOZ", "Myanmar": "MMR", "Burma": "MMR",
    "Namibia": "NAM", "Nauru": "NRU", "Nepal": "NPL",
    "Netherlands": "NLD", "New Zealand": "NZL", "Nicaragua": "NIC",
    "Niger": "NER", "Nigeria": "NGA",
    "North Macedonia": "MKD", "Macedonia": "MKD",
    "Norway": "NOR", "Oman": "OMN", "Pakistan": "PAK",
    "Palau": "PLW", "Panama": "PAN",
    "Papua New Guinea": "PNG", "Paraguay": "PRY", "Peru": "PER",
    "Philippines": "PHL", "Poland": "POL", "Portugal": "PRT",
    "Qatar": "QAT", "Romania": "ROU",
    "Russian Federation": "RUS", "Russia": "RUS",
    "Rwanda": "RWA", "Saint Kitts and Nevis": "KNA",
    "Saint Lucia": "LCA", "Saint Vincent and the Grenadines": "VCT",
    "Samoa": "WSM", "San Marino": "SMR",
    "Sao Tome and Principe": "STP", "Saudi Arabia": "SAU",
    "Senegal": "SEN", "Serbia": "SRB", "Seychelles": "SYC",
    "Sierra Leone": "SLE", "Singapore": "SGP",
    "Slovakia": "SVK", "Slovenia": "SVN",
    "Solomon Islands": "SLB", "Somalia": "SOM",
    "South Africa": "ZAF", "South Sudan": "SSD",
    "Spain": "ESP", "Sri Lanka": "LKA", "Sudan": "SDN",
    "Suriname": "SUR", "Sweden": "SWE", "Switzerland": "CHE",
    "Syrian Arab Republic": "SYR", "Syria": "SYR",
    "Tajikistan": "TJK",
    "Tanzania": "TZA", "United Republic of Tanzania": "TZA",
    "Thailand": "THA", "Timor-Leste": "TLS", "East Timor": "TLS",
    "Togo": "TGO", "Tonga": "TON",
    "Trinidad and Tobago": "TTO", "Tunisia": "TUN",
    "Türkiye": "TUR", "Turkey": "TUR",
    "Turkmenistan": "TKM", "Tuvalu": "TUV", "Uganda": "UGA",
    "Ukraine": "UKR",
    "United Arab Emirates": "ARE",
    "United Kingdom": "GBR",
    "United Kingdom of Great Britain and Northern Ireland": "GBR",
    "United States": "USA",
    "United States of America": "USA",
    "Uruguay": "URY", "Uzbekistan": "UZB",
    "Vanuatu": "VUT",
    "Venezuela": "VEN",
    "Venezuela (Bolivarian Republic of)": "VEN",
    "Viet Nam": "VNM", "Vietnam": "VNM",
    "Yemen": "YEM", "Zambia": "ZMB", "Zimbabwe": "ZWE",
    # Historical / observer
    "Kosovo": "XKX",
    "Palestine": "PSE", "State of Palestine": "PSE",
    "Holy See": "VAT", "Vatican": "VAT",
}


# ── Data container ───────────────────────────────────────────────────────────

@dataclass
class ResolutionRecord:
    undl_id:               str
    un_symbol:             str
    title:                 str
    vote_date:             date | None
    session:               int | None
    body:                  str                 # "UNGA" | "UNSC" | "UNHRC"
    short_description:     str                 # 520$a — UNDL abstract / concise summary
    resolution_text:       str                 # 995$a — resolution action summary / explanation of vote
    topic_tags:            list[str]           # 650$a + 610$a subject keywords
    meeting_record_symbol: str = ""            # 993$a — meeting verbatim record e.g. "S/PV.10089"
    votes_yes:             int = 0             # parsed from 996$a "Adopted Y-N-A, Nth meeting"
    votes_no:              int = 0
    votes_abstain:         int = 0
    votes_absent:          int = 0
    # country_votes populated lazily by fetch_country_votes()
    country_votes:         dict[str, str] = field(default_factory=dict)


# ── Public API ───────────────────────────────────────────────────────────────

def search_resolutions(
    from_date: str,
    until_date: str | None = None,
    use_voting_set: bool = False,
) -> Generator[ResolutionRecord, None, None]:
    """
    Yield ResolutionRecord objects for all UNDL voting records whose OAI
    datestamp falls between `from_date` and `until_date` (inclusive).

    Dates must be ISO-format strings: "YYYY-MM-DD".
    `until_date` defaults to today.

    use_voting_set
    --------------
    When True (default), harvest is restricted to the UNDL "Voting Data"
    OAI-PMH set — only resolutions with an actual recorded roll-call vote
    appear there.  Resolutions adopted by consensus (unanimous, no roll-call)
    live in the broader UNDL catalog and are invisible to this set.

    Set use_voting_set=False to query ALL UNDL records and rely solely on the
    089$b B-code whitelist (B01/B04/B06/B08) to identify resolutions.  This
    is slower (more records to parse) but finds every resolution regardless of
    whether a roll-call vote was held.

    Why date ranges don't map 1-to-1 to vote years
    ------------------------------------------------
    OAI-PMH filters by the *record's datestamp in the UNDL catalog* — i.e.,
    when UNDL staff indexed or last modified the document — NOT by the
    resolution's vote date.  For example, GA resolutions voted in Nov–Dec 2024
    are typically processed and indexed by UNDL in Jan–Feb 2025, so they carry
    a 2025 OAI datestamp.

    The correct approach for a multi-year backfill is therefore to request ONE
    continuous range (e.g. 2010-01-01 → today) and group results by the
    vote_date stored in the MARC21 record rather than by the OAI datestamp.
    """
    if until_date is None:
        until_date = date.today().isoformat()

    count = 0
    try:
        for rec in _oai_harvest_range(from_date, until_date, use_voting_set=use_voting_set):
            count += 1
            yield rec
        logger.info(
            "UNDL OAI-PMH: %s→%s yielded %d records", from_date, until_date, count
        )
        if count > 0:
            return
    except Exception as exc:
        logger.warning(
            "UNDL OAI-PMH failed (%s→%s, %s) — trying search-API fallback",
            from_date, until_date, exc,
        )

    if count > 0:
        return

    # Fallback: search API (WAF-blocked in most network environments)
    logger.info("UNDL search-API fallback (%s→%s)", from_date, until_date)
    try:
        year = int(from_date[:4])
    except ValueError:
        return
    jrec = 1
    total_known = None
    while True:
        records, total = _search_page_with_retry(year, jrec)
        if total_known is None:
            total_known = total
            logger.info("UNDL search-API: %d total records", total_known)
        for rec in records:
            yield rec
        jrec += len(records)
        if not records or (total_known and jrec > total_known):
            break
        time.sleep(REQUEST_DELAY)


def search_resolutions_by_year(year: int) -> Generator[ResolutionRecord, None, None]:
    """
    Convenience wrapper: yield records whose OAI datestamp is in `year`.

    NOTE: Due to UNDL's indexing lag, resolutions *voted* in year N often
    carry datestamps in year N+1.  For a reliable per-vote-year backfill,
    use the management command which calls search_resolutions() over the full
    range and groups results by rec.vote_date rather than by OAI datestamp.
    This wrapper is used by the daily Celery task where a short lookback window
    (14 days) is sufficient to catch newly indexed records.
    """
    yield from search_resolutions(
        from_date=f"{year}-01-01",
        until_date=f"{year}-12-31",
    )


# ── Website-based discovery (Playwright) ─────────────────────────────────────
# The UNDL website uses a Solr index that is updated faster than OAI-PMH.
# New resolutions can appear on the website days before their OAI datestamp
# is updated. The functions below use Playwright to discover record IDs from
# the website year-facet search, then fetch full MARC21 via OAI-PMH GetRecord.
#
# This is the RELIABLE path for year-specific imports because:
#   - The website year filter (fct__3=YYYY) uses the MARC21 992$a vote date
#   - OAI-PMH ListRecords uses catalog datestamps which don't match vote dates
#   - A/RES/80/253, S/RES/2820, etc. appear on website but not OAI ListRecords

# Real browser user-agent — UNDL WAF blocks bot UAs even through Playwright.
_PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# UNDL search URL with year facet — returns all resolutions for a given year.
# rg=100: fetch up to 100 per page (UNDL max); jrec=N: 1-based offset for pagination.
# A typical year has 200–400 resolutions; most individual months have < 100.
UNDL_SEARCH_YEAR_URL = (
    "https://digitallibrary.un.org/search?"
    "ln=en&p=RES&f=&rm=&sf=year&so=d&rg=100"
    "&c=Resolutions%20and%20Decisions&c=&of=hb&fti=0"
    "&fct__3={year}&fti=0"
)


def discover_undl_ids_by_year(year: int) -> list[str]:
    """
    Scrape the UNDL website year-facet search page (using Playwright) and
    return all UNDL record IDs (undl_ids) for the given year.

    Uses Playwright because the UNDL search page is JavaScript-rendered.
    The plain HTTP search API (of=xm, of=recjson) is WAF-blocked for
    non-browser user agents.

    Returns a deduplicated list of numeric undl_id strings, e.g. ["4110295", ...].
    Returns an empty list if Playwright is unavailable or the page fails to load.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.error(
            "discover_undl_ids_by_year: playwright not installed. "
            "Run: playwright install chromium"
        )
        return []

    base_url = UNDL_SEARCH_YEAR_URL.format(year=year)
    seen: set[str] = set()
    all_ids: list[str] = []

    logger.info("UNDL Playwright discovery: year=%d", year)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_PLAYWRIGHT_UA,
                locale="en-US",
            )
            page = ctx.new_page()

            jrec = 1
            total_records: int | None = None
            while True:
                url = base_url + (f"&jrec={jrec}" if jrec > 1 else "")
                logger.info("UNDL Playwright: GET year=%d jrec=%d", year, jrec)

                try:
                    # Use domcontentloaded — networkidle can hang on pages with
                    # background polling; domcontentloaded fires once HTML+CSS parsed.
                    page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                    # Give JS-rendered content up to 10s to populate record links
                    try:
                        page.wait_for_selector('a[href*="/record/"]', timeout=10_000)
                    except PwTimeout:
                        pass  # fall through to query_selector_all; may be empty
                except PwTimeout:
                    logger.warning(
                        "UNDL Playwright: page timeout (year=%d jrec=%d)", year, jrec
                    )
                    break
                except Exception as nav_exc:
                    logger.warning(
                        "UNDL Playwright: navigation error jrec=%d: %s", jrec, nav_exc
                    )
                    break

                # Extract all record IDs from href="/record/{id}" links
                page_ids: list[str] = []
                links = page.query_selector_all('a[href*="/record/"]')
                logger.info(
                    "UNDL Playwright: jrec=%d found %d raw links", jrec, len(links)
                )
                for link in links:
                    href = link.get_attribute("href") or ""
                    # href examples: /record/4110295  /record/4110295/files/...
                    parts = href.split("/record/")
                    if len(parts) < 2:
                        continue
                    candidate = parts[1].split("/")[0].split("?")[0].strip()
                    if candidate.isdigit() and candidate not in seen:
                        seen.add(candidate)
                        page_ids.append(candidate)

                if not page_ids:
                    logger.info(
                        "UNDL Playwright: no new IDs on jrec=%d — done", jrec
                    )
                    break

                all_ids.extend(page_ids)
                logger.info(
                    "UNDL Playwright: jrec=%d found %d IDs (running total=%d)",
                    jrec, len(page_ids), len(all_ids),
                )

                # Read total record count from page text (e.g. "59 records found")
                # to decide whether to fetch another page.
                if total_records is None:
                    body_text = page.inner_text("body")
                    m = re.search(r"(\d+)\s+records?\s+found", body_text, re.I)
                    if m:
                        total_records = int(m.group(1))
                        logger.info(
                            "UNDL Playwright: total_records=%d for year=%d",
                            total_records, year,
                        )

                # No more pages if we've collected enough IDs or no total found
                if total_records is None or len(all_ids) >= total_records:
                    break
                jrec += 100

            browser.close()

    except Exception as exc:
        logger.error("UNDL Playwright discovery failed: %s", exc)

    logger.info(
        "UNDL Playwright discovery: year=%d total_ids=%d", year, len(all_ids)
    )
    return all_ids


def fetch_record_by_oai_id(undl_id: str) -> "ResolutionRecord | None":
    """
    Fetch a single UNDL record by ID.

    Strategy (two-level, tried in order):
      1. OAI-PMH GetRecord — structured, includes datestamp, but has a lag of
         days to weeks for newly published records.
      2. MARC21 export endpoint (/record/{id}/export/xm) — serves the same
         MARC21 data without WAF restrictions and is available immediately after
         the record is published on the website.  Used when OAI-PMH returns no
         record (idDoesNotExist or empty response for newly indexed records).

    Returns a ResolutionRecord if the record is a valid resolution (has RES/ in
    its symbol), or None if the record cannot be fetched or is not a resolution.
    """
    rec = _fetch_via_oai_getrecord(undl_id)
    if rec is not None:
        return rec

    # OAI-PMH didn't have it — fall back to direct MARC21 export
    logger.info(
        "fetch_record_by_oai_id: OAI-PMH missed undl_id=%s — trying export endpoint",
        undl_id,
    )
    return _fetch_via_marc_export(undl_id)


def _fetch_via_oai_getrecord(undl_id: str) -> "ResolutionRecord | None":
    """OAI-PMH GetRecord path for fetch_record_by_oai_id."""
    identifier = f"oai:digitallibrary.un.org:{undl_id}"
    params = {
        "verb": "GetRecord",
        "metadataPrefix": "marcxml",
        "identifier": identifier,
    }

    resp = _oai_get_with_retry(_get_session(), params)
    if resp is None:
        return None

    ns_oai = {"oai": OAI_NS}
    ns_marc = {"m": MARC21_NS}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        logger.warning("OAI GetRecord XML parse error undl_id=%s: %s", undl_id, exc)
        return None

    error_el = root.find("oai:error", ns_oai)
    if error_el is not None:
        # idDoesNotExist is expected for newly published records not yet in OAI
        code = error_el.get("code", "unknown")
        if code not in ("idDoesNotExist", "noRecordsMatch"):
            logger.warning("OAI GetRecord undl_id=%s error: %s", undl_id, code)
        return None

    record_el = root.find(".//oai:record", ns_oai)
    if record_el is None:
        return None

    header = record_el.find("oai:header", ns_oai)
    if header is not None and header.get("status") == "deleted":
        return None

    datestamp = ""
    if header is not None:
        ds_el = header.find("oai:datestamp", ns_oai)
        if ds_el is not None and ds_el.text:
            datestamp = ds_el.text.strip()

    metadata_el = record_el.find("oai:metadata", ns_oai)
    if metadata_el is None:
        return None

    marc_el = (
        metadata_el.find(f"{{{MARC21_NS}}}record")
        or metadata_el.find("record")
    )
    if marc_el is None:
        return None

    if not marc_el.tag.startswith("{"):
        marc_el = _inject_ns(marc_el)

    rec = _parse_marc_record(marc_el, ns_marc, oai_datestamp=datestamp)
    if rec is None or not _is_resolution_record(rec):
        return None

    return rec


def _fetch_via_marc_export(undl_id: str) -> "ResolutionRecord | None":
    """
    Fallback: fetch MARC21 via the UNDL individual-record export endpoint.

    URL: https://digitallibrary.un.org/record/{id}/export/xm
    Returns a plain MARC21 <collection> XML document — no OAI-PMH wrapper.
    Available immediately for newly published records; not WAF-blocked.
    """
    url = f"https://digitallibrary.un.org/record/{undl_id}/export/xm"
    try:
        resp = _get_session().get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("MARC export fetch failed undl_id=%s: %s", undl_id, exc)
        return None

    xml_text = resp.text.strip()
    if not xml_text or "<collection" not in xml_text:
        logger.warning("MARC export: unexpected response for undl_id=%s", undl_id)
        return None

    # Inject MARC21 namespace if absent (export endpoint sometimes omits it)
    if MARC21_NS not in xml_text:
        xml_text = xml_text.replace(
            "<collection", f'<collection xmlns="{MARC21_NS}"', 1
        ).replace("<record>", f'<record xmlns="{MARC21_NS}">')

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("MARC export XML parse error undl_id=%s: %s", undl_id, exc)
        return None

    ns_marc = {"m": MARC21_NS}
    # Export returns a <collection> with one <record>
    marc_el = root.find(f"{{{MARC21_NS}}}record") or root.find("record")
    if marc_el is None:
        # Try as the root element itself
        marc_el = root if "record" in root.tag else None
    if marc_el is None:
        return None

    if not marc_el.tag.startswith("{"):
        marc_el = _inject_ns(marc_el)

    rec = _parse_marc_record(marc_el, ns_marc)
    if rec is None or not _is_resolution_record(rec):
        return None

    # Export has no OAI datestamp — ensure undl_id is set from URL
    if not rec.undl_id:
        rec.undl_id = undl_id

    logger.info(
        "MARC export: fetched undl_id=%s sym=%r date=%s",
        undl_id, rec.un_symbol, rec.vote_date,
    )
    return rec


def discover_voting_data_ids_by_year(year: int) -> list[str]:
    """
    Playwright search of the UNDL Voting Data collection for a given year.
    Returns a list of undl_ids for all Voting Data records (B23 type) in that year.

    Voting Data records are separate from Resolution records — they live in the
    "Voting Data" collection and contain per-country votes in MARC field 967.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.error("discover_voting_data_ids_by_year: playwright not installed")
        return []

    base_url = UNDL_VOTING_DATA_YEAR_URL.format(year=year)
    seen: set[str] = set()
    all_ids: list[str] = []
    total_records: int | None = None

    logger.info("UNDL Voting Data discovery: year=%d", year)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_PLAYWRIGHT_UA, locale="en-US")
            page = ctx.new_page()

            jrec = 1
            while True:
                url = base_url + (f"&jrec={jrec}" if jrec > 1 else "")
                logger.info("UNDL Voting Data: GET year=%d jrec=%d", year, jrec)

                try:
                    page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector('a[href*="/record/"]', timeout=10_000)
                    except PwTimeout:
                        pass
                except PwTimeout:
                    logger.warning("UNDL Voting Data: timeout year=%d jrec=%d", year, jrec)
                    break
                except Exception as nav_exc:
                    logger.warning("UNDL Voting Data: nav error jrec=%d: %s", jrec, nav_exc)
                    break

                links = page.query_selector_all('a[href*="/record/"]')
                page_ids: list[str] = []
                for link in links:
                    href = link.get_attribute("href") or ""
                    parts = href.split("/record/")
                    if len(parts) >= 2:
                        rid = parts[1].split("/")[0].split("?")[0].strip()
                        if rid.isdigit() and rid not in seen:
                            seen.add(rid)
                            page_ids.append(rid)

                if not page_ids:
                    break

                all_ids.extend(page_ids)

                if total_records is None:
                    body_text = page.inner_text("body")
                    m = re.search(r"(\d+)\s+records?\s+found", body_text, re.I)
                    if m:
                        total_records = int(m.group(1))
                        logger.info(
                            "UNDL Voting Data: total=%d for year=%d",
                            total_records, year,
                        )

                if total_records is None or len(all_ids) >= total_records:
                    break
                jrec += 100

            browser.close()

    except Exception as exc:
        logger.error("UNDL Voting Data discovery failed: %s", exc)

    logger.info(
        "UNDL Voting Data discovery: year=%d total_ids=%d", year, len(all_ids)
    )
    return all_ids


def fetch_votes_for_year(year: int) -> dict[str, dict[str, str]]:
    """
    Return all per-country votes for every resolution voted on in `year`.

    Uses Playwright to discover all Voting Data record IDs for the year, then
    fetches each record's MARC21 export (plain HTTP, no WAF issue) and parses
    field 967 for per-country votes.

    Returns:
        {un_symbol: {iso3_code: vote_str}}
        e.g. {"A/RES/ES-11/10": {"RUS": "no", "CHN": "abstain", "USA": "yes", ...}}

    Resolutions adopted without a vote (consensus) have no Voting Data record
    and will not appear in the returned dict.
    """
    voting_ids = discover_voting_data_ids_by_year(year)
    if not voting_ids:
        logger.warning("fetch_votes_for_year(%d): no voting data IDs found", year)
        return {}

    logger.info(
        "fetch_votes_for_year(%d): fetching MARC21 for %d voting records",
        year, len(voting_ids),
    )

    result: dict[str, dict[str, str]] = {}
    ns = {"m": MARC21_NS}

    for voting_id in voting_ids:
        xml = _fetch_marc_export_raw(voting_id)
        if not xml:
            logger.debug("fetch_votes_for_year: export failed for id=%s", voting_id)
            continue

        # Inject namespace
        if MARC21_NS not in xml:
            xml = xml.replace(
                "<collection", f'<collection xmlns="{MARC21_NS}"', 1
            ).replace("<record>", f'<record xmlns="{MARC21_NS}">', 1)

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue

        # Extract linked resolution symbol from 791$a
        sym_el = None
        for df in root.findall(".//m:datafield[@tag='791']", ns):
            sf = df.find("m:subfield[@code='a']", ns)
            if sf is not None and sf.text:
                sym_el = sf.text.strip()
                break

        if not sym_el:
            continue

        symbol = normalize_un_symbol(sym_el)
        votes = _parse_marc967_votes(xml)

        if votes:
            result[symbol] = votes
            logger.debug(
                "fetch_votes_for_year: %r → %d votes (id=%s)",
                symbol, len(votes), voting_id,
            )
        time.sleep(0.5)   # polite — plain HTTP, no OAI rate limits

    logger.info(
        "fetch_votes_for_year(%d): %d resolutions with vote data",
        year, len(result),
    )
    return result


def search_resolutions_by_year_website(
    year: int,
) -> Generator[ResolutionRecord, None, None]:
    """
    Yield ResolutionRecords for all resolutions in `year` using website-based
    discovery (Playwright) + OAI-PMH GetRecord for MARC21 metadata.

    This is MORE RELIABLE than search_resolutions_by_year() because:
    - Discovery uses the UNDL website Solr index, which is updated immediately
    - OAI-PMH ListRecords can miss resolutions whose datestamp predates the year
      (e.g. a draft record updated to a resolution keeps its old OAI datestamp)
    - New resolutions appear on the website days before OAI-PMH reflects them

    Requires playwright to be installed: pip install playwright && playwright install chromium

    Skips records where GetRecord fails (OAI not yet updated) — callers should
    re-run after a few hours if completeness is critical.
    """
    undl_ids = discover_undl_ids_by_year(year)
    if not undl_ids:
        logger.warning(
            "search_resolutions_by_year_website: no IDs discovered for year=%d "
            "(Playwright unavailable or website unreachable)",
            year,
        )
        return

    logger.info(
        "search_resolutions_by_year_website: fetching MARC21 for %d IDs (year=%d)",
        len(undl_ids), year,
    )
    fetched = skipped = wrong_year = 0

    for undl_id in undl_ids:
        rec = fetch_record_by_oai_id(undl_id)
        time.sleep(REQUEST_DELAY)

        if rec is None:
            skipped += 1
            logger.debug(
                "website-discovery: skipped undl_id=%s "
                "(GetRecord failed or not a resolution)",
                undl_id,
            )
            continue

        # Year guard: the website might return adjacent-year records for
        # resolutions that span sessions (e.g. A/RES/ES-11 emergency sessions).
        if rec.vote_date and rec.vote_date.year != year:
            wrong_year += 1
            logger.debug(
                "website-discovery: undl_id=%s sym=%r vote_date=%s not in year=%d — skip",
                undl_id, rec.un_symbol, rec.vote_date, year,
            )
            continue

        fetched += 1
        yield rec

    logger.info(
        "website-discovery year=%d: fetched=%d skipped=%d wrong_year=%d",
        year, fetched, skipped, wrong_year,
    )


def fetch_country_votes(undl_id: str, un_symbol: str = "") -> dict[str, str]:
    """
    Return per-country votes for a resolution by fetching its linked Voting Data
    record from UNDL (MARC type B23, field 967).

    The per-country breakdown is NOT on the resolution record page — it lives in
    a separate "Voting Data" record linked by un_symbol.  We find that record by
    searching the UNDL Voting Data collection via Playwright, then fetch its
    MARC21 export and parse field 967:
        967$c = ISO-3 country code
        967$d = Y (yes) | N (no) | A (abstain) | X (absent/non-voting)

    Returns an empty dict for consensus resolutions (no Voting Data record exists).
    """
    if not un_symbol:
        logger.debug("fetch_country_votes: no symbol for undl_id=%s — skipping", undl_id)
        return {}

    voting_id = _find_voting_record_id_by_symbol(un_symbol)
    if not voting_id:
        logger.debug(
            "fetch_country_votes: no Voting Data record for %r — likely consensus",
            un_symbol,
        )
        return {}

    xml = _fetch_marc_export_raw(voting_id)
    if not xml:
        logger.warning(
            "fetch_country_votes: MARC export failed for voting record %s (sym=%r)",
            voting_id, un_symbol,
        )
        return {}

    votes = _parse_marc967_votes(xml)
    logger.info(
        "fetch_country_votes: %r → voting_id=%s → %d country votes",
        un_symbol, voting_id, len(votes),
    )
    return votes


def _find_voting_record_id_by_symbol(un_symbol: str) -> str | None:
    """
    Use Playwright to search the UNDL Voting Data collection by resolution symbol
    and return the undl_id of the matching Voting Data record (B23 type).

    Returns None if no matching record is found (consensus resolution) or if
    Playwright is unavailable.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.warning("_find_voting_record_id_by_symbol: playwright not installed")
        return None

    import urllib.parse
    encoded = urllib.parse.quote(un_symbol, safe="")
    url = (
        f"https://digitallibrary.un.org/search?"
        f"p={encoded}&c=Voting+Data&of=hb&rg=5&ln=en"
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_PLAYWRIGHT_UA, locale="en-US")
            page = ctx.new_page()
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('a[href*="/record/"]', timeout=8_000)
            except PwTimeout:
                pass

            links = page.query_selector_all('a[href*="/record/"]')
            seen: set[str] = set()
            for link in links:
                href = link.get_attribute("href") or ""
                parts = href.split("/record/")
                if len(parts) >= 2:
                    rid = parts[1].split("/")[0].split("?")[0].strip()
                    if rid.isdigit():
                        seen.add(rid)

            browser.close()

            # If multiple IDs found, verify by checking 791$a in MARC
            for rid in seen:
                xml = _fetch_marc_export_raw(rid)
                if xml and un_symbol.replace(" ", "").upper() in xml.replace(" ", "").upper():
                    return rid

            return next(iter(seen)) if len(seen) == 1 else None

    except Exception as exc:
        logger.warning(
            "_find_voting_record_id_by_symbol(%r): %s", un_symbol, exc
        )
        return None


def _fetch_marc_export_raw(undl_id: str) -> str | None:
    """
    Fetch raw MARC21 XML via /record/{id}/export/xm (no WAF, no JS needed).
    Returns the XML string or None on failure.
    """
    url = f"https://digitallibrary.un.org/record/{undl_id}/export/xm"
    try:
        resp = _get_session().get(url, timeout=20)
        if not resp.ok:
            return None
        return resp.text
    except Exception as exc:
        logger.warning("_fetch_marc_export_raw(%s): %s", undl_id, exc)
        return None


def _parse_marc967_votes(xml_text: str) -> dict[str, str]:
    """
    Parse MARC21 field 967 from a Voting Data record XML and return
    {iso3_code: vote_string} for every country that cast a vote.

    Field 967 subfields:
        $c = ISO-3 country code (uppercase, e.g. "CHN")
        $d = vote code: Y=yes, N=no, A=abstain, X=absent/non-voting
    """
    # Inject namespace if absent
    if MARC21_NS not in xml_text:
        xml_text = xml_text.replace(
            "<collection", f'<collection xmlns="{MARC21_NS}"', 1
        ).replace("<record>", f'<record xmlns="{MARC21_NS}">', 1)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("_parse_marc967_votes XML error: %s", exc)
        return {}

    ns = {"m": MARC21_NS}
    votes: dict[str, str] = {}

    for df in root.findall(".//m:datafield[@tag='967']", ns):
        iso3_el = df.find("m:subfield[@code='c']", ns)
        vote_el  = df.find("m:subfield[@code='d']", ns)
        if iso3_el is None or vote_el is None:
            continue
        iso3      = (iso3_el.text or "").strip().upper()
        vote_code = (vote_el.text or "").strip().upper()
        vote_str  = VOTE_D_MAP.get(vote_code)
        if iso3 and vote_str:
            votes[iso3] = vote_str

    return votes


def normalize_un_symbol(symbol: str) -> str:
    """
    Normalize a UN document symbol to a consistent form.
    e.g. "A/RES/77/20 " → "A/RES/77/20"
    """
    return symbol.strip().upper().replace("  ", " ")


# ── OAI-PMH harvester (primary) ───────────────────────────────────────────────

def _monthly_windows(from_date: str, until_date: str) -> list[tuple[str, str]]:
    """
    Split [from_date, until_date] into sequential 1-month windows.

    Using monthly (not quarterly) windows keeps each OAI-PMH request small
    enough that UNDL does not return 503. A 3-month window can contain
    hundreds of records and regularly triggers rate-limiting.

    Examples
    --------
    "2024-11-01" → "2025-02-28"  yields:
        ("2024-11-01", "2024-11-30")
        ("2024-12-01", "2024-12-31")
        ("2025-01-01", "2025-01-31")
        ("2025-02-01", "2025-02-28")
    """
    from datetime import timedelta
    import calendar

    def add_months(d: date, months: int) -> date:
        month = d.month - 1 + months
        year  = d.year + month // 12
        month = month % 12 + 1
        day   = min(d.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    start  = date.fromisoformat(from_date)
    end    = date.fromisoformat(until_date)
    windows: list[tuple[str, str]] = []

    cursor = start
    while cursor <= end:
        window_end = min(add_months(cursor, 1) - timedelta(days=1), end)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)

    return windows


# Keep old name as alias so any external callers don't break
_quarterly_windows = _monthly_windows


def _oai_harvest_range(
    from_date: str,
    until_date: str,
    use_voting_set: bool = False,
) -> Generator[ResolutionRecord, None, None]:
    """
    Harvest all OAI-PMH records with datestamps in [from_date, until_date].

    Automatically splits the range into monthly windows to keep each
    individual request small (the UNDL server returns 503 / HTML error pages
    for large date spans).  Each window retries with exponential back-off.

    Deduplicates by undl_id across all windows so callers never receive
    the same physical record twice even if UNDL date-range boundaries overlap.

    use_voting_set=False: skips the "Voting Data" OAI set restriction and
    queries ALL UNDL records.  The B-code whitelist (B01/B04/B06/B08) is still
    applied, so only actual resolution types are returned.  Use this when
    resolutions adopted by consensus (no roll-call vote) are needed.
    """
    set_name  = _discover_voting_set() if use_voting_set else None
    session   = _get_session()
    today_iso = date.today().isoformat()

    logger.info(
        "OAI-PMH harvest %s→%s | set=%r | use_voting_set=%s",
        from_date, until_date, set_name, use_voting_set,
    )

    quarters  = _monthly_windows(from_date, until_date)
    seen_undl_ids: set[str] = set()

    for q_from, q_until in quarters:
        # Don't request dates in the future
        if q_from > today_iso:
            break

        params: dict = {
            "verb":           "ListRecords",
            "metadataPrefix": "marcxml",
            "from":           q_from,
            "until":          q_until,
        }
        if set_name:
            params["set"] = set_name

        logger.debug("OAI-PMH quarter %s → %s", q_from, q_until)
        page = 0

        while True:
            resp = _oai_get_with_retry(session, params)
            if resp is None:
                logger.warning(
                    "OAI-PMH: giving up on month %s→%s after %d retries — "
                    "records in this window will be missing",
                    q_from, q_until, OAI_MAX_RETRIES,
                )
                break

            records, token = _parse_oai_response(resp.text, q_from, q_until)
            new_this_page = 0
            for rec in records:
                key = rec.undl_id or rec.un_symbol
                if key and key in seen_undl_ids:
                    continue   # duplicate across monthly window boundaries
                if key:
                    seen_undl_ids.add(key)
                yield rec
                new_this_page += 1

            logger.info(
                "OAI-PMH %s→%s page=%d  parsed=%d  new=%d  token=%s",
                q_from, q_until, page, len(records), new_this_page,
                "yes" if token else "no",
            )
            page += 1

            if not token:
                break

            params = {"verb": "ListRecords", "resumptionToken": token}
            time.sleep(REQUEST_DELAY)

        # Brief pause between quarters to be polite
        time.sleep(REQUEST_DELAY)


def _oai_get_with_retry(
    session: requests.Session, params: dict
) -> requests.Response | None:
    """
    GET the OAI-PMH endpoint with exponential back-off retry.

    Retries on:
      - Connection / timeout errors
      - HTTP 429 (rate-limited)
      - HTTP 5xx (server errors including 503)
      - HTTP 200 but body is an HTML error page (server returned HTML instead of XML)

    Returns the Response on success, or None if all retries are exhausted.
    """
    for attempt in range(1, OAI_MAX_RETRIES + 1):
        try:
            resp = session.get(OAI_BASE, params=params, timeout=90)
        except requests.RequestException as exc:
            wait = OAI_RETRY_BASE * (2 ** (attempt - 1))
            logger.warning(
                "OAI-PMH connection error (attempt %d/%d): %s — retrying in %.0fs",
                attempt, OAI_MAX_RETRIES, exc, wait,
            )
            if attempt == OAI_MAX_RETRIES:
                return None
            time.sleep(wait)
            continue

        # Transient server errors
        if resp.status_code in (429, 500, 502, 503, 504):
            our_backoff  = OAI_RETRY_BASE * (2 ** (attempt - 1))
            server_hint  = float(resp.headers.get("Retry-After", 0))
            # Always wait at least our exponential backoff — UNDL often returns
            # Retry-After: 1 or 2 which is far too short and causes repeated 503s.
            wait = max(server_hint, our_backoff)
            logger.warning(
                "OAI-PMH HTTP %d (attempt %d/%d) — retrying in %.0fs "
                "(server hint=%.0fs, our min=%.0fs)",
                resp.status_code, attempt, OAI_MAX_RETRIES, wait, server_hint, our_backoff,
            )
            if attempt == OAI_MAX_RETRIES:
                return None
            time.sleep(wait)
            continue

        # Non-retryable HTTP error (4xx other than 429)
        if not resp.ok:
            logger.error("OAI-PMH non-retryable HTTP %d", resp.status_code)
            return None

        # HTTP 200 but body is an HTML error page, not XML
        body = resp.text.strip()
        if (body and not body.startswith("<")) or (body.startswith("<") and "<html" in body[:200].lower()):
            wait = OAI_RETRY_BASE * (2 ** (attempt - 1))
            logger.warning(
                "OAI-PMH: server returned HTML instead of XML (attempt %d/%d) "
                "— snippet: %r — retrying in %.0fs",
                attempt, OAI_MAX_RETRIES, body[:120], wait,
            )
            if attempt == OAI_MAX_RETRIES:
                return None
            time.sleep(wait)
            continue

        return resp  # success

    return None


def _discover_voting_set() -> str | None:
    """
    Call OAI-PMH ListSets to find the set name for the Voting Data collection.
    Result is cached in _VOTING_SET_CACHE after first successful call.
    Returns None if no voting-data set is found (caller should omit set param).
    """
    global _VOTING_SET_CACHE
    if _VOTING_SET_CACHE is not None:
        return _VOTING_SET_CACHE if _VOTING_SET_CACHE != "__none__" else None

    try:
        resp = _get_session().get(
            OAI_BASE, params={"verb": "ListSets"}, timeout=30
        )
        resp.raise_for_status()
        text = resp.text

        # Look for a setSpec that matches any of our candidates (case-insensitive)
        oai_ns = OAI_NS
        root = ET.fromstring(text)
        ns = {"oai": oai_ns}
        for set_el in root.findall(".//oai:set", ns):
            spec_el = set_el.find("oai:setSpec", ns)
            name_el = set_el.find("oai:setName", ns)
            spec = spec_el.text.strip() if spec_el is not None and spec_el.text else ""
            name = name_el.text.strip() if name_el is not None and name_el.text else ""
            combined = f"{spec} {name}".lower()
            if "voting" in combined or "vote" in combined:
                logger.info("OAI-PMH: found voting set: %r (%r)", spec, name)
                _VOTING_SET_CACHE = spec
                return spec
    except Exception as exc:
        logger.warning("OAI-PMH ListSets failed: %s — will harvest without set filter", exc)

    # No matching set found — harvest without set (slower but works)
    _VOTING_SET_CACHE = "__none__"
    return None


def _is_resolution_record(rec: "ResolutionRecord") -> bool:
    """
    Return True if this record is a UN resolution that must be saved.

    Rule (per project requirement): ANY record whose UN document symbol
    contains "RES/" is a resolution and is persisted — regardless of whether a
    roll-call vote was held.  Consensus / unanimous adoptions (peacekeeping
    mandate renewals, sanctions extensions, ceremonial GA resolutions, etc.)
    have a "RES/" symbol but no recorded country votes, and they count.

    Symbols covered: A/RES/…, S/RES/…, A/RES/ES-…, A/HRC/RES/…, E/RES/…

    The 089$b B-code whitelist (B01/B04/B06/B08) in _parse_marc_record already
    removes decisions, drafts and verbatim records upstream; this is the final
    symbol gate that drops anything still lacking a resolution symbol
    (presidential statements S/PRST/…, letters, reports, verbatim records).
    """
    return "RES/" in (rec.un_symbol or "")


def _parse_oai_response(
    xml_text: str,
    q_from: str = "",
    q_until: str = "",
) -> tuple[list[ResolutionRecord], str | None]:
    """
    Parse an OAI-PMH ListRecords XML response.

    Returns (list_of_ResolutionRecord, resumption_token_or_None).
    The resumption token is used to fetch the next page.

    Raises RuntimeError for unexpected OAI error codes so the caller can decide
    whether to retry.  Returns ([], None) only for noRecordsMatch (normal/expected).
    """
    if not xml_text or not xml_text.strip():
        logger.warning("OAI-PMH: empty response body (%s→%s)", q_from, q_until)
        return [], None

    # Guard against HTML error pages that slipped past _oai_get_with_retry
    if "<html" in xml_text[:500].lower():
        logger.error(
            "OAI-PMH: received HTML instead of XML (%s→%s) | snippet: %r",
            q_from, q_until, xml_text[:200],
        )
        return [], None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error(
            "OAI-PMH XML parse error (%s→%s): %s | snippet: %r",
            q_from, q_until, exc, xml_text[:300],
        )
        return [], None

    ns_oai  = {"oai":  OAI_NS}
    ns_marc = {"m": MARC21_NS}

    # Check for OAI error element
    error_el = root.find("oai:error", ns_oai)
    if error_el is not None:
        code = error_el.get("code", "unknown")
        msg  = (error_el.text or "").strip()
        if code == "noRecordsMatch":
            logger.info("OAI-PMH: noRecordsMatch (no voting records in date range)")
            return [], None
        raise RuntimeError(f"OAI-PMH error {code}: {msg}")

    records: list[ResolutionRecord] = []
    n_total = n_accepted = n_marc_rejected = n_symbol_rejected = 0

    for record_el in root.findall(".//oai:record", ns_oai):
        # Skip deleted records
        header = record_el.find("oai:header", ns_oai)
        if header is not None and header.get("status") == "deleted":
            continue

        # Extract OAI datestamp (catalog-indexing date) for vote_date fallback
        datestamp = ""
        if header is not None:
            ds_el = header.find("oai:datestamp", ns_oai)
            if ds_el is not None and ds_el.text:
                datestamp = ds_el.text.strip()

        # The MARC21 record is nested inside <metadata>
        metadata_el = record_el.find("oai:metadata", ns_oai)
        if metadata_el is None:
            continue

        # MARC21 <record> element — may have its own namespace or inherit
        marc_el = (
            metadata_el.find(f"{{{MARC21_NS}}}record")
            or metadata_el.find("record")
        )
        if marc_el is None:
            continue

        # Ensure namespace prefix works for _parse_marc_record
        if not marc_el.tag.startswith("{"):
            # Inject namespace on the element and all children
            marc_el = _inject_ns(marc_el)

        n_total += 1
        rec = _parse_marc_record(marc_el, ns_marc, oai_datestamp=datestamp)
        if rec is None:
            n_marc_rejected += 1
            continue
        if not _is_resolution_record(rec):
            logger.debug(
                "OAI-PMH: skip non-resolution undl_id=%s sym=%r (no RES/ in symbol)",
                rec.undl_id, rec.un_symbol,
            )
            n_symbol_rejected += 1
            continue
        n_accepted += 1
        records.append(rec)

    logger.info(
        "OAI-PMH parsed %s→%s: total_raw=%d accepted=%d "
        "bcode_rejected=%d symbol_rejected=%d",
        q_from, q_until,
        n_total, n_accepted, n_marc_rejected, n_symbol_rejected,
    )

    # Resumption token for next page
    token_el = root.find(".//oai:resumptionToken", ns_oai)
    token = None
    if token_el is not None and token_el.text and token_el.text.strip():
        token = token_el.text.strip()

    return records, token


def _inject_ns(element: ET.Element) -> ET.Element:
    """
    Return a copy of `element` with the MARC21 namespace injected on every tag.
    Needed when the OAI-PMH response omits the xmlns on the inner <record>.
    """
    new_el = ET.Element(f"{{{MARC21_NS}}}{element.tag}", element.attrib)
    new_el.text = element.text
    new_el.tail = element.tail
    for child in element:
        new_el.append(_inject_ns(child))
    return new_el


# ── Search API fallback (handles 202 Accepted via polling) ────────────────────

def _search_page_with_retry(year: int, jrec: int) -> tuple[list[ResolutionRecord], int]:
    """
    Fetch one page from the UNDL search API with MARC21 XML output.

    The UNDL search API sometimes returns HTTP 202 Accepted with an empty body
    (asynchronous processing model).  This function retries up to MAX_POLL_RETRIES
    times, waiting POLL_DELAY seconds each time, honouring Retry-After headers.
    """
    params = {
        "cc": "Voting Data",
        "of": "xm",
        "p": f"year:{year}",
        "rg": PAGE_SIZE,
        "jrec": jrec,
        "action_search": "Search",
        "sf": "date",
        "so": "d",
    }
    session = _get_session()

    for attempt in range(1, MAX_POLL_RETRIES + 1):
        try:
            resp = session.get(UNDL_SEARCH, params=params, timeout=60)
        except requests.RequestException as exc:
            logger.error("UNDL search request failed: %s", exc)
            return [], 0

        if resp.status_code == 200 and resp.text.strip():
            return _parse_marc_xml(resp.text)

        if resp.status_code == 202:
            wait = float(resp.headers.get("Retry-After", POLL_DELAY))
            logger.debug(
                "UNDL search: 202 Accepted (attempt %d/%d) — waiting %.1fs",
                attempt, MAX_POLL_RETRIES, wait,
            )
            time.sleep(wait)
            continue

        # Non-200/202 — log and bail
        logger.error(
            "UNDL search returned unexpected status %d for year=%d jrec=%d",
            resp.status_code, year, jrec,
        )
        return [], 0

    logger.error(
        "UNDL search: still getting 202 after %d retries (year=%d jrec=%d)",
        MAX_POLL_RETRIES, year, jrec,
    )
    return [], 0


def _parse_marc_xml(xml_text: str) -> tuple[list[ResolutionRecord], int]:
    """
    Parse a bare MARC21 XML <collection> document returned by the search API.
    Returns (records, total_hits).  Used by the search-API fallback path only.
    """
    text = xml_text.strip()
    if not text:
        return [], 0

    # Inject namespace if absent so XPath works consistently
    if MARC21_NS not in text:
        text = text.replace("<collection", f'<collection xmlns="{MARC21_NS}"', 1)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        logger.error("MARC21 XML parse error: %s | snippet: %r", exc, text[:300])
        return [], 0

    ns = {"m": MARC21_NS}
    try:
        total = int(root.get("total", "0"))
    except ValueError:
        total = 0

    records = []
    for record_el in root.findall("m:record", ns):
        rec = _parse_marc_record(record_el, ns)
        if rec:
            records.append(rec)

    if total == 0:
        total = len(records)
    return records, total


def _get_subfield(record_el, ns: dict, tag: str, code: str) -> str:
    """Return first matching subfield text, or ''."""
    for df in record_el.findall(f"m:datafield[@tag='{tag}']", ns):
        sf = df.find(f"m:subfield[@code='{code}']", ns)
        if sf is not None and sf.text:
            return sf.text.strip()
    return ""


def _get_all_subfields(record_el, ns: dict, tag: str, code: str) -> list[str]:
    """Return all subfield text values for a given field+code."""
    results = []
    for df in record_el.findall(f"m:datafield[@tag='{tag}']", ns):
        for sf in df.findall(f"m:subfield[@code='{code}']", ns):
            if sf.text and sf.text.strip():
                results.append(sf.text.strip())
    return results


def _parse_marc_record(
    record_el,
    ns: dict,
    oai_datestamp: str = "",
) -> "ResolutionRecord | None":
    """
    Extract a ResolutionRecord from a single MARC21 <record> element.
    Returns None for draft documents (089$b = "B02") and non-resolution
    document types (089$b starts with "A" — verbatim records, press releases).
    Accepts all "B" codes: B01 (GA res), B03 (GA dec), B04 (SC res),
    B05 (SC dec), B06 (ECOSOC res), B08 (HRC res), etc.

    Field mapping (verified against live UNDL OAI-PMH records):
      001   → undl_id
      089$b → coarse record-type pre-filter (B01 = adopted resolution for ALL
              bodies; B02/B03/B15/B16/B18 = drafts/decisions/letters skipped).
              The authoritative resolution test is the 191$a "RES/" symbol gate.
      191$a → un_symbol
      191$c → session
      245$a → title
      992$a → vote_date  (actual vote date; 269$a is publication date)
      269$a → vote_date fallback
      520$a → short_description  (UNDL abstract — concise summary)
      995$a → resolution_text  (action summary / "explanation of the vote")
      650$a → topic_tags  (subject keywords — NOT 610$a)
      996$a → vote totals string e.g. "Adopted 109-48-7, 83rd meeting"
    """
    # 001 — control number (UNDL ID)
    cf = record_el.find("m:controlfield[@tag='001']", ns)
    undl_id = cf.text.strip() if cf is not None and cf.text else ""

    # 089$b — UNDL record-type code.  VERIFIED against the live 2026 OAI feed:
    #   B01 = ADOPTED RESOLUTION — for EVERY body.  GA, SC and HRC final
    #         resolutions all carry B01 (e.g. "S/RES/2812 (2026)" → B01,
    #         "A/HRC/RES/61/6" → B01).  B04/B06/B08 are NOT reliable body
    #         markers — they turn up on report/letter documents — so they
    #         stay in the whitelist only as a harmless coarse pre-filter.
    #
    #   B02 = draft (e.g. "S/2026/23")           ← SKIP
    #   B03 = decision / verbatim meeting record  ← SKIP
    #   B15/B16/B18 = letters, reports, notes     ← SKIP
    #
    # This b-code check is only a COARSE pre-filter.  The authoritative gate is
    # _is_resolution_record(), which requires "RES/" in the 191$a symbol — that
    # is what actually distinguishes an adopted resolution from its draft/letter.
    # If 089$b is absent we keep the record and let the symbol gate decide.
    _RESOLUTION_CODES = {"B01", "B04", "B06", "B08"}
    # A single MARC record may carry MULTIPLE 089 fields or $b subfields.
    # Collect ALL values and accept if ANY is a resolution code.
    rec_types = {s.upper() for s in _get_all_subfields(record_el, ns, "089", "b") if s}
    if rec_types:
        if not (rec_types & _RESOLUTION_CODES):
            logger.debug(
                "OAI-PMH: skip undl_id=%s — b-codes=%s not in resolution whitelist",
                undl_id, rec_types,
            )
            return None   # decisions, drafts, verbatim records, etc.

    # 191$a — UN document symbol
    un_symbol = normalize_un_symbol(_get_subfield(record_el, ns, "191", "a"))
    if not un_symbol and not undl_id:
        return None

    # 191$c — GA session
    session_str = _get_subfield(record_el, ns, "191", "c")
    try:
        session = int(session_str) if session_str else None
    except ValueError:
        session = None

    # 245$a — title
    title = _get_subfield(record_el, ns, "245", "a").rstrip("/").strip()

    # 992$a — actual vote date (preferred); 269$a = publication date (fallback);
    # oai_datestamp = OAI-PMH catalog indexing date (last-resort fallback so we
    # never drop a valid resolution just because MARC date fields are missing).
    date_raw = _get_subfield(record_el, ns, "992", "a")
    vote_date = _parse_undl_date(date_raw)
    if not vote_date:
        date_raw = _get_subfield(record_el, ns, "269", "a")
        vote_date = _parse_undl_date(date_raw)
    if not vote_date and oai_datestamp:
        vote_date = _parse_undl_date(oai_datestamp)
        if vote_date:
            logger.debug(
                "OAI-PMH: using OAI datestamp %s as vote_date fallback for undl_id=%s",
                oai_datestamp, undl_id,
            )

    # 520$a — UNDL abstract: concise summary of what the resolution is about.
    # Used as short_description (human-readable one-paragraph summary).
    short_description = _get_subfield(record_el, ns, "520", "a")

    # 995$a — resolution action summary ("explanation of the vote").
    # Falls back to 520$a when 995$a is absent (some older records omit 995).
    resolution_text = (
        _get_subfield(record_el, ns, "995", "a")
        or short_description   # reuse 520$a if 995$a is absent
    )

    # 650$a — subject keywords (topic tags)
    # Note: 610$a is used for "named subjects" (org names), NOT subject keywords.
    topic_tags = _get_all_subfields(record_el, ns, "650", "a")
    # Also include 610$a for org-level subjects (e.g. "SANCTIONS")
    topic_tags += _get_all_subfields(record_el, ns, "610", "a")
    # Deduplicate while preserving order
    seen_tags: set[str] = set()
    unique_tags: list[str] = []
    for t in topic_tags:
        tu = t.upper()
        if tu not in seen_tags:
            seen_tags.add(tu)
            unique_tags.append(t)
    topic_tags = unique_tags

    # Determine body from un_symbol prefix
    body = _body_from_symbol(un_symbol)

    # 993$a — cross-references: draft symbol AND meeting verbatim record symbol
    # A single record may have multiple 993 fields; pick the one that looks like
    # a verbatim record (S/PV.NNNNN or A/NN/PV.NN).
    meeting_record_symbol = ""
    for ref in _get_all_subfields(record_el, ns, "993", "a"):
        ref = ref.strip()
        if re.match(r"[A-Z]/PV\.\d+|S/PV\.\d+|A/\d+/PV\.\d+", ref, re.I):
            meeting_record_symbol = ref
            break

    # 996$a — vote result string e.g. "Adopted 109-48-7, 83rd meeting"
    # Parse into integer totals.  Format: "Adopted Y-N-A, Nth meeting"
    votes_yes = votes_no = votes_abstain = votes_absent = 0
    vote_result_str = _get_subfield(record_el, ns, "996", "a")
    if vote_result_str:
        m = re.search(r"(\d+)-(\d+)-(\d+)", vote_result_str)
        if m:
            votes_yes     = int(m.group(1))
            votes_no      = int(m.group(2))
            votes_abstain = int(m.group(3))

    return ResolutionRecord(
        undl_id=undl_id,
        un_symbol=un_symbol,
        title=title,
        vote_date=vote_date,
        session=session,
        body=body,
        short_description=short_description,
        resolution_text=resolution_text,
        topic_tags=topic_tags,
        meeting_record_symbol=meeting_record_symbol,
        votes_yes=votes_yes,
        votes_no=votes_no,
        votes_abstain=votes_abstain,
        votes_absent=votes_absent,
    )


# ── Press release scraper — UN Meetings Coverage ─────────────────────────────
#
# UN Meetings Coverage (press.un.org) publishes structured press releases for
# most Security Council and significant General Assembly votes. Each release
# contains per-country paragraphs attributing statements to specific delegates.
#
# Data pipeline:
#   UNResolution.meeting_record_symbol  (e.g. "S/PV.10089")  MARC 993$a
#       → fetch PV record from UNDL → 993$a press release code "SC/16274"
#       → https://press.un.org/en/{year}/{sc|ga}{number}.doc.htm
#       → per-country paragraphs → UNVote.explanation
#
# Coverage:
#   UNSC  ~95 %  — every contested vote has Meetings Coverage
#   UNGA  ~60 %  — major contested votes; procedural/consensus skipped
#   UNHRC ~30 %  — selective

# press.un.org URL template. Body prefix: 'sc' | 'ga' | 'ecosoc' etc.
PRESS_UN_ORG_URL = "https://press.un.org/en/{year}/{prefix}{number}.doc.htm"


def fetch_press_release_code(meeting_record_symbol: str) -> str | None:
    """
    Given a meeting verbatim record symbol (e.g. "S/PV.10089"), find the
    corresponding UN Meetings Coverage press release code (e.g. "SC/16274").

    Strategy:
      1. Search UNDL for the PV record by symbol using Playwright.
      2. Fetch its MARC21 export via plain HTTP.
      3. Return the 993$a value that looks like a press-release code (SC/... GA/...).

    Returns None if the PV record cannot be found or has no press release linked.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.warning("fetch_press_release_code: playwright not installed")
        return None

    import urllib.parse
    encoded = urllib.parse.quote(meeting_record_symbol, safe="")
    search_url = (
        f"https://digitallibrary.un.org/search?"
        f"p={encoded}&of=hb&rg=5&ln=en"
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_PLAYWRIGHT_UA, locale="en-US")
            page = ctx.new_page()
            page.goto(search_url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('a[href*="/record/"]', timeout=8_000)
            except PwTimeout:
                pass

            links = page.query_selector_all('a[href*="/record/"]')
            pv_ids: set[str] = set()
            for link in links:
                href = link.get_attribute("href") or ""
                parts = href.split("/record/")
                if len(parts) >= 2:
                    rid = parts[1].split("/")[0].split("?")[0].strip()
                    if rid.isdigit():
                        pv_ids.add(rid)
            browser.close()
    except Exception as exc:
        logger.warning("fetch_press_release_code(%r): Playwright error: %s", meeting_record_symbol, exc)
        return None

    # Check each candidate for the press release code in 993$a
    for pv_id in pv_ids:
        xml = _fetch_marc_export_raw(pv_id)
        if not xml:
            continue

        if MARC21_NS not in xml:
            xml = xml.replace("<collection", f'<collection xmlns="{MARC21_NS}"', 1)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue

        ns = {"m": MARC21_NS}

        # Verify this record IS the right PV record (191$a matches symbol)
        sym_el = root.find(".//m:datafield[@tag='191']/m:subfield[@code='a']", ns)
        if sym_el is not None and sym_el.text:
            if meeting_record_symbol.upper() not in sym_el.text.upper():
                continue

        # Find the press release code in 993$a (format: SC/NNNNN or GA/NNNNN)
        for df in root.findall(".//m:datafield[@tag='993']", ns):
            sf = df.find("m:subfield[@code='a']", ns)
            if sf is None or not sf.text:
                continue
            val = sf.text.strip()
            if re.match(r"^(SC|GA|DSG|SG|ECOSOC|HR)/\d+$", val, re.I):
                logger.info(
                    "fetch_press_release_code(%r): found %r in undl_id=%s",
                    meeting_record_symbol, val, pv_id,
                )
                return val

    logger.debug(
        "fetch_press_release_code(%r): no press release code found", meeting_record_symbol
    )
    return None


def fetch_vote_explanations(
    press_release_code: str,
    vote_year: int,
) -> dict[str, str]:
    """
    Scrape the UN Meetings Coverage press release and return per-country
    explanation of vote statements.

    Args:
        press_release_code: e.g. "SC/16274" (from UNResolution.press_release_code)
        vote_year: the year of the vote (used in the URL)

    Returns:
        {iso3_code: explanation_text}
        e.g. {"RUS": "We cannot support this document...", "CHN": "..."}

    Uses Playwright because press.un.org renders content via JavaScript.
    Only countries that spoke on the vote are included.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.warning("fetch_vote_explanations: playwright not installed")
        return {}

    # Build press.un.org URL: SC/16274 → sc16274
    parts = press_release_code.split("/")
    if len(parts) != 2:
        logger.warning("fetch_vote_explanations: unexpected code format %r", press_release_code)
        return {}
    prefix = parts[0].lower()
    number = parts[1]
    url = PRESS_UN_ORG_URL.format(year=vote_year, prefix=prefix, number=number)

    logger.info("fetch_vote_explanations: fetching %s", url)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_PLAYWRIGHT_UA, locale="en-US")
            page = ctx.new_page()
            try:
                page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                page.wait_for_timeout(2_000)
            except PwTimeout:
                logger.warning("fetch_vote_explanations: timeout fetching %s", url)
                browser.close()
                return {}

            html = page.content()
            browser.close()
    except Exception as exc:
        logger.warning("fetch_vote_explanations(%r): %s", press_release_code, exc)
        return {}

    return _parse_press_release_explanations(html)


def _parse_press_release_explanations(html: str) -> dict[str, str]:
    """
    Parse a UN Meetings Coverage HTML page and extract per-country vote
    explanation statements.

    Approach:
      - Extract all <p> paragraph elements via BeautifulSoup
      - Scan each paragraph for country attribution patterns:
          "The representative of [Country]..."
          "[Country]'s representative..."
          "...said the representative of [Country]"
      - Match country name to ISO-3 via COUNTRY_NAME_TO_ISO3
      - Collect all paragraphs for each country and join them

    Returns {iso3: explanation_text}.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Build lookup: lowercase country name → iso3
    # Include common article-prefixed forms ("the United States" → "USA")
    name_to_iso3: dict[str, str] = {}
    for name, iso3 in COUNTRY_NAME_TO_ISO3.items():
        name_to_iso3[name.lower()] = iso3
        name_to_iso3[f"the {name.lower()}"] = iso3

    # Sort by length descending so longer names match before shorter substrings
    sorted_names = sorted(name_to_iso3.keys(), key=len, reverse=True)

    # Attribution patterns — each captures a country reference
    _ATTR_PATTERNS = [
        re.compile(
            r"(?:the\s+)?representative\s+of\s+(the\s+)?(?P<country>[A-Z][A-Za-z ,'()-]{2,50}?)(?:\s*,|\s+said|\s+stated|\s+noted|\s+stressed|\s+added|\s+highlighted|\s*$)",
            re.I,
        ),
        re.compile(
            r"(?P<country>[A-Z][A-Za-z ,'()-]{2,50?})'s\s+representative",
            re.I,
        ),
        re.compile(
            r"said\s+the\s+representative\s+of\s+(the\s+)?(?P<country>[A-Z][A-Za-z ,'()-]{2,50}?)(?:\s*[,.]|\s*$)",
            re.I,
        ),
    ]

    def _find_iso3_in_text(text: str) -> str | None:
        """Return ISO-3 for the first country name found in text."""
        text_lower = text.lower()
        for name in sorted_names:
            if name in text_lower:
                return name_to_iso3[name]
        # Try regex patterns for "representative of X" etc.
        for pat in _ATTR_PATTERNS:
            m = pat.search(text)
            if m:
                country_raw = m.group("country").strip().lower()
                # exact match
                if country_raw in name_to_iso3:
                    return name_to_iso3[country_raw]
                # partial — find the longest matching name that is a substring
                for name in sorted_names:
                    if name in country_raw or country_raw in name:
                        return name_to_iso3[name]
        return None

    # Extract paragraphs from the article body
    # press.un.org uses <div class="field-body"> or similar containers
    body_candidates = (
        soup.select("div.field--name-body p") or
        soup.select("div.field-items p") or
        soup.select("article p") or
        soup.select("main p") or
        soup.find_all("p")
    )

    # Collect per-country text blocks
    country_paragraphs: dict[str, list[str]] = {}

    for p in body_candidates:
        text = p.get_text(" ", strip=True)
        if not text or len(text) < 40:
            continue

        iso3 = _find_iso3_in_text(text)
        if iso3:
            country_paragraphs.setdefault(iso3, []).append(text)

    # Join multiple paragraphs per country with a space
    result: dict[str, str] = {
        iso3: " ".join(paras)
        for iso3, paras in country_paragraphs.items()
    }

    logger.info(
        "_parse_press_release_explanations: found explanations for %d countries",
        len(result),
    )
    return result


# ── HTML vote-table scraper ───────────────────────────────────────────────────

def _scrape_votes_from_html(html: str) -> dict[str, str]:
    """
    Parse the UNDL record HTML page and extract country → vote mapping.

    UNDL record pages (e.g. https://digitallibrary.un.org/record/404863) display
    voting results in a structured section.  The typical DOM patterns are:

    Pattern A — labelled spans with country lists (most common):
        <span class="...">In favour (109):</span>
        <span>Country A, Country B, ...</span>

    Pattern B — definition-list style:
        <dt>In favour</dt><dd>Country A, Country B, ...</dd>

    Pattern C — table with Member State + Vote columns:
        <tr><td>France</td><td>Yes</td></tr>

    Pattern D — heading + following paragraph:
        <h4>In favour</h4><p>Country A, Country B, ...</p>

    We try all patterns in order and merge results.
    """
    soup = BeautifulSoup(html, "lxml")
    country_votes: dict[str, str] = {}

    # ── Pattern A: <span> / <strong> labels followed by country text ──────────
    # Walk all elements; when we find a vote-category label, the NEXT sibling
    # or following element contains the country list.
    _try_label_then_text(soup, country_votes)

    # ── Pattern B: <dt>/<dd> definition lists ─────────────────────────────────
    if not country_votes:
        for dl in soup.find_all("dl"):
            current_vote = None
            for child in dl.children:
                if not hasattr(child, "name"):
                    continue
                if child.name == "dt":
                    text = child.get_text(strip=True).lower()
                    current_vote = _match_vote_heading(text)
                elif child.name == "dd" and current_vote:
                    raw = child.get_text(separator=", ", strip=True)
                    _extract_countries_from_text(raw, current_vote, country_votes)

    # ── Pattern C: table with Member State / Vote columns ─────────────────────
    if not country_votes:
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                continue
            country_col = next(
                (i for i, h in enumerate(headers) if "country" in h or "member" in h or "state" in h),
                None,
            )
            vote_col = next(
                (i for i, h in enumerate(headers) if h in ("vote", "decision", "position")),
                None,
            )
            if country_col is None or vote_col is None:
                continue
            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(country_col, vote_col):
                    continue
                country_name = cells[country_col].get_text(strip=True)
                vote_raw     = cells[vote_col].get_text(strip=True).lower()
                vote_code    = _match_vote_heading(vote_raw) or _VOTE_HEADING_MAP.get(vote_raw)
                if vote_code:
                    iso3 = _country_to_iso3(country_name)
                    if iso3:
                        country_votes[iso3] = vote_code

    # ── Pattern D: heading elements (<h3>–<h5>) followed by <p> ──────────────
    if not country_votes:
        current_vote = None
        for tag in soup.find_all(["h3", "h4", "h5", "p", "div"]):
            text = tag.get_text(strip=True).lower()
            if tag.name in ("h3", "h4", "h5"):
                current_vote = _match_vote_heading(text)
            elif tag.name in ("p", "div") and current_vote:
                raw = tag.get_text(separator=", ", strip=True)
                if raw:
                    _extract_countries_from_text(raw, current_vote, country_votes)

    return country_votes


def _try_label_then_text(soup: BeautifulSoup, out: dict) -> None:
    """
    Walk the page looking for vote-category labels (any inline element whose
    text matches a vote heading).  When found, collect country text from
    immediately following siblings / elements until the next vote label.

    This handles the common UNDL pattern:
        <span><strong>In favour (109):</strong> Country A, Country B, ...</span>
    as well as separated label + text nodes.
    """
    current_vote: str | None = None
    pending_text: list[str] = []

    def flush():
        if current_vote and pending_text:
            _extract_countries_from_text(", ".join(pending_text), current_vote, out)

    for el in soup.find_all(True):
        raw_text = el.get_text(strip=True)
        if not raw_text:
            continue

        # Check if this element IS a vote-category heading (short text only)
        matched = _match_vote_heading(raw_text.lower()) if len(raw_text) < 80 else None
        if matched:
            flush()
            pending_text = []
            current_vote = matched
            # Also grab any trailing country text on the SAME element
            # e.g. "In favour (109): France, Germany, ..."
            remainder = re.sub(r"(?i)(in favour|against|abstain\w*|non-participat\w*|non-members?|absent)[^:]*:?", "", raw_text, count=1).strip(" :")
            if remainder:
                pending_text.append(remainder)
        elif current_vote and el.name in ("span", "p", "td", "li", "div") and len(raw_text) > 2:
            # Avoid re-harvesting text from nested children of already-processed parents
            if not any(child.name in ("strong", "b", "em") and _match_vote_heading(child.get_text(strip=True).lower()) for child in el.find_all(True, recursive=False)):
                pending_text.append(raw_text)

    flush()


def _match_vote_heading(text: str) -> str | None:
    """
    Return a vote code if `text` (lowercase) contains a vote category keyword.
    Returns None if not a vote heading.
    """
    for heading, code in _VOTE_HEADING_MAP.items():
        if heading in text:
            return code
    return None


def _extract_countries_from_text(text: str, vote_code: str, out: dict) -> None:
    """
    Split a comma/semicolon-separated country list and add to `out`.
    Skips entries that don't map to a known country.
    """
    for name in re.split(r"[,;]\s*", text):
        name = name.strip().rstrip(".")
        if not name or len(name) < 3:
            continue
        iso3 = _country_to_iso3(name)
        if iso3:
            out[iso3] = vote_code


def _country_to_iso3(name: str) -> str | None:
    """
    Map a country name (as used by UNDL) to ISO-A3 code.
    Tries exact match, then DB lookup by name/full_name.
    """
    if not name:
        return None
    # Direct lookup in static map
    iso3 = COUNTRY_NAME_TO_ISO3.get(name)
    if iso3:
        return iso3
    # Try case-insensitive
    name_lower = name.lower()
    for k, v in COUNTRY_NAME_TO_ISO3.items():
        if k.lower() == name_lower:
            return v
    return None


# ── Symbol-derived structural tags (Layer 2) ─────────────────────────────────

_UN_SYMBOL_TAG_MAP = {
    "A/RES/":  "UNGA Resolution",
    "A/ES-":   "Emergency Special Session",
    "A/S-":    "Special Session",
    "S/RES/":  "Security Council",
    "S/":      "Security Council",
    "/C.1/":   "First Committee · Disarmament & Security",
    "/C.2/":   "Second Committee · Economic & Financial",
    "/C.3/":   "Third Committee · Social, Humanitarian & Cultural",
    "/C.4/":   "Fourth Committee · Special Political & Decolonization",
    "/C.5/":   "Fifth Committee · Administrative & Budgetary",
    "/C.6/":   "Sixth Committee · Legal",
    "HRC":     "Human Rights Council",
}


def tags_from_symbol(un_symbol: str) -> list[str]:
    """
    Return Layer-2 structural tags derived from the UN document symbol.

    Prefixes that must appear at the START of the symbol use startswith().
    Infixes (committee codes like /C.1/) use 'in' substring check.
    """
    if not un_symbol:
        return []
    tags = []
    # Prefixes — must match at start of symbol
    _START_PREFIXES = {
        "A/RES/": "UNGA Resolution",
        "A/ES-":  "Emergency Special Session",
        "A/S-":   "Special Session",
        "S/RES/": "Security Council",
        "S/":     "Security Council",
        "HRC/":   "Human Rights Council",
    }
    # Infixes — substring match (committee codes embedded in symbol)
    _INFIX_MAP = {
        "/C.1/": "First Committee · Disarmament & Security",
        "/C.2/": "Second Committee · Economic & Financial",
        "/C.3/": "Third Committee · Social, Humanitarian & Cultural",
        "/C.4/": "Fourth Committee · Special Political & Decolonization",
        "/C.5/": "Fifth Committee · Administrative & Budgetary",
        "/C.6/": "Sixth Committee · Legal",
        "HRC":   "Human Rights Council",
    }
    seen = set()
    for prefix, label in _START_PREFIXES.items():
        if un_symbol.startswith(prefix) and label not in seen:
            tags.append(label)
            seen.add(label)
    for infix, label in _INFIX_MAP.items():
        if infix in un_symbol and label not in seen:
            tags.append(label)
            seen.add(label)
    return tags


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_undl_date(raw: str) -> date | None:
    """
    Parse UNDL date formats:
      YYYYMMDD  → date(YYYY, MM, DD)
      YYYY-MM-DD → date(YYYY, MM, DD)
      YYYY       → date(YYYY, 1, 1)
    """
    raw = raw.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    # Year-only fallback
    m = re.search(r"\b(19|20)\d{2}\b", raw)
    if m:
        try:
            return date(int(m.group()), 1, 1)
        except ValueError:
            pass
    return None


def _body_from_symbol(symbol: str) -> str:
    """Derive UN body from document symbol prefix."""
    if symbol.startswith("S/RES/") or symbol.startswith("S/"):
        return "UNSC"
    if "HRC" in symbol or "/HRC/" in symbol:
        return "UNHRC"
    return "UNGA"
