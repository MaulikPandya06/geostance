"""
Management command: import_un_resolutions
==========================================
Backfill UN General Assembly voting records from the UN Digital Library (UNDL).
The UNDL is the official UN repository and is updated within days of each vote.

What gets imported
------------------
  UNResolution  — one per resolution: symbol, title, vote date, session,
                  resolution_text (explanation of the vote from UNDL abstract),
                  topic tags from UNDL subject keywords, symbol-derived tags.
  UNVote        — one per (resolution × country): who voted how.
  Event link    — each resolution is classified against the Events table via
                  semantic similarity; event=null if no match found.

Workflow
--------
  1. Fetch MARC21 XML records from UNDL via OAI-PMH (one continuous range,
     quarterly chunked to avoid 503s).
  2. Group incoming records by vote_date.year for per-year stats.
     ⚠️  OAI-PMH date filters use the *catalog indexing date*, not the vote date.
     GA resolutions voted in Nov–Dec are often indexed in UNDL the following
     Jan–Feb.  This command fetches from {from_year}-01-01 to today so all
     records are captured regardless of indexing lag.
  3. Upsert UNResolution (create or update existing by un_symbol).
  4. Scrape individual record page for country-level vote breakdown.
  5. Bulk-create UNVote rows.
  6. Queue: AI enrichment  +  event classification  (Celery, non-blocking).

Usage
-----
  # Full 15-year backfill (2010 → current year)
  python manage.py import_un_resolutions

  # Specific year range
  python manage.py import_un_resolutions --from-year 2020 --to-year 2024

  # Skip country-vote scraping (metadata only — much faster)
  python manage.py import_un_resolutions --skip-country-votes

  # Skip Celery; run AI enrichment + classification inline (slow but self-contained)
  python manage.py import_un_resolutions --from-year 2024 --inline-classify

  # Dry-run: log what would be imported without writing to DB
  python manage.py import_un_resolutions --from-year 2024 --dry-run
"""

import time
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from core.ingestion.un_library import (
    ResolutionRecord,
    fetch_country_votes,
    fetch_votes_for_year,
    normalize_un_symbol,
    search_resolutions,
    search_resolutions_by_year_website,
    tags_from_symbol,
)
from core.models import Country, UNResolution, UNVote

SCRAPE_DELAY = 1.5   # seconds between individual-record scrape requests


class Command(BaseCommand):
    help = (
        "Import UN voting records from the UN Digital Library (UNDL). "
        "Covers all years back to 2010 (15-year default window)."
    )

    def add_arguments(self, parser):
        current_year = date.today().year
        parser.add_argument(
            "--from-year", type=int, default=current_year - 15,
            help="Start year for backfill (default: 15 years ago).",
        )
        parser.add_argument(
            "--to-year", type=int, default=current_year,
            help="End year for backfill (default: current year).",
        )
        parser.add_argument(
            "--skip-country-votes", action="store_true", default=False,
            help=(
                "Only import resolution metadata; skip the per-record HTML scrape "
                "that retrieves individual country vote data. Runs 10x faster."
            ),
        )
        parser.add_argument(
            "--batch-size", type=int, default=200,
            help="DB bulk-create batch size (default: 200).",
        )
        parser.add_argument(
            "--inline-classify", action="store_true", default=False,
            help=(
                "Run event classification inline after import instead of queuing "
                "as a Celery task. Slower but works without a running Celery worker."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Fetch and parse data but do NOT write to the database.",
        )
        parser.add_argument(
            "--no-ai-enrich", action="store_true", default=False,
            help="Skip queuing AI enrichment tasks after import.",
        )
        parser.add_argument(
            "--voting-set-only", action="store_true", default=False,
            help=(
                "Restrict the harvest to the UNDL 'Voting Data' OAI set "
                "(roll-call votes only).  Faster, but MISSES resolutions "
                "adopted by consensus.  By default the full UNDL catalog is "
                "queried so every RES-symbol resolution is captured."
            ),
        )
        parser.add_argument(
            "--oai-from", type=str, default=None,
            help=(
                "Override the OAI-PMH harvest start date (YYYY-MM-DD). "
                "Default: {from_year}-01-01.  Use a wider range (e.g. the "
                "previous July) when resolutions are missing because UNDL "
                "indexed them earlier than expected."
            ),
        )
        parser.add_argument(
            "--oai-until", type=str, default=None,
            help=(
                "Override the OAI-PMH harvest end date (YYYY-MM-DD). "
                "Default: today."
            ),
        )
        parser.add_argument(
            "--website-discovery", action="store_true", default=False,
            help=(
                "Use Playwright to discover record IDs from the UNDL website "
                "(year-facet search), then fetch full MARC21 via OAI-PMH GetRecord. "
                "More reliable than OAI ListRecords for year-specific imports because "
                "the website Solr index is updated faster than OAI-PMH datestamps. "
                "Requires: playwright install chromium. "
                "Works best when --from-year == --to-year (single year target)."
            ),
        )

    # ── Main handler ──────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        from_year        = options["from_year"]
        to_year          = options["to_year"]
        skip_votes       = options["skip_country_votes"]
        batch_size       = options["batch_size"]
        inline_classify  = options["inline_classify"]
        dry_run          = options["dry_run"]
        no_ai_enrich     = options["no_ai_enrich"]
        voting_set_only  = options["voting_set_only"]
        use_voting_set   = voting_set_only
        oai_from_override  = options.get("oai_from")
        oai_until_override = options.get("oai_until")
        website_discovery  = options["website_discovery"]

        website_discovery = options["website_discovery"]

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "[DRY RUN] No data will be written to the database."
            ))

        # OAI-PMH filters by indexing date, not vote date.  GA resolutions voted
        # in Nov–Dec are indexed by UNDL in the following Jan–Feb, so we must
        # fetch from from_year right up to today in one continuous pass, then
        # group by vote_date.year for per-year stats.
        fetch_from  = oai_from_override  or f"{from_year}-01-01"
        fetch_until = oai_until_override or date.today().isoformat()

        if website_discovery:
            set_note = "Playwright website discovery + OAI-PMH GetRecord"
        elif voting_set_only:
            set_note = "Voting Data set only (roll-call votes)"
        else:
            set_note = "full UNDL catalog (all RES symbols)"

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== UN Resolution Import — UNDL ==="
            f"\n  Vote years : {from_year}–{to_year}"
            f"\n  OAI range  : {fetch_from} → {fetch_until}"
            f"\n  Scope      : {set_note}"
        ))
        if voting_set_only:
            self.stdout.write(self.style.WARNING(
                "  --voting-set-only: roll-call votes ONLY; consensus resolutions will be skipped."
            ))
        # Pre-load lookups
        country_lookup: dict[str, Country] = {
            c.isoa3_code.upper(): c
            for c in Country.objects.all()
        }
        self.stdout.write(f"  Countries in DB: {len(country_lookup)}")

        existing_symbols: set[str] = set(
            UNResolution.objects.exclude(un_symbol="")
            .values_list("un_symbol", flat=True)
        )
        existing_vote_pairs: set[tuple] = set(
            UNVote.objects.values_list("resolution_id", "country_id")
        )

        # Per-year accumulators — keyed by vote_date.year
        # Structure: {year: [created, updated, votes]}
        year_stats: dict[int, list[int]] = {}

        def _stats(yr: int) -> list[int]:
            if yr not in year_stats:
                year_stats[yr] = [0, 0, 0]   # [created, updated, votes]
            return year_stats[yr]

        total_votes = 0
        new_votes: list[UNVote] = []

        # ── Choose record source ───────────────────────────────────────────────
        # website_discovery: Playwright finds IDs from UNDL Solr → GetRecord MARC21.
        #   More reliable for targeted year imports; Solr is ahead of OAI-PMH.
        # OAI ListRecords: fast for multi-year backfills; may miss records whose
        #   OAI datestamp doesn't match the vote year.
        if website_discovery:
            if from_year != to_year:
                self.stdout.write(self.style.WARNING(
                    "  --website-discovery works best for a single year "
                    f"(--from-year == --to-year). Running year by year for "
                    f"{from_year}–{to_year}."
                ))

            def _website_record_source():
                for yr in range(from_year, to_year + 1):
                    self.stdout.write(f"  Playwright discovery: year={yr}")
                    yield from search_resolutions_by_year_website(yr)

            record_source = _website_record_source()
        else:
            record_source = search_resolutions(
                fetch_from, fetch_until, use_voting_set=use_voting_set
            )

        # ── Pre-fetch all votes for target years (website-discovery mode) ──────
        # In website-discovery mode, we bulk-fetch all Voting Data records for
        # each year in one Playwright pass, building a {symbol: {iso3: vote}} dict.
        # This avoids one Playwright search per resolution (which would be very slow).
        bulk_votes: dict[str, dict[str, str]] = {}
        if website_discovery and not skip_votes and not dry_run:
            for yr in range(from_year, to_year + 1):
                self.stdout.write(f"  Fetching Voting Data records: year={yr}")
                year_votes = fetch_votes_for_year(yr)
                bulk_votes.update(year_votes)
                self.stdout.write(
                    f"  Voting Data: {len(year_votes)} resolutions with votes for {yr}"
                )

        for rec in record_source:
            # Bucket by vote_date year; skip records outside the requested range
            vote_year = rec.vote_date.year if rec.vote_date else None
            if vote_year is None or vote_year < from_year or vote_year > to_year:
                continue

            symbol = rec.un_symbol
            is_new = symbol not in existing_symbols

            if dry_run:
                if rec.vote_date:
                    if is_new:
                        existing_symbols.add(symbol)
                        _stats(vote_year)[0] += 1
                        self.stdout.write(
                            f"  [{vote_year}] would create {rec.un_symbol!r}"
                            f" — {rec.title[:55]}"
                        )
                    else:
                        _stats(vote_year)[1] += 1
                        self.stdout.write(
                            f"  [{vote_year}] would update {rec.un_symbol!r}"
                            f" — {rec.title[:55]}"
                        )
                continue

            resolution_obj = self._upsert_resolution(rec)
            if resolution_obj is None:
                continue

            if is_new:
                existing_symbols.add(symbol)
                _stats(vote_year)[0] += 1
            else:
                _stats(vote_year)[1] += 1

            if skip_votes:
                continue

            # Vote lookup: prefer bulk pre-fetched dict, then per-resolution search
            symbol_key = rec.un_symbol
            if bulk_votes:
                country_votes = bulk_votes.get(symbol_key, {})
            elif rec.country_votes:
                country_votes = rec.country_votes
            else:
                country_votes = fetch_country_votes(rec.undl_id, un_symbol=symbol_key)
                if country_votes:
                    time.sleep(SCRAPE_DELAY)

            for iso3, vote_str in country_votes.items():
                country = country_lookup.get(iso3.upper())
                if not country:
                    continue
                pair = (resolution_obj.pk, country.pk)
                if pair in existing_vote_pairs:
                    continue
                new_votes.append(UNVote(
                    resolution=resolution_obj,
                    country=country,
                    vote=vote_str,
                ))
                existing_vote_pairs.add(pair)
                _stats(vote_year)[2] += 1
                total_votes += 1

            if len(new_votes) >= batch_size * 5:
                self._flush_votes(new_votes, batch_size)
                new_votes = []

        if new_votes and not dry_run:
            self._flush_votes(new_votes, batch_size)

        # ── Per-year summary ──────────────────────────────────────────────────
        total_created = total_updated = 0
        for yr in sorted(year_stats):
            created, updated, votes = year_stats[yr]
            total_created += created
            total_updated += updated
            if dry_run:
                self.stdout.write(
                    f"\n  ── {yr} ──"
                    f"\n    would create={created:,}  would update={updated:,}"
                )
            else:
                self.stdout.write(
                    f"\n  ── {yr} ──"
                    f"\n    created={created:,}  updated={updated:,}  votes={votes:,}"
                )

        # ── Grand total ───────────────────────────────────────────────────────
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n=== Dry-run complete (no data written) ==="
                f"\n  Would create : {total_created:,}"
                f"\n  Would update : {total_updated:,}"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n=== Import complete ==="
                f"\n  Resolutions created : {total_created:,}"
                f"\n  Resolutions updated : {total_updated:,}"
                f"\n  Votes created       : {total_votes:,}"
            ))

        if not dry_run:
            self._post_import(
                inline_classify=inline_classify,
                no_ai_enrich=no_ai_enrich,
            )

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _upsert_resolution(self, rec: ResolutionRecord) -> UNResolution | None:
        """
        Create a new UNResolution or update an existing one by un_symbol.
        Returns the model instance, or None on error.
        """
        if not rec.vote_date:
            return None

        symbol_tags = tags_from_symbol(rec.un_symbol)

        defaults = dict(
            undl_id=rec.undl_id,
            title=rec.title or "",
            vote_date=rec.vote_date,
            session=rec.session,
            body=rec.body,
            short_description=rec.short_description or "",   # 520$a — UNDL abstract
            resolution_text=rec.resolution_text or "",       # 995$a — action summary
            topic_tags=rec.topic_tags,
            symbol_tags=symbol_tags,
            meeting_record_symbol=rec.meeting_record_symbol or "",
        )

        try:
            with transaction.atomic():
                if rec.un_symbol:
                    obj, created = UNResolution.objects.update_or_create(
                        un_symbol=rec.un_symbol,
                        defaults=defaults,
                    )
                else:
                    # No symbol — create only if undl_id is new
                    obj, created = UNResolution.objects.get_or_create(
                        undl_id=rec.undl_id,
                        defaults={**defaults, "un_symbol": ""},
                    )
            return obj
        except Exception as exc:
            self.stdout.write(
                self.style.ERROR(
                    f"    DB error for {rec.un_symbol!r}: {exc}"
                )
            )
            return None

    @staticmethod
    def _flush_votes(votes: list, batch_size: int) -> int:
        with transaction.atomic():
            UNVote.objects.bulk_create(
                votes, batch_size=batch_size, ignore_conflicts=True
            )
        return len(votes)

    # ── Post-import tasks ─────────────────────────────────────────────────────

    def _post_import(self, inline_classify: bool, no_ai_enrich: bool) -> None:
        """
        Run the two post-import stages in the correct order:
            1. enrich_resolution_with_ai   (LLM: ai_tags + explanation)
            2. classify_resolution_to_event (LLM: link to Event if conf >= 0.7)

        Classification reads the explanation that enrichment produces, so the
        stages MUST be ordered.  In Celery mode this is guaranteed by the
        per-resolution enrich→classify chain inside bulk_enrich_and_classify.
        In inline mode we enrich-then-classify each resolution in a loop.
        """
        threshold = 0.7

        # ── Inline mode: no Celery needed, ordered per-resolution ──────────────
        if inline_classify:
            from core.tasks import (
                classify_resolution_to_event,
                enrich_resolution_with_ai,
            )

            qs = UNResolution.objects.filter(event__isnull=True)
            total = qs.count()
            self.stdout.write(
                f"\n  Running inline enrich → classify on {total:,} resolutions…"
            )
            enriched = classified = failed = 0
            for res in qs.iterator():
                try:
                    if not no_ai_enrich:
                        enrich_resolution_with_ai(res.pk)
                        enriched += 1
                    result = classify_resolution_to_event(res.pk, threshold=threshold)
                    if result.get("linked"):
                        classified += 1
                except Exception as exc:
                    failed += 1
                    self.stdout.write(self.style.ERROR(
                        f"    pk={res.pk} failed: {exc}"
                    ))
            self.stdout.write(self.style.SUCCESS(
                f"  Inline done: enriched={enriched:,} linked={classified:,} "
                f"failed={failed:,}"
            ))
            return

        # ── Celery mode: queue one ordered chain per resolution ────────────────
        try:
            if no_ai_enrich:
                from core.tasks import bulk_classify_resolutions
                task = bulk_classify_resolutions.delay(threshold=threshold)
                self.stdout.write(
                    f"\n  Queued classification (enrichment skipped): {task.id}"
                )
            else:
                from core.tasks import bulk_enrich_and_classify
                task = bulk_enrich_and_classify.delay(threshold=threshold)
                self.stdout.write(
                    f"\n  Queued enrich → classify chains: {task.id}"
                )
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  Could not queue post-import tasks (Celery unavailable?): {exc}"
                    "\n  Run manually (in order):"
                    "\n    python manage.py enrich_un_resolutions --inline"
                    "\n    python manage.py classify_resolutions --inline"
                )
            )
