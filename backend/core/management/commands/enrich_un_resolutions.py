"""
Management command: enrich_un_resolutions
==========================================
Backfills Layer-3 AI enrichment (ai_tags + plain-English explanation) for
UNResolution records that have not yet been enriched by the Celery task.

Can be run directly (--inline) for small batches, or it queues Celery tasks
for the full dataset.

Usage:
  # Queue all un-enriched resolutions via Celery (recommended for large sets)
  python manage.py enrich_un_resolutions

  # Re-enrich everything, even already-enriched records
  python manage.py enrich_un_resolutions --force

  # Run inline (no Celery required) — useful for testing / small imports
  python manage.py enrich_un_resolutions --inline --limit 50

  # Only resolutions from a specific year range
  python manage.py enrich_un_resolutions --year-from 2015 --year-to 2024 --inline
"""

import time

from django.core.management.base import BaseCommand

from core.models import UNResolution


class Command(BaseCommand):
    help = "Enrich UNResolution records with AI-generated tags and explanation (Layer 3)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Re-enrich even resolutions that already have an explanation.",
        )
        parser.add_argument(
            "--inline",
            action="store_true",
            default=False,
            help=(
                "Run enrichment inline (synchronous) instead of queuing Celery tasks. "
                "Slow for large datasets — use for testing or when Celery is unavailable."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of resolutions to process (default: all).",
        )
        parser.add_argument(
            "--year-from",
            type=int,
            default=None,
            help="Only enrich resolutions from this year onward.",
        )
        parser.add_argument(
            "--year-to",
            type=int,
            default=None,
            help="Only enrich resolutions up to and including this year.",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=0.5,
            help=(
                "Seconds to wait between inline API calls (default: 0.5). "
                "Increase if you hit rate limits."
            ),
        )

    def handle(self, *args, **options):
        force     = options["force"]
        inline    = options["inline"]
        limit     = options["limit"]
        year_from = options["year_from"]
        year_to   = options["year_to"]
        delay     = options["delay"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "=== UN Resolution AI Enrichment (Layer 3) ==="
        ))

        # Build queryset
        qs = UNResolution.objects.all()
        if not force:
            qs = qs.filter(explanation_generated_at__isnull=True)
        if year_from:
            qs = qs.filter(vote_date__year__gte=year_from)
        if year_to:
            qs = qs.filter(vote_date__year__lte=year_to)
        qs = qs.order_by("vote_date")
        if limit:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "No resolutions need enrichment. Use --force to re-enrich existing records."
            ))
            return

        self.stdout.write(
            f"  Found {total:,} resolutions to enrich "
            f"({'inline' if inline else 'via Celery'}, force={force})."
        )

        if inline:
            self._run_inline(qs, total, delay, force)
        else:
            self._queue_celery(qs, total)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _run_inline(self, qs, total, delay, force: bool = False):
        # Call the real Celery task synchronously (no worker needed) so there
        # is exactly ONE enrichment code path / prompt — no separate copy.
        from core.tasks import enrich_resolution_with_ai

        succeeded = failed = skipped = 0

        for i, res in enumerate(qs, 1):
            self.stdout.write(
                f"  [{i}/{total}] rcid={res.rcid} {res.title[:60] or res.short_description[:60]}",
                ending=" … ",
            )
            self.stdout.flush()

            try:
                result = enrich_resolution_with_ai(res.pk, force=force)
                if result.get("skipped"):
                    self.stdout.write(self.style.WARNING(f"skipped ({result.get('reason')})"))
                    skipped += 1
                else:
                    tags = result.get("ai_tags", [])
                    self.stdout.write(
                        self.style.SUCCESS(f"OK ({len(tags)} tags)")
                    )
                    succeeded += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"FAILED: {exc}"))
                failed += 1

            if delay and i < total:
                time.sleep(delay)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Done ===\n"
            f"  Succeeded : {succeeded:,}\n"
            f"  Skipped   : {skipped:,}\n"
            f"  Failed    : {failed:,}"
        ))

    def _queue_celery(self, qs, total):
        from core.tasks import enrich_resolution_with_ai

        queued = 0
        for res in qs:
            enrich_resolution_with_ai.apply_async(args=[res.pk])
            queued += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n  Queued {queued:,} Celery tasks.\n"
            f"  Monitor progress via Flower or: celery -A geovoice inspect active"
        ))

