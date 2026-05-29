"""
Management command: historical_backfill
=========================================
One command to seed GeoStance with historical data from 2020 (or any date range).

TWO independent pipelines — run together or separately:

  1. GDELT (global news coverage of tracked events)
     → Queues one Celery task per month per event
     → Each task fetches up to 250 articles from GDELT DOC API

  2. Government archive scrapers (official government press releases)
     → Queues one Celery task per source
     → Scrapers: mea, state-dept, un-news, china-mfa, russia-mfa, uk-fcdo

Usage examples:
  # Full backfill from 2020 — GDELT + all gov scrapers
  python manage.py historical_backfill --start-date 2020-01-01

  # GDELT only, for a specific event
  python manage.py historical_backfill --start-date 2020-01-01 --source gdelt --event-id 1

  # India MEA official statements from 2022
  python manage.py historical_backfill --start-date 2022-01-01 --source mea

  # All gov scrapers, last 2 years
  python manage.py historical_backfill --start-date 2023-01-01 --source gov

  # Dry run — see what would be queued without actually queuing
  python manage.py historical_backfill --start-date 2020-01-01 --dry-run

Notes:
  - Celery workers must be running for tasks to execute
  - For a demo/quick test without Celery, use --run-inline
    (runs synchronously in this process, slower but needs no worker)
  - GDELT queues one task per (event × month) — safe, small, retryable
  - Gov scrapers queue one task per source — can run for hours if range is large
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from core.ingestion.gov_scrapers import SCRAPERS
from core.models import Event


def _iter_months(start: date, end: date):
    """Yield (year, month) tuples for every month in [start, end]."""
    current = start.replace(day=1)
    while current <= end:
        yield current.year, current.month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)


class Command(BaseCommand):
    help = (
        "Backfill GeoStance with historical diplomatic statements from 2020 onwards. "
        "Uses GDELT (global) and/or government archive scrapers (primary sources)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--start-date",
            required=True,
            type=str,
            help="Start of backfill window (ISO format: YYYY-MM-DD). E.g. 2020-01-01",
        )
        parser.add_argument(
            "--end-date",
            type=str,
            default=None,
            help="End of backfill window (default: today). E.g. 2024-12-31",
        )
        parser.add_argument(
            "--source",
            type=str,
            default="all",
            choices=["all", "gdelt", "gov"] + list(SCRAPERS.keys()),
            help=(
                "Which pipeline(s) to run:\n"
                "  all        → GDELT + all gov scrapers (default)\n"
                "  gdelt      → GDELT month-by-month for every event\n"
                "  gov        → All government archive scrapers\n"
                "  mea        → India Ministry of External Affairs only\n"
                "  state-dept → US State Department only\n"
                "  un-news    → UN News archive only\n"
                "  china-mfa  → China MFA only\n"
                "  russia-mfa → Russia MFA only\n"
                "  uk-fcdo    → UK FCDO only\n"
            ),
        )
        parser.add_argument(
            "--event-id",
            type=int,
            default=None,
            help="Limit GDELT backfill to a single Event ID (default: all events).",
        )
        parser.add_argument(
            "--gdelt-max-records",
            type=int,
            default=250,
            help="Max articles per GDELT month query (max 250, default 250).",
        )
        parser.add_argument(
            "--no-text-extraction",
            action="store_true",
            default=False,
            help=(
                "Skip full-article text extraction (saves time; stores title only). "
                "AI classification still works but with less context."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be queued without actually queuing tasks.",
        )
        parser.add_argument(
            "--run-inline",
            action="store_true",
            default=False,
            help=(
                "Run tasks synchronously in this process (no Celery workers needed). "
                "Slow — use only for testing or when Celery is unavailable."
            ),
        )

    def handle(self, *args, **options):
        # ── Parse date range ──────────────────────────────────────────────────
        try:
            start_date = date.fromisoformat(options["start_date"])
        except ValueError:
            raise CommandError(f"Invalid --start-date: {options['start_date']}")

        end_str = options.get("end_date")
        end_date = date.fromisoformat(end_str) if end_str else date.today()

        if start_date > end_date:
            raise CommandError("--start-date must be before --end-date")

        source     = options["source"]
        dry_run    = options["dry_run"]
        run_inline = options["run_inline"]
        extract    = not options["no_text_extraction"]
        max_rec    = min(options["gdelt_max_records"], 250)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== GeoStance Historical Backfill ==="
            f"\n  Range  : {start_date} → {end_date}"
            f"\n  Source : {source}"
            f"\n  Mode   : {'DRY RUN' if dry_run else ('inline' if run_inline else 'Celery queue')}"
        ))

        run_gdelt = source in ("all", "gdelt")
        run_gov   = source in ("all", "gov") or source in SCRAPERS

        total_queued = 0

        # ── GDELT Pipeline ────────────────────────────────────────────────────
        if run_gdelt:
            self.stdout.write(self.style.HTTP_INFO("\n[GDELT] Setting up monthly tasks…"))

            # Resolve events
            if options["event_id"]:
                events = list(Event.objects.filter(pk=options["event_id"]))
                if not events:
                    raise CommandError(f"Event ID {options['event_id']} not found.")
            else:
                events = list(Event.objects.all())

            if not events:
                self.stdout.write(self.style.WARNING(
                    "  No Events found in DB. Add events first, then re-run."
                ))
            else:
                month_list = list(_iter_months(start_date, end_date))
                total_gdelt = len(events) * len(month_list)

                self.stdout.write(
                    f"  Events : {len(events)}\n"
                    f"  Months : {len(month_list)} ({start_date.strftime('%b %Y')} → {end_date.strftime('%b %Y')})\n"
                    f"  Tasks  : {total_gdelt} (one per event × month)"
                )

                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f"  [DRY RUN] Would queue {total_gdelt} GDELT tasks."
                    ))
                else:
                    from core.tasks import backfill_gdelt_month
                    queued = 0
                    for event in events:
                        for year, month in month_list:
                            if run_inline:
                                backfill_gdelt_month(
                                    event.id, year, month,
                                    max_records=max_rec,
                                    extract_text=extract,
                                )
                            else:
                                backfill_gdelt_month.delay(
                                    event.id, year, month,
                                    max_records=max_rec,
                                    extract_text=extract,
                                )
                            queued += 1

                            if queued % 50 == 0:
                                self.stdout.write(
                                    f"  … queued {queued}/{total_gdelt}", ending="\r"
                                )

                    self.stdout.write("")
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ Queued {queued} GDELT tasks."
                    ))
                    total_queued += queued

        # ── Government Scraper Pipeline ───────────────────────────────────────
        if run_gov:
            self.stdout.write(self.style.HTTP_INFO("\n[GOV SCRAPERS] Setting up scraper tasks…"))

            # Determine which scrapers to run
            if source in SCRAPERS:
                scraper_keys = [source]
            else:
                scraper_keys = list(SCRAPERS.keys())

            for key in scraper_keys:
                _, _, label = SCRAPERS[key]
                self.stdout.write(f"  Queuing: {label} ({key})")

                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f"  [DRY RUN] Would queue scraper task for {label}."
                    ))
                    continue

                from core.tasks import backfill_gov_archive
                if run_inline:
                    backfill_gov_archive(
                        key,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    )
                else:
                    backfill_gov_archive.delay(
                        key,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    )
                total_queued += 1

            if not dry_run:
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ Queued {len(scraper_keys)} government scraper task(s)."
                ))

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                "Dry run complete. Re-run without --dry-run to actually queue tasks."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"✓ Done! {total_queued} task(s) queued.\n\n"
                "Next steps:\n"
                "  1. Make sure Celery workers are running:\n"
                "       celery -A config worker -l INFO --concurrency=2\n"
                "  2. After ingestion completes, classify new RawPosts:\n"
                "       celery -A config call core.tasks.classify_rawposts_with_ai\n"
                "  3. Monitor progress in Celery logs or Flower dashboard.\n"
            ))
