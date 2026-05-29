"""
Management command: review_event_suggestions
=============================================
Interactive CLI for reviewing auto-detected EventSuggestions.

The AI classifier surfaces these when it encounters articles about
geopolitical events NOT in the known Event list.

Actions per suggestion:
  a  → Approve  — create a real Event from this suggestion
  m  → Merge    — fold into an existing Event (shows nearest match)
  r  → Reject   — discard (routine news, not a tracked event)
  s  → Skip     — leave as pending (review later)
  q  → Quit     — stop reviewing

Usage:
  python manage.py review_event_suggestions
  python manage.py review_event_suggestions --min-articles 3
  python manage.py review_event_suggestions --list-only
  python manage.py review_event_suggestions --auto-approve --min-articles 10
"""

from datetime import date

from django.core.management.base import BaseCommand

from core.models import Event, EventSuggestion


class Command(BaseCommand):
    help = "Review auto-detected event suggestions from the AI classifier."

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-articles",
            type=int,
            default=1,
            help="Only show suggestions with at least N supporting articles (default: 1).",
        )
        parser.add_argument(
            "--list-only",
            action="store_true",
            default=False,
            help="Print all pending suggestions without interactive review.",
        )
        parser.add_argument(
            "--auto-approve",
            action="store_true",
            default=False,
            help=(
                "Automatically approve all suggestions that have >= --min-articles "
                "articles AND are not near-duplicates of existing events (sim < 0.75)."
            ),
        )

    def handle(self, *args, **options):
        min_articles = options["min_articles"]
        list_only    = options["list_only"]
        auto_approve = options["auto_approve"]

        suggestions = (
            EventSuggestion.objects
            .filter(status="pending", article_count__gte=min_articles)
            .select_related("nearest_event")
            .order_by("-article_count", "-created_at")
        )

        total = suggestions.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                f"No pending suggestions with >= {min_articles} articles. "
                "Run classify_rawposts_with_ai to generate more."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Event Suggestions Review ({total} pending) ==="
        ))

        # ── List-only mode ────────────────────────────────────────────────────
        if list_only:
            for i, s in enumerate(suggestions, 1):
                self._print_suggestion(i, total, s)
            return

        # ── Auto-approve mode ─────────────────────────────────────────────────
        if auto_approve:
            from core.services.event_service import find_similar_event, embed_event_text
            approved = rejected = 0
            for s in suggestions:
                _, sim, band = find_similar_event(s.suggested_name, s.suggested_description)
                if band in ("same", "likely"):
                    self.stdout.write(
                        f"  SKIP (too similar to existing, sim={sim:.3f}): {s.suggested_name}"
                    )
                    rejected += 1
                else:
                    self._approve_suggestion(s)
                    approved += 1
            self.stdout.write(self.style.SUCCESS(
                f"\nAuto-approve done: {approved} approved, {rejected} skipped (near-duplicate)."
            ))
            return

        # ── Interactive mode ──────────────────────────────────────────────────
        approved = merged = rejected = skipped_count = 0

        for i, sugg in enumerate(suggestions, 1):
            self._print_suggestion(i, total, sugg)

            while True:
                action = input(
                    "  Action [a=approve / m=merge / r=reject / s=skip / q=quit]: "
                ).strip().lower()

                if action == "q":
                    self.stdout.write("\nQuitting. Remaining suggestions left as pending.")
                    self._print_summary(approved, merged, rejected, skipped_count)
                    return

                elif action == "a":
                    self._approve_suggestion(sugg)
                    approved += 1
                    break

                elif action == "m":
                    merged_ok = self._interactive_merge(sugg)
                    if merged_ok:
                        merged += 1
                        break
                    # else: re-prompt

                elif action == "r":
                    sugg.status = "rejected"
                    sugg.save(update_fields=["status", "updated_at"])
                    self.stdout.write(self.style.WARNING("  Rejected."))
                    rejected += 1
                    break

                elif action == "s":
                    skipped_count += 1
                    break

                else:
                    self.stdout.write("  Unknown action. Use: a / m / r / s / q")

        self._print_summary(approved, merged, rejected, skipped_count)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _print_suggestion(self, index, total, s):
        self.stdout.write(f"\n[{index}/{total}] {self.style.HTTP_INFO(s.suggested_name)}")
        if s.suggested_description:
            self.stdout.write(f"  Description : {s.suggested_description}")
        self.stdout.write(f"  Articles    : {s.article_count}")
        self.stdout.write(f"  First seen  : {s.created_at.strftime('%Y-%m-%d')}")
        if s.nearest_event:
            self.stdout.write(
                f"  Nearest event: {s.nearest_event.title} "
                f"(similarity={s.similarity_score:.3f})"
            )
        else:
            self.stdout.write("  Nearest event: (none — genuinely new)")

        # Show sample supporting article titles
        samples = list(
            s.supporting_posts.values_list("title", flat=True)
            .exclude(title="")[:3]
        )
        if samples:
            self.stdout.write("  Sample articles:")
            for t in samples:
                self.stdout.write(f"    • {t[:100]}")

    def _approve_suggestion(self, sugg):
        from core.services.event_service import embed_event_text

        embedding = embed_event_text(
            sugg.suggested_name, sugg.suggested_description
        ) if sugg.embedding is None else sugg.embedding

        event = Event.objects.create(
            title=sugg.suggested_name,
            description=sugg.suggested_description,
            start_date=date.today(),
            embedding=embedding,
        )
        sugg.status = "approved"
        sugg.approved_event = event
        sugg.save(update_fields=["status", "approved_event", "updated_at"])

        # Re-queue supporting articles so they get classified against the new event
        requeued = sugg.supporting_posts.filter(
            classify_ai_processed=True
        ).update(classify_ai_processed=False)

        self.stdout.write(self.style.SUCCESS(
            f"  Approved → Event '{event.title}' (id={event.pk}) "
            f"| {requeued} articles re-queued for classification."
        ))

    def _interactive_merge(self, sugg) -> bool:
        """Prompt user to pick an existing Event to merge into."""
        self.stdout.write("\n  Existing Events:")
        events = list(Event.objects.order_by("-start_date")[:20])
        for i, e in enumerate(events):
            self.stdout.write(f"    [{i}] {e.title} (id={e.pk}, {e.start_date})")

        choice = input("  Enter event number to merge into (or blank to cancel): ").strip()
        if not choice:
            return False

        try:
            idx = int(choice)
            target = events[idx]
        except (ValueError, IndexError):
            self.stdout.write("  Invalid choice.")
            return False

        sugg.status = "merged"
        sugg.approved_event = target
        sugg.save(update_fields=["status", "approved_event", "updated_at"])

        # Re-queue supporting articles so they now classify against the target event
        requeued = sugg.supporting_posts.update(classify_ai_processed=False)
        self.stdout.write(self.style.SUCCESS(
            f"  Merged into '{target.title}' | {requeued} articles re-queued."
        ))
        return True

    def _print_summary(self, approved, merged, rejected, skipped):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Summary ==="
            f"\n  Approved : {approved}"
            f"\n  Merged   : {merged}"
            f"\n  Rejected : {rejected}"
            f"\n  Skipped  : {skipped}"
        ))
        if approved + merged > 0:
            self.stdout.write(
                "\nNext: run classify_rawposts_with_ai to process re-queued articles."
            )
