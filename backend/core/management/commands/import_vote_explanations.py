"""
Management command: import_vote_explanations
=============================================
Fetch UN Meetings Coverage press release explanations for every resolution
that had a recorded vote and store them on the corresponding UNVote rows.

For each qualifying UNResolution this command:
  1. Looks up the PV record (S/PV.NNNNN) on UNDL via Playwright and reads the
     993$a press release code (e.g. "SC/16274").
  2. Fetches press.un.org/{year}/{prefix}{number}.doc.htm via Playwright.
  3. Parses per-country paragraphs and matches them to UNVote rows by ISO-3.
  4. Saves UNVote.explanation.

Usage
-----
  # All resolutions with votes that don't have explanations yet
  python manage.py import_vote_explanations

  # Specific year only
  python manage.py import_vote_explanations --year 2026

  # Specific resolution symbol
  python manage.py import_vote_explanations --symbol "S/RES/2812 (2026)"

  # Re-fetch even if already done
  python manage.py import_vote_explanations --year 2026 --force

  # Run inline (no Celery needed)
  python manage.py import_vote_explanations --year 2026 --inline

  # Dry-run: show what would be fetched, don't write to DB
  python manage.py import_vote_explanations --year 2026 --dry-run
"""

import time

from django.core.management.base import BaseCommand

from core.ingestion.un_library import fetch_press_release_code, fetch_vote_explanations
from core.models import UNResolution

POLITE_DELAY = 3.0   # seconds between press.un.org requests


class Command(BaseCommand):
    help = "Fetch UN Meetings Coverage vote explanations and store on UNVote rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year", type=int, default=None,
            help="Restrict to resolutions voted in this year.",
        )
        parser.add_argument(
            "--symbol", type=str, default=None,
            help="Process a single resolution by UN symbol (e.g. 'S/RES/2812 (2026)').",
        )
        parser.add_argument(
            "--force", action="store_true", default=False,
            help="Re-fetch even if vote explanations already exist.",
        )
        parser.add_argument(
            "--inline", action="store_true", default=False,
            help=(
                "Run synchronously in this process (no Celery). "
                "Default queues one Celery task per resolution."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Print which resolutions would be processed; don't write to DB.",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Stop after processing N resolutions (useful for testing).",
        )

    def handle(self, *args, **options):
        year     = options["year"]
        symbol   = options["symbol"]
        force    = options["force"]
        inline   = options["inline"]
        dry_run  = options["dry_run"]
        limit    = options["limit"]

        if dry_run:
            self.stdout.write(self.style.WARNING("[DRY RUN] Nothing will be written."))

        # ── Build queryset ────────────────────────────────────────────────────
        qs = UNResolution.objects.filter(
            meeting_record_symbol__gt="",  # only resolutions imported via new pipeline
            votes__isnull=False,
        ).distinct()

        if year:
            qs = qs.filter(vote_date__year=year)
        if symbol:
            qs = qs.filter(un_symbol=symbol)
        if not force:
            qs = qs.exclude(votes__explanation__gt="")

        total = qs.count()
        self.stdout.write(
            f"=== Vote Explanation Import ==="
            f"\n  Resolutions to process : {total}"
            f"\n  Year filter            : {year or 'all'}"
            f"\n  Symbol filter          : {symbol or 'all'}"
            f"\n  Force re-fetch         : {force}"
            f"\n  Mode                   : {'inline' if inline else 'Celery'}"
        )

        if dry_run:
            for res in qs[:limit or 999]:
                self.stdout.write(
                    f"  would process: {res.un_symbol!r}"
                    f" meeting={res.meeting_record_symbol!r}"
                )
            return

        if not inline:
            from core.tasks import enrich_resolution_vote_explanations
            queued = 0
            for res in qs[:limit or 99999]:
                enrich_resolution_vote_explanations.apply_async(
                    args=[res.pk], kwargs={"force": force}
                )
                queued += 1
            self.stdout.write(self.style.SUCCESS(f"\nQueued {queued} Celery tasks."))
            return

        # ── Inline mode ───────────────────────────────────────────────────────
        processed = ok = no_code = no_press = 0

        for res in qs[:limit or 99999]:
            processed += 1
            self.stdout.write(
                f"\n[{processed}/{total}] {res.un_symbol!r} "
                f"({res.vote_date}) meeting={res.meeting_record_symbol!r}"
            )

            # Step 1: get press release code
            if not res.press_release_code:
                code = fetch_press_release_code(res.meeting_record_symbol)
                if not code:
                    self.stdout.write(f"  ✗ no press release code found")
                    no_code += 1
                    continue
                res.press_release_code = code
                res.save(update_fields=["press_release_code"])
                self.stdout.write(f"  press_release_code = {code}")
            else:
                self.stdout.write(f"  press_release_code = {res.press_release_code} (cached)")

            # Step 2: fetch and parse press release
            explanations = fetch_vote_explanations(
                res.press_release_code, res.vote_date.year
            )
            if not explanations:
                self.stdout.write(f"  ✗ no explanations extracted from press release")
                no_press += 1
                time.sleep(POLITE_DELAY)
                continue

            self.stdout.write(
                f"  ✓ {len(explanations)} countries found in press release"
            )

            # Step 3: match to UNVote and save
            updated = 0
            for vote in res.votes.select_related("country").all():
                iso3 = vote.country.isoa3_code.upper()
                text = explanations.get(iso3, "")
                if text and (force or not vote.explanation):
                    vote.explanation = text
                    vote.save(update_fields=["explanation"])
                    updated += 1
                    self.stdout.write(
                        f"    {iso3}: {text[:80]}..."
                        if len(text) > 80 else f"    {iso3}: {text}"
                    )

            self.stdout.write(f"  Saved {updated} explanations.")
            ok += 1
            time.sleep(POLITE_DELAY)

        self.stdout.write(self.style.SUCCESS(
            f"\n=== Done ==="
            f"\n  Processed  : {processed}"
            f"\n  OK         : {ok}"
            f"\n  No code    : {no_code}"
            f"\n  No content : {no_press}"
        ))
