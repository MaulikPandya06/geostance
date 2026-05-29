from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Event, Statement
from core.services.embedding_service import embed_statement
from core.tasks import regenerate_summary_task


@receiver(post_save, sender=Statement)
def auto_embed_on_save(sender, instance, created, **kwargs):
    """
    Automatically chunk and embed a Statement whenever it is created.
    Only runs on creation — not on every save (to avoid re-embedding
    on field updates).
    """
    if created:
        embed_statement(instance)
        regenerate_summary_task.apply_async(
            args=[instance.country.id, instance.event.id],
            countdown=10,
        )


@receiver(post_save, sender=Event)
def auto_embed_event_on_save(sender, instance, created, **kwargs):
    """
    Embed an Event's title+description whenever it is created or its
    title/description changes (embedding is None after a text update).

    Uses update() internally to avoid an infinite signal loop.
    Safe to run synchronously — Event embeddings are small and fast.
    """
    # Re-embed on creation or when embedding is missing
    if instance.embedding is None:
        try:
            from core.services.event_service import embed_and_save_event
            embed_and_save_event(instance)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "auto_embed_event_on_save failed for Event %s: %s",
                instance.pk, exc,
            )
