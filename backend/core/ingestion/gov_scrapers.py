"""
core/ingestion/gov_scrapers.py
================================
Playwright-based historical scrapers for official government / UN press release archives.
These produce primary-source official statements — highest quality data for GeoStance.

Why Playwright (not just requests):
  - Several archives use JavaScript for pagination or lazy loading
  - Playwright is already in the stack (playwright==1.59.0)
  - Falls back gracefully to requests+BeautifulSoup for static pages

Scrapers included:
  1. India MEA       — mea.gov.in/press-releases
  2. US State Dept   — state.gov/press-releases
  3. UN News         — news.un.org
  4. China MFA       — fmprc.gov.cn (English)
  5. Russia MFA      — mid.ru (English)
  6. UK FCDO         — gov.uk/fcdo news

Each scraper:
  - Accepts a date range (start_date, end_date)
  - Returns a generator of article dicts: {url, title, text, published_date, country_iso}
  - Is idempotent (callers check for existing post_id before saving)
"""

import hashlib
import logging
import time
from datetime import date, datetime
from typing import Generator

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_html(url: str, timeout: int = 20, retries: int = 2) -> str | None:
    """Fetch raw HTML with retry + polite delay."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; GeoStance/1.0; "
            "Academic research crawler; contact@geostance.in)"
        )
    }
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            time.sleep(1)   # polite crawl — 1 req/sec
            return resp.text
        except Exception as exc:
            if attempt < retries:
                logger.debug("Retry %d for %s: %s", attempt + 1, url, exc)
                time.sleep(3)
            else:
                logger.warning("Failed to fetch %s: %s", url, exc)
                return None


def _extract_text(url: str) -> str:
    """Extract clean article text using trafilatura (best-in-class extractor)."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
            )
            return text or ""
    except Exception as exc:
        logger.debug("trafilatura failed for %s: %s", url, exc)
    return ""


def _parse_date_flexible(date_str: str) -> date | None:
    """Try multiple date formats commonly found on government sites."""
    formats = [
        "%B %d, %Y",   # January 15, 2022
        "%d %B %Y",    # 15 January 2022
        "%d-%m-%Y",    # 15-01-2022
        "%Y-%m-%d",    # 2022-01-15
        "%d/%m/%Y",    # 15/01/2022
        "%b %d, %Y",   # Jan 15, 2022
        "%d %b %Y",    # 15 Jan 2022
        "%Y%m%d",      # 20220115
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


ArticleDict = dict   # {url, title, text, published_date, country_iso}

# ── 1. India Ministry of External Affairs ─────────────────────────────────────

def scrape_india_mea(
    start_date: date,
    end_date: date,
    max_pages: int = 50,
) -> Generator[ArticleDict, None, None]:
    """
    Scrapes India MEA press release archive.
    URL pattern: https://www.mea.gov.in/press-releases.htm
    The site loads press releases in a table with offset-based pagination.
    We request pages until we go past start_date.
    """
    BASE = "https://www.mea.gov.in"
    LISTING_URL = f"{BASE}/press-releases.htm"
    page = 0

    logger.info("[MEA-IND] Scraping %s → %s", start_date, end_date)

    for page_num in range(max_pages):
        offset = page_num * 10
        url = f"{LISTING_URL}?{offset}/Press_Releases" if offset > 0 else LISTING_URL
        html = _get_html(url)
        if not html:
            break

        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("div.view-content .views-row, table.views-table tbody tr")

        if not rows:
            # Try alternate selectors MEA has used at different times
            rows = soup.select(".field-content a[href*='/bilateral-documents'], a[href*='/press-releases']")

        found_any = False
        oldest_on_page = None

        for row in rows:
            link_el = row.select_one("a[href]") or (row if row.name == "a" else None)
            if not link_el:
                continue

            href = link_el.get("href", "")
            if not href.startswith("http"):
                href = BASE + href
            title = link_el.get_text(strip=True)

            # Find date in the row
            date_el = row.select_one(".date-display-single, .views-field-field-release-date, span.date")
            date_str = date_el.get_text(strip=True) if date_el else ""
            pub_date = _parse_date_flexible(date_str) if date_str else None

            if pub_date:
                oldest_on_page = pub_date
                if pub_date > end_date:
                    continue
                if pub_date < start_date:
                    return   # gone past our window — stop

            text = _extract_text(href)
            found_any = True

            yield {
                "url": href,
                "title": title,
                "text": text,
                "published_date": pub_date,
                "country_iso": "IND",
            }

        if not found_any:
            break

        # If the oldest article on this page is still newer than start_date, continue
        if oldest_on_page and oldest_on_page < start_date:
            break


# ── 2. US State Department ────────────────────────────────────────────────────

def scrape_us_state_dept(
    start_date: date,
    end_date: date,
) -> Generator[ArticleDict, None, None]:
    """
    Scrapes US State Department press releases archive.
    The site supports ?year=YYYY filtering which makes date-scoped scraping efficient.
    URL: https://www.state.gov/press-releases/?year=2022
    """
    BASE = "https://www.state.gov"

    years = range(start_date.year, end_date.year + 1)

    for year in years:
        page = 1
        logger.info("[STATE-USA] Scraping year %d", year)

        while True:
            url = f"{BASE}/press-releases/?year={year}&paged={page}"
            html = _get_html(url)
            if not html:
                break

            soup = BeautifulSoup(html, "lxml")

            # State Dept article cards
            articles = soup.select("article.press-release, li.views-row, div.collection-result")
            if not articles:
                break

            found_any = False

            for article in articles:
                link_el = article.select_one("a[href*='/press-releases/']")
                if not link_el:
                    continue

                href = link_el.get("href", "")
                if not href.startswith("http"):
                    href = BASE + href
                title = link_el.get_text(strip=True)

                # Extract date
                date_el = article.select_one("time, span.date, .date-display-single, p.date")
                pub_date = None
                if date_el:
                    dt_attr = date_el.get("datetime") or date_el.get_text(strip=True)
                    pub_date = _parse_date_flexible(dt_attr)

                if pub_date:
                    if pub_date > end_date:
                        continue
                    if pub_date < start_date:
                        continue   # year loop handles range; some pages mix years

                text = _extract_text(href)
                found_any = True

                yield {
                    "url": href,
                    "title": title,
                    "text": text,
                    "published_date": pub_date,
                    "country_iso": "USA",
                }

            if not found_any:
                break

            # Check if next page exists
            next_btn = soup.select_one("a.next, a[rel='next'], li.pager__item--next a")
            if not next_btn:
                break
            page += 1


# ── 3. United Nations News ────────────────────────────────────────────────────

def scrape_un_news(
    start_date: date,
    end_date: date,
) -> Generator[ArticleDict, None, None]:
    """
    Scrapes UN News archive.
    UN News has a structured date-range URL:
    https://news.un.org/en/news/topic/peace-and-security?date=2022-01-01:2022-12-31
    We iterate by year for broad coverage.
    """
    BASE = "https://news.un.org"
    TOPICS = [
        "peace-and-security",
        "humanitarian-aid",
        "human-rights",
        "climate-change",
    ]

    years = range(start_date.year, end_date.year + 1)

    for year in years:
        y_start = max(start_date, date(year, 1, 1))
        y_end   = min(end_date,   date(year, 12, 31))

        for topic in TOPICS:
            page = 0
            logger.info("[UN-NEWS] Scraping topic=%s year=%d", topic, year)

            while True:
                url = (
                    f"{BASE}/en/news/topic/{topic}"
                    f"?date={y_start}:{y_end}"
                    f"&page={page}"
                )
                html = _get_html(url)
                if not html:
                    break

                soup = BeautifulSoup(html, "lxml")
                items = soup.select("article, li.story-node, div.story-card")

                if not items:
                    break

                found_any = False

                for item in items:
                    link_el = item.select_one("a[href*='/story/']")
                    if not link_el:
                        continue

                    href = link_el.get("href", "")
                    if not href.startswith("http"):
                        href = BASE + href
                    title = link_el.get_text(strip=True)

                    date_el = item.select_one("time, span.date, .field--name-field-story-date")
                    pub_date = None
                    if date_el:
                        dt_attr = date_el.get("datetime") or date_el.get_text(strip=True)
                        pub_date = _parse_date_flexible(dt_attr)

                    text = _extract_text(href)
                    found_any = True

                    yield {
                        "url": href,
                        "title": title,
                        "text": text,
                        "published_date": pub_date,
                        "country_iso": None,  # UN is multilateral — no single country
                    }

                if not found_any:
                    break

                next_btn = soup.select_one("a.next, a[rel='next'], li.pager-next a")
                if not next_btn:
                    break
                page += 1


# ── 4. China Ministry of Foreign Affairs (English) ────────────────────────────

def scrape_china_mfa(
    start_date: date,
    end_date: date,
    max_pages: int = 100,
) -> Generator[ArticleDict, None, None]:
    """
    Scrapes China MFA English press releases.
    URL: https://www.fmprc.gov.cn/mfa_eng/xwfw_665399/s2510_665401/
    Pagination is offset-based (page=1,2,3…)
    """
    BASE = "https://www.fmprc.gov.cn"
    LISTING = f"{BASE}/mfa_eng/xwfw_665399/s2510_665401/"

    logger.info("[MFA-CHN] Scraping %s → %s", start_date, end_date)

    for page_num in range(1, max_pages + 1):
        url = f"{LISTING}?page={page_num}" if page_num > 1 else LISTING
        html = _get_html(url)
        if not html:
            break

        soup = BeautifulSoup(html, "lxml")
        items = soup.select("ul.news_list li, div.news-item, li.clearfix")

        if not items:
            break

        found_any = False
        oldest_on_page = None

        for item in items:
            link_el = item.select_one("a[href]")
            if not link_el:
                continue

            href = link_el.get("href", "")
            if not href.startswith("http"):
                href = BASE + href if href.startswith("/") else f"{LISTING}{href}"
            title = link_el.get_text(strip=True)

            # Date is often in a <span> or sibling element
            date_el = item.select_one("span.date, em, .date-info, p.date")
            pub_date = None
            if date_el:
                pub_date = _parse_date_flexible(date_el.get_text(strip=True))

            if pub_date:
                oldest_on_page = pub_date
                if pub_date > end_date:
                    continue
                if pub_date < start_date:
                    return

            text = _extract_text(href)
            found_any = True

            yield {
                "url": href,
                "title": title,
                "text": text,
                "published_date": pub_date,
                "country_iso": "CHN",
            }

        if not found_any:
            break
        if oldest_on_page and oldest_on_page < start_date:
            break


# ── 5. Russia MFA (English) ───────────────────────────────────────────────────

def scrape_russia_mfa(
    start_date: date,
    end_date: date,
    max_pages: int = 100,
) -> Generator[ArticleDict, None, None]:
    """
    Scrapes Russia MFA English press releases.
    URL: https://mid.ru/en/press_service/spokesman/briefings/
    """
    BASE = "https://mid.ru"
    LISTING = f"{BASE}/en/foreign_policy/news/"

    logger.info("[MFA-RUS] Scraping %s → %s", start_date, end_date)

    for page_num in range(max_pages):
        url = f"{LISTING}?page={page_num}" if page_num > 0 else LISTING
        html = _get_html(url, timeout=30)
        if not html:
            break

        soup = BeautifulSoup(html, "lxml")
        items = soup.select("div.announce, article.news-item, li.news__item")

        if not items:
            break

        found_any = False
        oldest_on_page = None

        for item in items:
            link_el = item.select_one("a[href]")
            if not link_el:
                continue

            href = link_el.get("href", "")
            if not href.startswith("http"):
                href = BASE + href
            title = link_el.get_text(strip=True)

            date_el = item.select_one("time, span.date, .announce__date, .date")
            pub_date = None
            if date_el:
                dt = date_el.get("datetime") or date_el.get_text(strip=True)
                pub_date = _parse_date_flexible(dt)

            if pub_date:
                oldest_on_page = pub_date
                if pub_date > end_date:
                    continue
                if pub_date < start_date:
                    return

            text = _extract_text(href)
            found_any = True

            yield {
                "url": href,
                "title": title,
                "text": text,
                "published_date": pub_date,
                "country_iso": "RUS",
            }

        if not found_any:
            break
        if oldest_on_page and oldest_on_page < start_date:
            break


# ── 6. UK Foreign Commonwealth Development Office ────────────────────────────

def scrape_uk_fcdo(
    start_date: date,
    end_date: date,
) -> Generator[ArticleDict, None, None]:
    """
    Scrapes UK FCDO news & press releases.
    gov.uk provides structured date filtering via query params.
    URL: https://www.gov.uk/search/news-and-communications?keywords=&...
    """
    BASE = "https://www.gov.uk"

    years = range(start_date.year, end_date.year + 1)

    for year in years:
        page = 1
        logger.info("[FCDO-GBR] Scraping year %d", year)

        while True:
            url = (
                f"{BASE}/search/news-and-communications"
                f"?organisations[]=foreign-commonwealth-development-office"
                f"&public_timestamp[from]={year}-01-01"
                f"&public_timestamp[to]={year}-12-31"
                f"&page={page}"
            )
            html = _get_html(url)
            if not html:
                break

            soup = BeautifulSoup(html, "lxml")
            items = soup.select("li.gem-c-document-list__item, article")

            if not items:
                break

            found_any = False

            for item in items:
                link_el = item.select_one("a[href]")
                if not link_el:
                    continue

                href = link_el.get("href", "")
                if not href.startswith("http"):
                    href = BASE + href
                title = link_el.get_text(strip=True)

                date_el = item.select_one("time, .gem-c-metadata__definition, span.date")
                pub_date = None
                if date_el:
                    dt = date_el.get("datetime") or date_el.get_text(strip=True)
                    pub_date = _parse_date_flexible(dt)

                if pub_date:
                    if pub_date > end_date or pub_date < start_date:
                        continue

                text = _extract_text(href)
                found_any = True

                yield {
                    "url": href,
                    "title": title,
                    "text": text,
                    "published_date": pub_date,
                    "country_iso": "GBR",
                }

            if not found_any:
                break

            next_btn = soup.select_one("a[rel='next']")
            if not next_btn:
                break
            page += 1


# ── Registry: slug → (scraper_function, country_iso, label) ──────────────────

SCRAPERS = {
    "mea":        (scrape_india_mea,    "IND", "India MEA"),
    "state-dept": (scrape_us_state_dept,"USA", "US State Dept"),
    "un-news":    (scrape_un_news,       None, "UN News"),
    "china-mfa":  (scrape_china_mfa,    "CHN", "China MFA"),
    "russia-mfa": (scrape_russia_mfa,   "RUS", "Russia MFA"),
    "uk-fcdo":    (scrape_uk_fcdo,      "GBR", "UK FCDO"),
}
