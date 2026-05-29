"""
Management command: classify_resolutions
=========================================
Link UN resolutions to geopolitical Events using an LLM (Llama-3.3-70B).

For each UNResolution the LLM receives the full list of known Events plus the
resolution's AI explanation and tags, and returns the best-matching event_id
with a confidence 0.0-1.0.  If confidence >= --threshold the resolution's
`event` FK is set; otherwise the resolution is left unlinked (event=NULL) but
still persists in the DB.

Prerequisites
-------------
  Resolutions should be enriched first (so they have an `explanation`):
      python manage.py enrich_un_resolutions --inline

Modes
-----
  --queue   (default) Send tasks to Celery.  Fast to start; workers do the work.
  --inline  Run classification in this process.  Slow but no Celery needed.

Usage
-----
  # Queue all unclassified resolutions (needs Celery worker running)
  python manage.py classify_resolutions

  # Run inline — no Celery needed
  python manage.py classify_resolutions --inline

  # Re-classify everything, including already-classified resolutions
  python manage.py classify_resolutions --inline --force

  # Custom LLM confidence threshold (default 0.7)
  python manage.py classify_resolutions --inline --threshold 0.6

  # Only classify a specific year's resolutions
  python manage.py classify_resolutions --inline --year 2024

  # Limit batch size (useful for testing)
  python manage.py classify_resolutions --inline --limit 50

  # Dry-run: show which resolutions would be classified, without writing
  python manage.py classify_resolutions --inline --dry-run
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Link UNResolution records to geopolitical Events via semantic similarity. "
        "Run 'embed_events' first if Event.embedding is empty."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--inline", action="store_true", default=False,
            help=(
                "Run classification in this process (no Celery). "
                "Slower but works without a running worker."
            ),
        )
        parser.add_argument(
            "--force", action="store_true", default=False,
            help=(
                "Re-classify ALL resolutions, including those already linked to an Event. "
                "Without this flag only unclassified resolutions are processed."
            ),
        )
        parser.add_argument(
            "--threshold", type=float, default=0.7,
            help=(
                "LLM confidence threshold for linking a resolution to an Event "
                "(default: 0.7).  Below this the resolution stays unlinked "
                "(event=NULL) but still persists in the DB."
            ),
        )
        parser.add_argument(
            "--year", type=int, default=None,
            help="Only classify resolutions with vote_date in this year.",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Maximum number of resolutions to process (useful for testing).",
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Show what would be classified without writing to the DB.",
        )

    def handle(self, *args, **options):
        from core.models import Event, UNResolution

        inline       = options["inline"]
        force        = options["force"]
        threshold    = options["threshold"]
        year         = options["year"]
        limit        = options["limit"]
        dry_run      = options["dry_run"]

        # ── Sanity check: are there events to classify against? ───────────────
        # LLM-based classification needs the Events list, but NOT embeddings.
        total_events = Event.objects.count()
        if total_events == 0:
            self.stdout.write(self.style.ERROR(
                "No Events in the database.  Create some events first."
            ))
            return

        # ── Build resolution queryset ─────────────────────────────────────────
        qs = UNResolution.objects.all()

        if not force:
            qs = qs.filter(event_id__isnull=True)

        if year is not None:
            qs = qs.filter(vote_date__year=year)

        if limit is not None:
            qs = qs[:limit]

        total_res = qs.count()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== Classify Resolutions -> Events ==="
            f"\n  Events in DB           : {total_events:,}"
            f"\n  Resolutions to process : {total_res:,}"
            f"\n  Threshold              : {threshold}"
            f"\n  Mode                   : {'inline' if inline else 'Celery queue'}"
            f"{'  (DRY RUN)' if dry_run else ''}"
        ))

        if total_res == 0:
            self.stdout.write(self.style.SUCCESS(
                "Nothing to do — all resolutions are already classified. "
                "Use --force to reclassify."
            ))
            return

        # ── Dispatch ──────────────────────────────────────────────────────────
        if dry_run:
            self._dry_run(qs)
            return

        if inline:
            self._run_inline(qs, threshold)
        else:
            self._run_queued(qs, threshold)

    # ── Inline mode ───────────────────────────────────────────────────────────

    def _run_inline(self, qs, threshold: float) -> None:
        from core.tasks import classify_resolution_to_event

        linked = failed = skipped = 0
        pks = list(qs.values_list("pk", flat=True))

        for i, pk in enumerate(pks, 1):
            try:
                result = classify_resolution_to_event(pk, threshold=threshold)
                if result.get("linked"):
                    linked += 1
                    self.stdout.write(
                        f"  [{i}/{len(pks)}] LINKED   pk={pk} "
                        f"→ event_id={result['event_id']} "
                        f"(conf={result['confidence']:.3f})"
                    )
                elif result.get("skipped"):
                    skipped += 1
                else:
                    best = result.get("confidence", 0)
                    reason = result.get("reason", "")
                    self.stdout.write(
                        f"  [{i}/{len(pks)}] no match pk={pk} "
                        f"(best={best:.3f})"
                        f"(reason={reason})"
                        )
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(
                    f"  [{i}/{len(pks)}] ERROR    pk={pk}: {exc}"
                ))

        style = self.style.SUCCESS if failed == 0 else self.style.WARNING
        self.stdout.write(style(
            f"\n=== Classification complete ==="
            f"\n  Linked  : {linked:,}"
            f"\n  No match: {len(pks) - linked - failed - skipped:,}"
            f"\n  Skipped : {skipped:,}"
            f"\n  Errors  : {failed:,}"
        ))

    # ── Queued (Celery) mode ──────────────────────────────────────────────────

    def _run_queued(self, qs, threshold: float) -> None:
        try:
            from core.tasks import bulk_classify_resolutions
        except ImportError as exc:
            self.stdout.write(self.style.ERROR(f"Cannot import tasks: {exc}"))
            return

        # If the queryset is pre-filtered (year / force), queue individually
        # rather than calling bulk_classify_resolutions which has its own filter logic.
        pks = list(qs.values_list("pk", flat=True))

        try:
            from core.tasks import classify_resolution_to_event
            for pk in pks:
                classify_resolution_to_event.apply_async(
                    args=[pk], kwargs={"threshold": threshold}
                )
            self.stdout.write(self.style.SUCCESS(
                f"\n  Queued {len(pks):,} classify tasks → Celery"
                f"\n  Monitor with: celery -A config worker --loglevel=info"
            ))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"Failed to queue tasks: {exc}\n"
                "Is Celery running?  Try --inline to run without Celery."
            ))

    # ── Dry run ───────────────────────────────────────────────────────────────

    def _dry_run(self, qs) -> None:
        total = qs.count()
        self.stdout.write(f"\n  Would classify {total:,} resolutions:")
        for res in qs.only("pk", "un_symbol", "title", "vote_date")[:20]:
            self.stdout.write(
                f"    [{res.pk}] {res.un_symbol or '—':20s} "
                f"{str(res.vote_date or '?'):12s} {res.title[:50]}"
            )
        if total > 20:
            self.stdout.write(f"    … and {total - 20:,} more")
