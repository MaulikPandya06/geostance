"""
GeoStance Celery tasks
======================
All ingestion is free — no Elasticsearch, no paid APIs.

Ingestion pipeline:
  1. ingest_rss_feeds()         — pulls official MFA / UN / government RSS feeds
  2. ingest_gdelt_articles()    — queries GDELT 2.0 Doc API for a tracked event
  3. classify_rawposts_with_ai()— AI stance classification (NVIDIA NIM, free tier)
  4. regenerate_summary_task()  — AI summary per country×event pair
"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import redis
import requests
from celery import chain, shared_task
from django.db import IntegrityError
from django.utils import timezone

from core.models import Country, Event, EventSuggestion, RawPost, Statement
from core.services.summary_service import regenerate_country_event_summary
from core.utils.text import url_to_post_id

logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(
    os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
)

CLASSIFY_LOCK_KEY = "classify_rawposts_running"
CLASSIFY_LOCK_TTL = 60 * 30   # 30 min
SUMMARY_LOCK_TTL  = 60 * 10   # 10 min

# ─────────────────────────────────────────────────────────────────────────────
# RSS Feed Registry
# Official government / intergovernmental sources — 100 % free
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    # ── South Asia ──────────────────────────────────────────────────────────
    {
        "url": "https://www.mea.gov.in/rss/press-releases.xml",
        "country_iso": "IND",
        "label": "India MEA",
    },
    {
        "url": "https://mofa.gov.pk/feed/",
        "country_iso": "PAK",
        "label": "Pakistan MFA",
    },
    # ── East Asia ────────────────────────────────────────────────────────────
    {
        "url": "https://www.fmprc.gov.cn/mfa_eng/rss.xml",
        "country_iso": "CHN",
        "label": "China MFA",
    },
    {
        "url": "https://www.mofa.go.jp/rss/mofa_rss_en.xml",
        "country_iso": "JPN",
        "label": "Japan MOFA",
    },
    {
        "url": "https://www.mofa.go.kr/eng/rss/pressRelease.xml",
        "country_iso": "KOR",
        "label": "South Korea MOFA",
    },
    # ── Europe ───────────────────────────────────────────────────────────────
    {
        "url": "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office.atom",
        "country_iso": "GBR",
        "label": "UK FCDO",
    },
    {
        "url": "https://www.diplomatie.gouv.fr/spip.php?page=backend-de",
        "country_iso": "FRA",
        "label": "France MFA",
    },
    {
        "url": "https://www.auswaertiges-amt.de/blob/215226/feed.rss",
        "country_iso": "DEU",
        "label": "Germany AA",
    },
    # ── Americas ─────────────────────────────────────────────────────────────
    {
        "url": "https://www.state.gov/press-releases/feed/",
        "country_iso": "USA",
        "label": "US State Dept",
    },
    {
        "url": "https://www.canada.ca/en/global-affairs.rss",
        "country_iso": "CAN",
        "label": "Canada GAC",
    },
    # ── Middle East ──────────────────────────────────────────────────────────
    {
        "url": "https://www.mofa.gov.sa/en/MediaCenter/News/pages/rss.aspx",
        "country_iso": "SAU",
        "label": "Saudi Arabia MFA",
    },
    {
        "url": "https://www.mfa.gov.tr/rss.en.mfa",
        "country_iso": "TUR",
        "label": "Turkey MFA",
    },
    # ── Russia / Central Asia ────────────────────────────────────────────────
    {
        "url": "https://mid.ru/en/rss.xml",
        "country_iso": "RUS",
        "label": "Russia MFA",
    },
    # ── Multilateral / UN ────────────────────────────────────────────────────
    {
        "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        "country_iso": None,   # UN is multilateral — skip country assignment
        "label": "UN News",
    },
]


@shared_task
def ingest_rss_feeds():
    """
    Pull every feed in RSS_FEEDS, save new articles as RawPost.
    Uses feedparser (pure-Python, no external service).
    Returns counts of created / skipped / errored per feed.
    """
    try:
        import feedparser
    except ImportError:
        logger.error("feedparser not installed. Run: pip install feedparser")
        return {"error": "feedparser_missing"}

    results = []

    for feed_cfg in RSS_FEEDS:
        feed_url     = feed_cfg["url"]
        country_iso  = feed_cfg.get("country_iso")
        label        = feed_cfg.get("label", feed_url)

        country = None
        if country_iso:
            country = Country.objects.filter(isoa3_code=country_iso).first()
            if not country:
                logger.warning("Country not found for ISO %s (%s)", country_iso, label)
                results.append({"feed": label, "error": "country_not_found"})
                continue

        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.error("Feed parse error %s: %s", label, exc)
            results.append({"feed": label, "error": str(exc)})
            continue

        created = skipped = 0

        for entry in parsed.entries:
            link = (entry.get("link") or "").strip()
            if not link:
                skipped += 1
                continue

            # Normalised URL hash — catches http/https, www., UTM variants
            post_id = url_to_post_id(link)

            # Parse publish date
            published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            if published_parsed:
                try:
                    posted_at = timezone.make_aware(
                        datetime(*published_parsed[:6]),
                        timezone.get_current_timezone(),
                    )
                except Exception:
                    posted_at = timezone.now()
            else:
                posted_at = timezone.now()

            title   = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()

            # Strip HTML tags from summary
            try:
                from bs4 import BeautifulSoup
                summary = BeautifulSoup(summary, "lxml").get_text(" ", strip=True)
            except Exception:
                pass

            try:
                RawPost.objects.create(
                    country=country,
                    platform="rss",
                    account_handle="",
                    post_id=post_id,
                    post_text=summary,
                    combined_text=f"{title}\n\n{summary}",
                    posted_at=posted_at,
                    post_url=link,
                    source_url=link,
                    title=title,
                    language="en",
                    content_type="rss",
                    classify_ai_processed=False,
                )
                created += 1
            except IntegrityError:
                # Another worker already inserted this post_id — safe to skip
                skipped += 1

        logger.info("[RSS] %s → created=%s skipped=%s", label, created, skipped)
        results.append({"feed": label, "created": created, "skipped": skipped})

    return results


# ─────────────────────────────────────────────────────────────────────────────
# GDELT 2.0 DOC API  — completely free, no API key
# Docs: https://blog.gdeltproject.org/gdelt-2-0-our-global-data-feed/
# ─────────────────────────────────────────────────────────────────────────────

GDELT_API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


def _build_gdelt_query(event: Event) -> str:
    """Build a keyword query string from an event's title + description."""
    # Use first 4 words of title as keywords
    words = (event.title + " " + event.description).split()
    keywords = " ".join(words[:6])
    return keywords


@shared_task
def ingest_gdelt_articles(event_id: int, timespan: str = "1week", max_records: int = 50):
    """
    Query GDELT 2.0 DOC API for news articles about a tracked event.
    Extracts full article text via trafilatura, saves as RawPost.

    Args:
        event_id:    GeoStance Event pk to query for
        timespan:    GDELT timespan string (e.g. '24h', '1week', '1month')
        max_records: Maximum GDELT results to fetch (max 250 per request)
    """
    try:
        import trafilatura
    except ImportError:
        logger.error("trafilatura not installed. Run: pip install trafilatura")
        return {"error": "trafilatura_missing"}

    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        logger.error("Event %s not found", event_id)
        return {"error": "event_not_found"}

    query = _build_gdelt_query(event)

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": min(max_records, 250),
        "timespan": timespan,
        "format": "json",
        "sourcelang": "english",
    }

    url = f"{GDELT_API_BASE}?{urlencode(params)}"
    logger.info("[GDELT] Querying: %s", url)

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("[GDELT] API error: %s", exc)
        return {"error": str(exc)}

    articles = data.get("articles", [])
    if not articles:
        logger.info("[GDELT] No articles returned for event %s", event_id)
        return {"event_id": event_id, "articles_found": 0, "created": 0}

    created = skipped = failed = 0

    # Map GDELT country codes (FIPS → ISO-A3) — partial list of key countries
    # GDELT uses FIPS 10-4 country codes; we map the most common ones
    GDELT_FIPS_TO_ISO3 = {
        "IN": "IND", "CH": "CHN", "RS": "RUS", "US": "USA",
        "UK": "GBR", "FR": "FRA", "GM": "DEU", "JA": "JPN",
        "PK": "PAK", "IS": "ISR", "PA": "PAK", "IR": "IRN",
        "SY": "SYR", "EG": "EGY", "SA": "SAU", "TU": "TUR",
        "AU": "AUS", "CA": "CAN", "BR": "BRA", "AR": "ARG",
        "SF": "ZAF", "NG": "NGA", "KS": "KOR", "VM": "VNM",
        "ID": "IDN", "MY": "MYS", "TH": "THA", "BD": "BGD",
        "UP": "UKR", "PL": "POL", "IT": "ITA", "SP": "ESP",
        "NL": "NLD", "SW": "SWE", "NO": "NOR", "DA": "DNK",
        "FI": "FIN", "EZ": "CZE", "HU": "HUN", "RO": "ROU",
        "GR": "GRC", "PO": "PRT", "BE": "BEL", "AE": "ARE",
        "QA": "QAT", "KU": "KWT", "IO": "IRQ", "YM": "YEM",
        "LY": "LBY", "MO": "MAR", "TU": "TUN", "AG": "DZA",
        "ET": "ETH", "KE": "KEN", "GH": "GHA", "MX": "MEX",
        "CO": "COL", "VE": "VEN", "CI": "CHL", "PE": "PER",
    }

    country_cache: dict[str, Country | None] = {}

    for article in articles:
        article_url = (article.get("url") or "").strip()
        if not article_url:
            skipped += 1
            continue

        # Normalised URL hash — catches http/https, www., UTM variants
        post_id = url_to_post_id(article_url)

        # Resolve country from GDELT's sourcecountry field
        gdelt_country_code = (article.get("sourcecountry") or "").strip()
        if gdelt_country_code not in country_cache:
            iso3 = GDELT_FIPS_TO_ISO3.get(gdelt_country_code)
            country_cache[gdelt_country_code] = (
                Country.objects.filter(isoa3_code=iso3).first() if iso3 else None
            )
        country = country_cache[gdelt_country_code]
        if not country:
            skipped += 1
            continue

        # Extract full article text using trafilatura
        try:
            downloaded = trafilatura.fetch_url(article_url)
            full_text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
            ) or ""
        except Exception as exc:
            logger.debug("Text extraction failed for %s: %s", article_url, exc)
            full_text = ""

        title     = (article.get("title") or "").strip()
        seendate  = article.get("seendate", "")  # e.g. "20240115T120000Z"

        try:
            posted_at = timezone.make_aware(
                datetime.strptime(seendate, "%Y%m%dT%H%M%SZ"),
                timezone.utc,
            )
        except Exception:
            posted_at = timezone.now()

        combined = f"{title}\n\n{full_text}".strip() or title

        try:
            RawPost.objects.create(
                country=country,
                platform="gdelt",
                account_handle="",
                post_id=post_id,
                post_text=full_text,
                combined_text=combined,
                posted_at=posted_at,
                post_url=article_url,
                source_url=article_url,
                title=title,
                language="en",
                content_type="gdelt",
                classify_ai_processed=False,
            )
            created += 1
        except IntegrityError:
            skipped += 1
        except Exception as exc:
            logger.error("RawPost create error: %s", exc)
            failed += 1

    logger.info(
        "[GDELT] event=%s created=%s skipped=%s failed=%s",
        event_id, created, skipped, failed,
    )
    return {
        "event_id": event_id,
        "articles_found": len(articles),
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }


@shared_task
def ingest_all_events_gdelt(timespan: str = "1week"):
    """
    Run GDELT ingestion for every tracked Event in the DB.
    Safe to schedule daily via Celery Beat.
    """
    events = list(Event.objects.values_list("id", flat=True))
    for event_id in events:
        ingest_gdelt_articles.delay(event_id, timespan=timespan)
    return {"queued_events": len(events)}


# ─────────────────────────────────────────────────────────────────────────────
# Historical Backfill Tasks
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def backfill_gdelt_month(
    event_id: int,
    year: int,
    month: int,
    max_records: int = 250,
    extract_text: bool = True,
):
    """
    Backfill GDELT articles for ONE calendar month for a single event.
    Each month is a separate Celery task so the queue stays responsive
    and failed months can be retried individually.

    Queued by the `historical_backfill` management command.
    """
    from datetime import date
    from core.ingestion.gdelt import (
        months_between, build_event_query,
        fetch_gdelt_window, save_articles_as_rawposts,
    )

    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        logger.error("backfill_gdelt_month: Event %s not found", event_id)
        return {"error": "event_not_found"}

    # Build country lookup once
    country_lookup = {
        c.isoa3_code.upper(): c
        for c in Country.objects.all()
    }

    query = build_event_query(event)
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    # Get the single window for this month
    windows = months_between(start, end)
    if not windows:
        return {"created": 0, "skipped": 0}

    window_start, window_end = windows[0]
    articles = fetch_gdelt_window(query, window_start, window_end, max_records)
    created, skipped = save_articles_as_rawposts(
        articles, country_lookup, extract_text=extract_text
    )

    logger.info(
        "[GDELT-BACKFILL] event=%s %d-%02d → articles=%d created=%d skipped=%d",
        event_id, year, month, len(articles), created, skipped,
    )
    return {
        "event_id": event_id,
        "year": year,
        "month": month,
        "articles_fetched": len(articles),
        "created": created,
        "skipped": skipped,
    }


@shared_task
def backfill_gov_archive(
    source: str,
    start_date_str: str,
    end_date_str: str,
):
    """
    Run a government archive scraper for a given date range.

    Args:
        source:         One of: mea, state-dept, un-news, china-mfa, russia-mfa, uk-fcdo
        start_date_str: ISO date string e.g. "2020-01-01"
        end_date_str:   ISO date string e.g. "2024-12-31"
    """
    from datetime import date
    from core.ingestion.gov_scrapers import SCRAPERS
    from core.models import RawPost, Country
    import hashlib
    from django.utils import timezone

    if source not in SCRAPERS:
        logger.error("backfill_gov_archive: unknown source '%s'", source)
        return {"error": f"unknown_source: {source}"}

    scraper_fn, country_iso, label = SCRAPERS[source]

    try:
        start = date.fromisoformat(start_date_str)
        end   = date.fromisoformat(end_date_str)
    except ValueError as exc:
        return {"error": f"bad_date: {exc}"}

    # Country lookup
    country_lookup = {
        c.isoa3_code.upper(): c
        for c in Country.objects.all()
    }

    created = skipped = failed = 0

    try:
        import trafilatura
        _has_trafilatura = True
    except ImportError:
        _has_trafilatura = False

    for article in scraper_fn(start, end):
        url = (article.get("url") or "").strip()
        if not url:
            skipped += 1
            continue

        # Normalised URL hash — catches http/https, www., UTM variants
        post_id = url_to_post_id(url)

        # Resolve country
        iso = (article.get("country_iso") or country_iso or "").upper()
        country = country_lookup.get(iso)
        if not country:
            skipped += 1
            continue

        pub_date = article.get("published_date")
        if pub_date:
            posted_at = timezone.make_aware(
                datetime(pub_date.year, pub_date.month, pub_date.day),
                timezone.get_current_timezone(),
            )
        else:
            posted_at = timezone.now()

        title = (article.get("title") or "").strip()
        text  = (article.get("text")  or "").strip()

        try:
            RawPost.objects.create(
                country=country,
                platform="scrape",
                account_handle="",
                post_id=post_id,
                post_text=text,
                combined_text=f"{title}\n\n{text}".strip(),
                posted_at=posted_at,
                post_url=url,
                source_url=url,
                title=title,
                language="en",
                content_type=f"scrape:{source}",
                classify_ai_processed=False,
            )
            created += 1
        except IntegrityError:
            # Concurrent worker or cross-pipeline duplicate — safe to skip
            skipped += 1
        except Exception as exc:
            logger.error("[%s] Save error %s: %s", label, url, exc)
            failed += 1

    logger.info(
        "[GOV-SCRAPER] %s %s→%s created=%d skipped=%d failed=%d",
        label, start_date_str, end_date_str, created, skipped, failed,
    )
    return {
        "source": source,
        "start": start_date_str,
        "end":   end_date_str,
        "created": created,
        "skipped": skipped,
        "failed":  failed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AI Classification  (unchanged logic, just cleaned up)
# ─────────────────────────────────────────────────────────────────────────────

def get_nvidia_client():
    from openai import OpenAI
    return OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=os.getenv("NVIDIA_NIM_API_KEY"),
    )


@shared_task
def classify_rawposts_with_ai(batch_size: int = 25):
    """
    Batch-classify unprocessed RawPosts with NVIDIA NIM (Llama 3.3 70B).
    Creates Statement objects for confident matches (confidence >= 0.5).
    """
    redis_client.setex(CLASSIFY_LOCK_KEY, CLASSIFY_LOCK_TTL, "1")

    try:
        from openai import OpenAI

        client = get_nvidia_client()

        unprocessed = list(
            RawPost.objects
            .filter(classify_ai_processed=False)
            .select_related("country")[:batch_size]
        )

        if not unprocessed:
            logger.info("No unprocessed RawPosts found.")
            return {"processed": 0}

        events = list(Event.objects.values("id", "title", "description", "start_date"))
        if not events:
            logger.warning("No Events found in DB.")
            return {"error": "No events in DB"}

        event_list_text = "\n".join([
            f"[ID:{e['id']}] {e['title']} — {e['description']} (since {e['start_date']})"
            for e in events
        ])

        posts_text = ""
        for i, post in enumerate(unprocessed):
            text = (post.combined_text or post.post_text or "").strip()[:1000]
            posts_text += f"\nPOST_INDEX: {i}\nCountry: {post.country.name}\nText: {text}\n---"

        prompt = f"""You are a senior geopolitical intelligence analyst.

KNOWN GEOPOLITICAL EVENTS (match to ONE of these by ID, or null):
{event_list_text}

Analyze EACH diplomatic post below.

POSTS:
{posts_text}

Return exactly {len(unprocessed)} JSON objects in an array:
[
  {{
    "post_index": 0,
    "event_id": <integer or null>,
    "suggested_event_name": "<SHORT canonical name IF event_id is null — e.g. 'North Korea ICBM Test 2024' — else null>",
    "suggested_event_description": "<ONE sentence IF event_id is null — else null>",
    "stance": "support" | "neutral" | "oppose",
    "confidence": <float 0.0-1.0>,
    "summary": "<2-3 sentence English summary>",
    "topics": ["topic1", "topic2"],
    "reasoning": "<1 sentence: why this event and stance>"
  }},
  ...
]

RULES:
- Match to a KNOWN event even via indirect references (geography, organisations, terminology)
- If the post is clearly about a geopolitical event NOT in the known list:
    set event_id: null AND fill suggested_event_name + suggested_event_description
- If the post is routine/bilateral/unrelated to any major event:
    set event_id: null, suggested_event_name: null
- Same order as posts (post_index must match)
- ONLY valid JSON array, no markdown, no extra text"""

        response = client.chat.completions.create(
            model="meta/llama-3.3-70b-instruct",
            messages=[
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.1,
            max_tokens=8192,
        )

        raw = response.choices[0].message.content.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        results = json.loads(raw)

        processed = created_statements = failed = 0

        for item in results:
            idx = item.get("post_index")
            if idx is None or idx >= len(unprocessed):
                continue

            post = unprocessed[idx]

            try:
                stance = item.get("stance", "neutral")
                if stance not in ("support", "neutral", "oppose"):
                    stance = "neutral"

                confidence = float(item.get("confidence", 0.0))
                event_id   = item.get("event_id")
                event      = None

                if event_id:
                    event = Event.objects.filter(id=event_id).first()

                if event and confidence >= 0.5:
                    # Normalise the URL so the same article reached via
                    # http/www/UTM variants all map to one Statement.
                    # Fall back to empty string (never None) so the
                    # unique_statement_per_source_url DB constraint
                    # (which excludes source_url='') is not triggered.
                    norm_url = (
                        url_to_post_id(post.post_url)   # stable 32-char key
                        if post.post_url
                        else ""
                    )
                    # Use the normalised hash as lookup key so identical
                    # articles from different pipelines produce one Statement.
                    lookup = (
                        {"event": event, "country": post.country, "source_url": norm_url}
                        if norm_url
                        else {"event": event, "country": post.country, "raw_post": post}
                    )
                    try:
                        Statement.objects.get_or_create(
                            **lookup,
                            defaults={
                                "raw_post": post,
                                "text": (post.combined_text or post.post_text or ""),
                                "stance": stance,
                                "confidence_score": confidence,
                                "summary": item.get("summary", ""),
                                "topics": item.get("topics", []),
                                "source_url": norm_url,
                                "publish_date": post.posted_at.date(),
                            },
                        )
                        created_statements += 1
                        logger.info(
                            "[%s] stance=%s conf=%.2f",
                            post.country.name, stance, confidence,
                        )
                    except IntegrityError:
                        # Race: another worker created this Statement first
                        logger.debug(
                            "[%s] Statement already exists (race), skipping",
                            post.country.name,
                        )
                else:
                    logger.info(
                        "[%s] No match or low confidence %.2f",
                        post.country.name, confidence,
                    )
                    # ── Auto-detect new events ────────────────────────────
                    # If the LLM suggested a new event name, run it through
                    # semantic dedup before storing as an EventSuggestion.
                    suggested_name = (item.get("suggested_event_name") or "").strip()
                    if suggested_name:
                        _handle_event_suggestion(post, item, suggested_name)

                processed += 1
                post.classify_ai_processed = True
                post.save()

            except Exception as exc:
                logger.error("Error processing post index %s: %s", idx, exc)
                failed += 1

        logger.info(
            "Batch done — Processed: %s | Created: %s | Failed: %s",
            processed, created_statements, failed,
        )
        return {
            "processed": processed,
            "statements_created": created_statements,
            "failed": failed,
        }

    except json.JSONDecodeError as exc:
        logger.error("JSON parse failed: %s", exc)
        return {"error": "json_parse_failed"}

    except Exception as exc:
        logger.error("classify task error: %s", exc)
        return {"error": str(exc)}

    finally:
        redis_client.delete(CLASSIFY_LOCK_KEY)
        logger.info("Classify lock released.")


@shared_task
def full_ingest_and_classify():
    """
    Master pipeline task — safe to run on a daily Celery Beat schedule:
      1. Pull RSS feeds from all official government sources
      2. Pull GDELT articles for all tracked events
      3. Classify all new RawPosts with AI
    """
    if redis_client.exists(CLASSIFY_LOCK_KEY):
        logger.info("Classify still running. Skipping ingestion.")
        return {"skipped": True, "reason": "classify_locked"}

    rss_result  = ingest_rss_feeds()
    gdelt_result = ingest_all_events_gdelt.delay("1week")
    classify_rawposts_with_ai.delay()

    return {
        "rss":     rss_result,
        "gdelt":   "queued",
        "classify": "queued",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Event suggestion helpers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_event_suggestion(post, llm_item: dict, suggested_name: str):
    """
    Called when the LLM returns a suggested_event_name for an unmatched article.

    Pipeline:
      1. Check semantic similarity against existing Events
         - similarity > 0.92 → actually belongs to an existing event;
           create a Statement instead of an EventSuggestion
         - 0.75–0.92 → probably same; store as suggestion flagged 'likely'
         - < 0.75 → genuinely new; store/increment suggestion

      2. For each genuinely new suggestion, check existing EventSuggestions
         for semantic duplicates and merge (increment article_count) rather
         than creating a new suggestion row.
    """
    from core.services.event_service import find_similar_event
    from core.services.event_service import embed_event_text
    from pgvector.django import CosineDistance

    suggested_desc = (llm_item.get("suggested_event_description") or "").strip()

    # Step 1 — check against existing Events
    similar_event, similarity, band = find_similar_event(
        suggested_name, suggested_desc
    )

    if band == "same" and similar_event:
        # The article actually belongs to an existing event — create Statement
        confidence = float(llm_item.get("confidence") or 0.6)
        stance = llm_item.get("stance", "neutral")
        if stance not in ("support", "neutral", "oppose"):
            stance = "neutral"

        from core.utils.text import url_to_post_id
        norm_url = url_to_post_id(post.post_url) if post.post_url else ""
        lookup = (
            {"event": similar_event, "country": post.country, "source_url": norm_url}
            if norm_url
            else {"event": similar_event, "country": post.country, "raw_post": post}
        )
        try:
            Statement.objects.get_or_create(
                **lookup,
                defaults={
                    "raw_post": post,
                    "text": post.combined_text or post.post_text or "",
                    "stance": stance,
                    "confidence_score": confidence,
                    "summary": llm_item.get("summary", ""),
                    "topics": llm_item.get("topics", []),
                    "source_url": norm_url,
                    "publish_date": post.posted_at.date(),
                },
            )
            logger.info(
                "Redirected suggestion '%s' → existing Event '%s' (sim=%.3f)",
                suggested_name, similar_event.title, similarity,
            )
        except IntegrityError:
            pass
        return

    # Step 2 — check existing EventSuggestions for semantic duplicates
    sugg_embedding = embed_event_text(suggested_name, suggested_desc)

    merged_into = None
    if sugg_embedding:
        try:
            nearest_sugg = (
                EventSuggestion.objects
                .filter(status="pending")
                .exclude(embedding=None)
                .annotate(dist=CosineDistance("embedding", sugg_embedding))
                .order_by("dist")
                .first()
            )
            if nearest_sugg and (1.0 - float(nearest_sugg.dist)) > 0.80:
                # Merge into existing suggestion
                nearest_sugg.article_count += 1
                nearest_sugg.supporting_posts.add(post)
                nearest_sugg.save(update_fields=["article_count", "updated_at"])
                merged_into = nearest_sugg
                logger.info(
                    "Merged suggestion '%s' into existing '%s' (count=%d)",
                    suggested_name, nearest_sugg.suggested_name,
                    nearest_sugg.article_count,
                )
        except Exception as exc:
            logger.debug("EventSuggestion similarity check failed: %s", exc)

    if merged_into is None:
        # Create a fresh EventSuggestion
        try:
            sugg = EventSuggestion.objects.create(
                suggested_name=suggested_name,
                suggested_description=suggested_desc,
                embedding=sugg_embedding,
                article_count=1,
                nearest_event=similar_event,
                similarity_score=round(similarity, 4),
                status="pending",
            )
            sugg.supporting_posts.add(post)
            logger.info(
                "New EventSuggestion created: '%s' (nearest=%.3f)",
                suggested_name, similarity,
            )
        except Exception as exc:
            logger.error("Failed to create EventSuggestion: %s", exc)


@shared_task
def auto_promote_event_suggestions(min_articles: int = 5):
    """
    Promote pending EventSuggestions that have accumulated enough evidence
    (article_count >= min_articles) AND are not near-duplicates of existing
    Events (similarity < 0.75).

    Creates a new Event for each qualifying suggestion, then links all
    supporting RawPosts back through re-classification.

    Safe to run on a weekly Celery Beat schedule.
    """
    candidates = EventSuggestion.objects.filter(
        status="pending",
        article_count__gte=min_articles,
    )

    promoted = skipped = 0

    for sugg in candidates:
        from core.services.event_service import find_similar_event, embed_event_text
        from datetime import date

        # Re-check similarity at promotion time (new events may have been added)
        _, similarity, band = find_similar_event(
            sugg.suggested_name, sugg.suggested_description
        )

        if band in ("same", "likely"):
            # Still too close to existing — skip auto-promotion, flag for human
            logger.info(
                "EventSuggestion '%s' not promoted (too similar to existing, %.3f)",
                sugg.suggested_name, similarity,
            )
            skipped += 1
            continue

        # Promote
        from core.models import Event
        embedding = embed_event_text(sugg.suggested_name, sugg.suggested_description)

        event = Event.objects.create(
            title=sugg.suggested_name,
            description=sugg.suggested_description,
            start_date=date.today(),
            embedding=embedding,
        )
        sugg.status = "approved"
        sugg.approved_event = event
        sugg.save(update_fields=["status", "approved_event", "updated_at"])

        # Re-queue supporting RawPosts for classification against the new event
        sugg.supporting_posts.update(classify_ai_processed=False)

        logger.info(
            "Auto-promoted EventSuggestion '%s' → Event id=%s (%d articles re-queued)",
            sugg.suggested_name, event.pk, sugg.article_count,
        )
        promoted += 1

    return {"promoted": promoted, "skipped_too_similar": skipped}


# ─────────────────────────────────────────────────────────────────────────────
# UN Resolution live ingestion  (daily fetch + event classification)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def fetch_new_un_votes(lookback_days: int = 14):
    """
    Fetch UN voting records published in the last `lookback_days` days from
    the UN Digital Library and upsert them into the database.

    Schedule: daily (Celery Beat).  lookback_days=14 ensures we catch any
    records that appear in UNDL a few days after the actual vote.

    After upserting each resolution:
      - Queues enrich_resolution_with_ai for AI tagging (unless already done)
      - Queues classify_resolution_to_event for Event linkage
    """
    from core.ingestion.un_library import (
        search_resolutions_by_year,
        fetch_country_votes,
        tags_from_symbol,
    )
    from core.models import Country, UNResolution, UNVote
    from django.db import transaction
    import time

    today      = date.today()
    cutoff     = today - timedelta(days=lookback_days)
    check_year = cutoff.year   # may span two years near Jan 1

    country_lookup = {c.isoa3_code.upper(): c for c in Country.objects.all()}

    created_res = updated_res = created_votes = 0

    years_to_check = {cutoff.year, today.year}  # handle Dec→Jan boundary
    for year in years_to_check:
        for rec in search_resolutions_by_year(year):
            if rec.vote_date is None or rec.vote_date < cutoff:
                continue

            symbol_tags = tags_from_symbol(rec.un_symbol)
            defaults = dict(
                undl_id=rec.undl_id,
                title=rec.title or "",
                vote_date=rec.vote_date,
                session=rec.session,
                body=rec.body,
                short_description=rec.short_description or "",
                resolution_text=rec.resolution_text or "",
                topic_tags=rec.topic_tags,
                symbol_tags=symbol_tags,
            )
            try:
                with transaction.atomic():
                    if rec.un_symbol:
                        obj, is_new = UNResolution.objects.update_or_create(
                            un_symbol=rec.un_symbol,
                            defaults=defaults,
                        )
                    else:
                        obj, is_new = UNResolution.objects.get_or_create(
                            undl_id=rec.undl_id,
                            defaults={**defaults, "un_symbol": ""},
                        )
            except Exception as exc:
                logger.error("fetch_new_un_votes: DB error for %s: %s", rec.un_symbol, exc)
                continue

            if is_new:
                created_res += 1
            else:
                updated_res += 1

            # Country votes (scrape individual record)
            if rec.undl_id:
                country_votes = fetch_country_votes(rec.undl_id)
                time.sleep(1.0)
                new_vote_objs = []
                existing_pairs = set(
                    UNVote.objects.filter(resolution=obj)
                    .values_list("resolution_id", "country_id")
                )
                for iso3, vote_str in country_votes.items():
                    country = country_lookup.get(iso3.upper())
                    if not country:
                        continue
                    if (obj.pk, country.pk) in existing_pairs:
                        continue
                    new_vote_objs.append(
                        UNVote(resolution=obj, country=country, vote=vote_str)
                    )
                if new_vote_objs:
                    with transaction.atomic():
                        UNVote.objects.bulk_create(
                            new_vote_objs, ignore_conflicts=True
                        )
                    created_votes += len(new_vote_objs)

            # Queue an ordered enrich → classify chain.  Classification reads
            # the plain-English explanation that enrichment generates, so the
            # two MUST run in sequence (firing them in parallel makes classify
            # run before the explanation exists → "no_explanation" skips).
            chain(
                enrich_resolution_with_ai.si(obj.pk),
                classify_resolution_to_event.si(obj.pk, threshold=0.7),
            ).apply_async()

    logger.info(
        "fetch_new_un_votes: created=%d updated=%d votes=%d (lookback=%d days)",
        created_res, updated_res, created_votes, lookback_days,
    )
    return {
        "created_resolutions": created_res,
        "updated_resolutions": updated_res,
        "created_votes":       created_votes,
        "lookback_days":       lookback_days,
    }

CLASSIFY_RESOLUTION_PROMPT = """\
You are a senior UN geopolitical analyst.

Your task is to determine whether a UN resolution DIRECTLY belongs to one of \
the known geopolitical events listed below.

KNOWN GEOPOLITICAL EVENTS:
{event_list}

UN RESOLUTION TO CLASSIFY:
Symbol      : {un_symbol}
Title       : {title}
Explanation : {explanation}
AI Tags     : {ai_tags}
Vote Date   : {vote_date}

INSTRUCTIONS:
- A resolution matches an event ONLY if it directly addresses, is a response \
to, or is explicitly caused by that specific geopolitical event.
- Loose thematic overlap (e.g. both involve sanctions, or both involve the \
Middle East) is NOT enough — you must see a direct causal or substantive link.
- If the resolution is routine, procedural, or covers a topic not specifically \
represented in the event list, set event_id to null.
- IMPORTANT: It is correct and expected to return null for many resolutions. \
Do NOT force a match just because an event is the "closest" option. When in \
doubt, return null.
- Confidence scoring — only assign ≥0.7 if the link is unambiguous:
    0.9–1.0 = resolution explicitly names or directly responds to this event
    0.7–0.89 = strong, specific, contextual link — not just shared theme
    0.5–0.69 = plausible but indirect — do NOT link at this level
    0.0–0.49 = weak, thematic, or unrelated — return null

Respond ONLY in this exact JSON format (no markdown, no extra text):
{{
  "event_id": <integer or null>,
  "event_title": "<matched event title or null>",
  "confidence": <float 0.0–1.0>,
  "reason": "<one sentence: what specific link exists, or why no match>"
}}"""


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def classify_resolution_to_event(self, resolution_id: int, threshold: float = 0.7):
    """
    Classify a UNResolution to a geopolitical Event using an LLM.

    The LLM receives the full list of known events and the resolution's
    explanation + AI tags, then returns:
      - event_id   : PK of the matched Event (or null)
      - confidence : float 0.0–1.0
      - reason     : one-sentence justification

    Only links the resolution if confidence >= threshold (default 0.5).

    On success: sets UNResolution.event_id and event_classified_at.
    On failure (no match, low confidence, LLM error): leaves fields unchanged.

    Safe to call both as a Celery task and directly in-process from
    the classify_resolutions management command.
    """
    from core.models import UNResolution, Event
    from django.utils import timezone

    _inline = not getattr(self.request, "id", None)  # True when called directly

    # ── Load resolution ───────────────────────────────────────────────────────
    try:
        res = UNResolution.objects.get(pk=resolution_id)
    except UNResolution.DoesNotExist:
        return {"skipped": True, "reason": "not_found"}

    # ── Build resolution text ─────────────────────────────────────────────────
    # Explanation is the richest signal — generated by AI in plain English.
    # Fall back to short_description if explanation not yet generated.
    explanation = (
        res.explanation.strip()
        if res.explanation
        else res.short_description.strip() if res.short_description
        else ""
    )

    if not explanation:
        logger.warning(
            "classify_resolution_to_event: no explanation for res %s — "
            "run enrich_un_resolutions first",
            resolution_id,
        )
        return {"linked": False, "reason": "no_explanation"}

    # ── Load all events ───────────────────────────────────────────────────────
    events = list(Event.objects.values("id", "title", "description", "start_date"))
    if not events:
        logger.warning("classify_resolution_to_event: no Events in DB")
        return {"linked": False, "reason": "no_events"}

    # ── Temporal filter: exclude events that started AFTER the vote date ───────
    # A resolution cannot be about an event that hadn't started yet.
    # Allow a 30-day buffer for events that were imminent at the time of the vote.
    from datetime import timedelta
    if res.vote_date:
        cutoff = res.vote_date + timedelta(days=30)
        eligible_events = [
            e for e in events
            if e["start_date"] is None or e["start_date"] <= cutoff
        ]
        excluded = len(events) - len(eligible_events)
        if excluded:
            logger.info(
                "classify_resolution_to_event: excluded %d future event(s) "
                "for res %s (vote_date=%s)",
                excluded, resolution_id, res.vote_date,
            )
        if not eligible_events:
            logger.info(
                "classify_resolution_to_event: no eligible events for res %s "
                "(all events started after vote_date=%s)",
                resolution_id, res.vote_date,
            )
            return {"linked": False, "reason": "no_eligible_events_for_vote_date"}
    else:
        eligible_events = events

    # Build the event list text for the prompt
    event_list_text = "\n".join([
        f"[ID:{e['id']}] {e['title']} — {(e['description'] or '')[:200]} (since {e['start_date']})"
        for e in eligible_events
    ])

    # ── Build prompt ──────────────────────────────────────────────────────────
    # Clean the title — strip letter/communication boilerplate
    import re
    clean_title = re.sub(
        r"^(Letter|Note verbale|Identical letters|Communication) dated.*?"
        r"(Secretary-General|United Nations)\.?\s*",
        "",
        res.title or "",
        flags=re.IGNORECASE | re.DOTALL,
    ).strip() or res.title or "N/A"

    prompt = CLASSIFY_RESOLUTION_PROMPT.format(
        event_list=event_list_text,
        un_symbol=res.un_symbol or "N/A",
        title=clean_title[:200],
        explanation=explanation[:600],
        ai_tags=", ".join(res.ai_tags) if res.ai_tags else "none",
        vote_date=res.vote_date or "N/A",
    )

    # ── LLM call ──────────────────────────────────────────────────────────────
    try:
        client = get_nvidia_client()
        response = client.chat.completions.create(
            model="meta/llama-3.3-70b-instruct",
            messages=[
                {"role": "system", "content": "Return only valid JSON. No markdown."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.1,   # low temp — deterministic classification
            max_tokens=256,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as exc:
        exc_str = str(exc)
        logger.error(
            "classify_resolution_to_event: LLM call failed for res %s: %s",
            resolution_id, exc_str,
        )

        # ── 429 Rate limit — exponential backoff ─────────────────────────────
            # NVIDIA NIM free tier allows ~10 RPM. Back off and retry with
            # increasing delays: 60s → 120s → 240s → 480s → 960s
        if "429" in exc_str or "Too Many Requests" in exc_str:
            retry_count   = self.request.retries          # 0-based
            backoff_delay = 60 * (2 ** retry_count)       # 60, 120, 240, 480, 960
            logger.warning(
                "Rate limited (429) on res %s — retry %d/%d in %ds",
                resolution_id, retry_count + 1, self.max_retries, backoff_delay,
            )
            if _inline:
                # Inline mode (management command) — just sleep and re-raise
                import time
                time.sleep(backoff_delay)
                raise
            raise self.retry(exc=exc, countdown=backoff_delay)

        # Other errors (network, timeout, etc.) — retry with fixed delay
        if _inline:
            raise
        raise self.retry(exc=exc, countdown=30)

    # ── Parse LLM response ────────────────────────────────────────────────────
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())

        matched_event_id = data.get("event_id")
        confidence       = float(data.get("confidence", 0.0))
        reason           = str(data.get("reason", "")).strip()
        matched_title    = str(data.get("event_title", "")).strip()

    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error(
            "classify_resolution_to_event: JSON parse failed for res %s: %s | raw=%r",
            resolution_id, exc, raw[:200],
        )
        return {"linked": False, "reason": "parse_error", "raw": raw[:200]}

    # ── Validate event_id exists in DB ────────────────────────────────────────
    if matched_event_id is not None:
        valid_ids = {e["id"] for e in eligible_events}
        if matched_event_id not in valid_ids:
            logger.warning(
                "classify_resolution_to_event: LLM returned unknown event_id=%s for res %s",
                matched_event_id, resolution_id,
            )
            matched_event_id = None

    # ── Apply threshold and save ──────────────────────────────────────────────
    logger.info(
        "Resolution %s → event_id=%s title=%r confidence=%.2f reason=%s",
        resolution_id, matched_event_id, matched_title, confidence, reason,
    )

    if matched_event_id and confidence >= threshold:
        UNResolution.objects.filter(pk=resolution_id).update(
            event_id=matched_event_id,
            event_classified_at=timezone.now(),
        )
        logger.info(
            "LINKED: Resolution %s (rcid=%s) → Event [%s] '%s' (confidence=%.2f)",
            resolution_id, res.rcid or res.un_symbol,
            matched_event_id, matched_title, confidence,
        )
        return {
            "linked":     True,
            "event_id":   matched_event_id,
            "confidence": confidence,
            "reason":     reason,
        }
    else:
        logger.info(
            "NO MATCH: Resolution %s confidence=%.2f < threshold=%.2f | reason=%s",
            resolution_id, confidence, threshold, reason,
        )
        return {
            "linked":         False,
            "confidence":     confidence,
            "threshold":      threshold,
            "matched_event":  matched_title or None,
            "reason":         reason,
        }


@shared_task
def bulk_classify_resolutions(force: bool = False, threshold: float = 0.7):
    """
    Queue classify_resolution_to_event for every unclassified UNResolution.
    A resolution is considered unclassified when event_id is NULL.
    event_classified_at is only set on successful linkage, so it cannot be
    used to skip already-attempted resolutions.
    Safe to call after a large import or on a weekly schedule.
    """
    from core.models import UNResolution

    qs = UNResolution.objects.all()
    if not force:
        qs = qs.filter(event_id__isnull=True)

    pks = list(qs.values_list("pk", flat=True))
    for pk in pks:
        classify_resolution_to_event.apply_async(
            args=[pk], kwargs={"threshold": threshold}
        )

    logger.info("bulk_classify_resolutions: queued %d tasks", len(pks))
    return {"queued": len(pks)}


@shared_task
def bulk_enrich_and_classify(threshold: float = 0.7, force: bool = False):
    """
    Orchestrator: for every resolution that still needs work, queue an ordered
    enrich → classify CHAIN so classification always runs against a freshly
    generated explanation.

    This is the single entry point used after a bulk import and by the daily
    fetch task.  Each resolution gets its own chain:

        enrich_resolution_with_ai(pk)  →  classify_resolution_to_event(pk)

    A resolution "needs work" when it is missing an explanation OR is not yet
    linked to an Event.  Already-enriched resolutions skip the enrich step
    (it returns early) but still flow into classification.

    Args:
        threshold: confidence cutoff for linking a resolution to an Event.
        force:     re-enrich + re-classify everything, even completed records.
    """
    from django.db.models import Q

    from core.models import UNResolution

    qs = UNResolution.objects.all()
    if not force:
        qs = qs.filter(
            Q(explanation_generated_at__isnull=True) | Q(event_id__isnull=True)
        )

    pks = list(qs.values_list("pk", flat=True))
    for pk in pks:
        chain(
            enrich_resolution_with_ai.si(pk, force=force),
            classify_resolution_to_event.si(pk, threshold=threshold),
        ).apply_async()

    logger.info("bulk_enrich_and_classify: queued %d enrich→classify chains", len(pks))
    return {"queued": len(pks), "threshold": threshold, "force": force}
# ─────────────────────────────────────────────────────────────────────────────
# UN Resolution AI enrichment  (Layer 3: ai_tags + plain-English explanation)
# ─────────────────────────────────────────────────────────────────────────────

# ENRICH_UN_PROMPT = """\
# You are a UN policy analyst. Given the following UN resolution details, do two things:

# 1. Generate a list of 3–8 concise semantic tags (single words or short phrases) that
#    best describe what this resolution is about. Focus on the policy substance, not the
#    procedural category. Examples: "peacekeeping", "arms embargo", "nuclear non-proliferation",
#    "humanitarian corridors", "sanctions relief", "climate finance".

# 2. Write a single plain-English paragraph (2–4 sentences) that explains what this
#    resolution does and why it matters — as if briefing a non-expert analyst.

# Resolution ID   : {rcid}
# UN Symbol       : {un_symbol}
# Session         : {session}
# Vote date       : {vote_date}
# Title / Res ID  : {title}
# Description     : {description}
# Existing tags   : {existing_tags}

# Respond ONLY in this exact JSON format (no markdown, no prose outside JSON):
# {{
#   "ai_tags": ["tag1", "tag2", "tag3"],
#   "explanation": "Plain-English paragraph here."
# }}"""

ENRICH_UN_PROMPT = """\
You are a UN policy analyst. Given the following UN resolution details, do three things:

1. Generate a list of 4-8 concise semantic tags (single words or short phrases) that
   best describe what this resolution is about. Focus on the policy substance.
   Examples: "peacekeeping", "arms embargo", "nuclear non-proliferation",
   "sanctions relief", "economic coercion", "unilateral measures".

2. Write a single plain-English paragraph (2-4 sentences) that explains what this
   resolution does and why it matters. Include the specific countries, conflicts,
   or geopolitical tensions it relates to by name.

3. Name the broader geopolitical conflict or crisis this resolution belongs to
   (e.g. "Russia-Ukraine War", "US-Iran nuclear standoff", "US-Israel-Iran War", "Israel-Gaza conflict").
   If it spans multiple conflicts, name all of them.

Resolution ID   : {rcid}
UN Symbol       : {un_symbol}
Session         : {session}
Vote date       : {vote_date}
Title / Res ID  : {title}
Description     : {description}
Existing tags   : {existing_tags}

Respond ONLY in this exact JSON format (no markdown, no prose outside JSON):
{{
  "ai_tags": ["tag1", "tag2", "tag3"],
  "explanation": "Plain-English paragraph here, naming specific countries and conflicts.",
  "parent_conflicts": ["Conflict name 1", "Conflict name 2"]
}}"""


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def enrich_resolution_with_ai(self, resolution_id: int, force: bool = False):
    """
    Generate Layer-3 AI tags and a plain-English explanation for a UNResolution.

    Uses NVIDIA NIM (Llama-3.3-70b-instruct) — same free-tier API used throughout.

    Args:
        resolution_id: PK of the UNResolution to enrich.
        force: If True, re-run even if explanation_generated_at is already set.

    Returns:
        dict with keys: skipped, ai_tags, explanation
    """
    from core.models import UNResolution
    from django.utils import timezone

    try:
        res = UNResolution.objects.get(pk=resolution_id)
    except UNResolution.DoesNotExist:
        logger.warning("enrich_resolution_with_ai: UNResolution %s not found", resolution_id)
        return {"skipped": True, "reason": "not_found"}

    if not force and res.explanation_generated_at is not None:
        logger.debug("UNResolution %s already enriched — skipping", resolution_id)
        return {"skipped": True, "reason": "already_enriched"}

    existing_tags = list(set((res.topic_tags or []) + (res.symbol_tags or [])))

    # Prefer the richer UNDL abstract; fall back to Voeten short_description
    description_text = (
        res.resolution_text[:600] if res.resolution_text
        else res.short_description[:400] if res.short_description
        else "N/A"
    )

    prompt = ENRICH_UN_PROMPT.format(
        rcid=res.rcid or "N/A",
        un_symbol=res.un_symbol or "N/A",
        session=res.session or "N/A",
        vote_date=res.vote_date,
        title=res.title or res.short_description[:120],
        description=description_text,
        existing_tags=", ".join(existing_tags) if existing_tags else "none",
    )

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.getenv("NVIDIA_NIM_API_KEY"),
        )
        response = client.chat.completions.create(
            model="meta/llama-3.3-70b-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=512,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("enrich_resolution_with_ai: LLM call failed for %s: %s", resolution_id, exc)
        raise self.retry(exc=exc)

    # Parse JSON response
    try:
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        ai_tags     = [str(t).strip() for t in data.get("ai_tags", []) if str(t).strip()]
        explanation = str(data.get("explanation", "")).strip()
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.error(
            "enrich_resolution_with_ai: JSON parse failed for resolution %s: %s | raw=%r",
            resolution_id, exc, raw[:200],
        )
        return {"skipped": True, "reason": "parse_error", "raw": raw[:200]}

    UNResolution.objects.filter(pk=resolution_id).update(
        ai_tags=ai_tags,
        explanation=explanation,
        explanation_generated_at=timezone.now(),
    )

    logger.info(
        "Enriched UNResolution %s (rcid=%s): %d AI tags, explanation %d chars",
        resolution_id, res.rcid, len(ai_tags), len(explanation),
    )
    return {"skipped": False, "ai_tags": ai_tags, "explanation": explanation[:120]}


@shared_task
def bulk_enrich_un_resolutions(force: bool = False, batch_size: int = 200):
    """
    Queue enrich_resolution_with_ai for every UNResolution not yet enriched.
    Safe to run as a one-off or on a weekly Celery Beat schedule.

    Args:
        force: Re-enrich even resolutions that already have explanations.
        batch_size: Celery chord chunk size (controls parallelism).
    """
    from core.models import UNResolution

    qs = UNResolution.objects.all()
    if not force:
        qs = qs.filter(explanation_generated_at__isnull=True)

    pks = list(qs.values_list("pk", flat=True))
    total = len(pks)
    logger.info("bulk_enrich_un_resolutions: queuing %d resolutions (force=%s)", total, force)

    queued = 0
    for pk in pks:
        enrich_resolution_with_ai.apply_async(args=[pk], kwargs={"force": force})
        queued += 1

    return {"queued": queued, "total_in_db": total}


# ─────────────────────────────────────────────────────────────────────────────
# Vote explanation enrichment — UN Meetings Coverage press releases
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def enrich_resolution_vote_explanations(self, resolution_id: int, force: bool = False):
    """
    Fetch and store per-country vote explanations for a single UNResolution.

    Pipeline:
      1. Skip if resolution has no votes or no meeting_record_symbol.
      2. If press_release_code not yet stored: look up the PV record on UNDL
         (Playwright search) and extract 993$a code, then save it.
      3. Fetch press.un.org press release (Playwright) and parse per-country
         paragraphs via _parse_press_release_explanations.
      4. Match paragraphs to UNVote rows by ISO-3 code and save explanation.

    Returns dict with keys: status, code, updated.
    """
    from core.models import UNResolution, UNVote
    from core.ingestion.un_library import (
        fetch_press_release_code,
        fetch_vote_explanations,
    )

    try:
        res = UNResolution.objects.get(pk=resolution_id)
    except UNResolution.DoesNotExist:
        return {"status": "not_found"}

    # Only enrich resolutions that had a recorded vote
    if not res.votes.exists():
        return {"status": "no_votes"}

    if not force and res.votes.filter(explanation__gt="").exists():
        return {"status": "already_done"}

    # ── Step 1: resolve press_release_code ───────────────────────────────────
    if not res.press_release_code:
        if not res.meeting_record_symbol:
            logger.debug(
                "enrich_resolution_vote_explanations: pk=%d has no meeting_record_symbol",
                resolution_id,
            )
            return {"status": "no_meeting_record"}

        code = fetch_press_release_code(res.meeting_record_symbol)
        if not code:
            return {"status": "no_press_release_code"}

        res.press_release_code = code
        res.save(update_fields=["press_release_code"])

    # ── Step 2: fetch and parse press release ────────────────────────────────
    explanations = fetch_vote_explanations(res.press_release_code, res.vote_date.year)
    if not explanations:
        logger.info(
            "enrich_resolution_vote_explanations: pk=%d code=%s — no explanations extracted",
            resolution_id, res.press_release_code,
        )
        return {"status": "no_explanations", "code": res.press_release_code}

    # ── Step 3: match to UNVote rows and save ───────────────────────────────
    updated = 0
    for vote in res.votes.select_related("country").all():
        iso3 = vote.country.isoa3_code.upper()
        text = explanations.get(iso3, "")
        if text and (force or not vote.explanation):
            vote.explanation = text
            vote.save(update_fields=["explanation"])
            updated += 1

    logger.info(
        "enrich_resolution_vote_explanations: pk=%d %r → %d/%d votes explained",
        resolution_id, res.un_symbol, updated, res.votes.count(),
    )
    return {
        "status": "ok",
        "code": res.press_release_code,
        "countries_in_release": len(explanations),
        "updated": updated,
    }


@shared_task
def bulk_enrich_vote_explanations(force: bool = False):
    """
    Queue enrich_resolution_vote_explanations for every UNResolution that:
      - Has at least one vote
      - Has a meeting_record_symbol (i.e. was imported via the new pipeline)
      - Has not yet had explanations fetched (unless force=True)
    """
    from core.models import UNResolution

    qs = UNResolution.objects.filter(
        meeting_record_symbol__gt="",
        votes__isnull=False,
    ).distinct()

    if not force:
        qs = qs.exclude(votes__explanation__gt="")

    pks = list(qs.values_list("pk", flat=True))
    logger.info("bulk_enrich_vote_explanations: queuing %d resolutions", len(pks))

    for pk in pks:
        enrich_resolution_vote_explanations.apply_async(
            args=[pk], kwargs={"force": force}
        )

    return {"queued": len(pks)}


# ─────────────────────────────────────────────────────────────────────────────
# Summary regeneration  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def regenerate_summary_task(country_id: int, event_id: int):
    """
    Regenerate the AI summary for a country×event pair (debounced via Redis).
    """
    lock_key = f"summary_lock:{country_id}:{event_id}"

    if redis_client.exists(lock_key):
        logger.info("Summary regeneration skipped (debounced): %s", lock_key)
        return {"skipped": True, "reason": "debounced"}

    redis_client.setex(lock_key, SUMMARY_LOCK_TTL, "1")

    try:
        country = Country.objects.get(id=country_id)
        event   = Event.objects.get(id=event_id)
        regenerate_country_event_summary(country, event)
        logger.info("Summary regenerated: %s | %s", country.name, event.title)
        return {"success": True}

    except Exception as exc:
        logger.error("Summary regeneration failed: %s", str(exc))
        return {"success": False, "error": str(exc)}
