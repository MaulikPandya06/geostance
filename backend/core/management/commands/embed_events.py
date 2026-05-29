"""
Management command: embed_events
=================================
Backfill semantic embeddings for Events that don't have one yet.

The Event.embedding column (pgVector, 1024-dim) is required for:
  • Event deduplication (get_or_create_event_safe)
  • UN resolution → Event classification (classify_resolution_to_event)

A new Event created via the admin or API only gets its embedding if the
post_save signal fires while NVIDIA_NIM_API_KEY is set.  If the key was
absent at creation time, or the signal failed, embedding will be NULL.
This command fills those gaps.

Usage
-----
  # Embed all Events that have no embedding (safe to re-run)
  python manage.py embed_events

  # Re-embed EVERY Event (regenerate, e.g. after model change)
  python manage.py embed_events --force

  # Embed a single Event by primary key
  python manage.py embed_events --event-id 42

  # Dry-run: show which events would be embedded
  python manage.py embed_events --dry-run
"""

import time

from django.core.management.base import BaseCommand

EMBED_DELAY = 0.3   # seconds between API calls — stay inside NIM free-tier limits


class Command(BaseCommand):
    help = "Backfill or regenerate semantic embeddings for Event records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true", default=False,
            help="Re-embed ALL events, including those that already have an embedding.",
        )
        parser.add_argument(
            "--event-id", type=int, default=None,
            help="Embed a single Event by primary key (ignores --force scope).",
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Show how many events need embedding without calling the API.",
        )

    def handle(self, *args, **options):
        from core.models import Event
        from core.services.event_service import embed_event_text

        force    = options["force"]
        event_id = options["event_id"]
        dry_run  = options["dry_run"]

        # ── Build queryset ────────────────────────────────────────────────────
        if event_id is not None:
            qs = Event.objects.filter(pk=event_id)
            if not qs.exists():
                self.stdout.write(self.style.ERROR(f"Event id={event_id} not found."))
                return
        elif force:
            qs = Event.objects.all()
        else:
            qs = Event.objects.filter(embedding__isnull=True)

        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "All events already have embeddings. Use --force to regenerate."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== Embed Events ==="
            f"\n  Events to embed : {total:,}"
            f"{'  (dry-run — no API calls)' if dry_run else ''}"
        ))

        if dry_run:
            for ev in qs.only("id", "title")[:20]:
                self.stdout.write(f"  [{ev.pk}] {ev.title}")
            if total > 20:
                self.stdout.write(f"  … and {total - 20:,} more")
            return

        # ── Embed ─────────────────────────────────────────────────────────────
        ok = failed = skipped = 0

        for ev in qs.only("id", "title", "description", "embedding"):
            if ev.embedding is not None and not force:
                skipped += 1
                continue

            embedding = embed_event_text(ev.title, ev.description or "")
            if embedding is None:
                self.stdout.write(self.style.WARNING(
                    f"  FAILED  [{ev.pk}] {ev.title[:70]}"
                ))
                failed += 1
            else:
                # Use update() to avoid re-triggering post_save signal
                Event.objects.filter(pk=ev.pk).update(embedding=embedding)
                self.stdout.write(f"  OK      [{ev.pk}] {ev.title[:70]}")
                ok += 1

            time.sleep(EMBED_DELAY)

        style = self.style.SUCCESS if failed == 0 else self.style.WARNING
        self.stdout.write(style(
            f"\n=== Done ==="
            f"\n  Embedded : {ok:,}"
            f"\n  Failed   : {failed:,}"
            f"\n  Skipped  : {skipped:,}"
        ))

        if failed:
            self.stdout.write(self.style.WARNING(
                "  Check NVIDIA_NIM_API_KEY is set and the NIM service is reachable."
            ))
