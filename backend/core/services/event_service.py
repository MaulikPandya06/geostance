"""
core/services/event_service.py
================================
Semantic event deduplication and safe event creation.

Key function: get_or_create_event_safe()
  - Embeds the proposed event title + description
  - Compares against every existing Event embedding (pgVector cosine distance)
  - Returns the existing Event if similarity is above threshold
  - Creates a new Event only if genuinely novel
  - Logs a warning in the ambiguous middle band

Similarity thresholds (tunable via settings):
  > 0.92  → SAME event   — return existing silently
  0.75–0.92 → LIKELY SAME — return existing + log WARNING
  < 0.75  → NEW event    — create and embed

This prevents "Russia-Ukraine War" and "Ukraine Crisis" and
"War in Ukraine" from splitting into three separate Event records.
"""

import logging
import os

from openai import OpenAI
from pgvector.django import CosineDistance

logger = logging.getLogger(__name__)

EMBEDDING_MODEL      = "nvidia/nv-embedqa-e5-v5"
EMBEDDING_DIMENSIONS = 1024

# Cosine *similarity* thresholds (1 - cosine_distance)
THRESHOLD_SAME   = 0.92   # above this → treat as identical event
THRESHOLD_LIKELY = 0.75   # above this → probably same, warn and reuse


def _get_nvidia_client() -> OpenAI:
    return OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=os.getenv("NVIDIA_NIM_API_KEY"),
    )


def embed_event_text(title: str, description: str = "") -> list[float] | None:
    """
    Return a 1024-dimensional embedding for an event's title + description.
    Uses the 'passage' input_type — correct for corpus documents (Events,
    StatementChunks) that sit on the retrieval side of the asymmetric model.
    Returns None if the API call fails.
    """
    text = f"{title}. {description}".strip(". ")
    if not text:
        return None
    try:
        client = _get_nvidia_client()
        resp = client.embeddings.create(
            input=[text],
            model=EMBEDDING_MODEL,
            encoding_format="float",
            extra_body={"input_type": "passage", "truncate": "END"},
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.error("embed_event_text failed for '%s': %s", title, exc)
        return None


def embed_text_as_query(text: str) -> list[float] | None:
    """
    Return a 1024-dimensional embedding for a *query* string.

    nv-embedqa-e5-v5 is an asymmetric retrieval model:
      • Corpus documents (Events, StatementChunks) must use input_type="passage"
      • Search queries must use input_type="query"

    Using "passage" for both sides suppresses similarity scores by ~0.25–0.30,
    causing genuine matches to score ~0.4 instead of ~0.65–0.75.

    Use this function when embedding UN resolution titles / short descriptions
    for classification against the Event corpus.
    """
    if not text:
        return None
    try:
        client = _get_nvidia_client()
        resp = client.embeddings.create(
            input=[text],
            model=EMBEDDING_MODEL,
            encoding_format="float",
            extra_body={"input_type": "query", "truncate": "END"},
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.error("embed_text_as_query failed: %s", exc)
        return None


def find_similar_event(
    title: str,
    description: str = "",
    same_threshold: float = THRESHOLD_SAME,
    likely_threshold: float = THRESHOLD_LIKELY,
):
    """
    Search all existing Events by semantic similarity to the given title/description.

    Returns:
        (event, similarity, band) where band is one of:
          'same'   → similarity > same_threshold
          'likely' → same_threshold >= similarity > likely_threshold
          'new'    → similarity <= likely_threshold  (event is None)

    If no events have embeddings yet, returns (None, 0.0, 'new').
    """
    from core.models import Event

    embedding = embed_event_text(title, description)
    if embedding is None:
        return None, 0.0, "new"

    # Find the nearest existing event by cosine distance
    try:
        nearest = (
            Event.objects
            .exclude(embedding=None)
            .annotate(distance=CosineDistance("embedding", embedding))
            .order_by("distance")
            .first()
        )
    except Exception as exc:
        logger.error("find_similar_event DB query failed: %s", exc)
        return None, 0.0, "new"

    if nearest is None:
        return None, 0.0, "new"

    similarity = round(1.0 - float(nearest.distance), 4)

    if similarity > same_threshold:
        logger.info(
            "Event match SAME (%.3f): '%s' → '%s'",
            similarity, title, nearest.title,
        )
        return nearest, similarity, "same"
    elif similarity > likely_threshold:
        logger.warning(
            "Event match LIKELY (%.3f): '%s' is probably '%s' — reusing existing.",
            similarity, title, nearest.title,
        )
        return nearest, similarity, "likely"
    else:
        logger.info(
            "Event match NEW (%.3f): '%s' has no close existing event.",
            similarity, title,
        )
        return None, similarity, "new"


def get_or_create_event_safe(
    title: str,
    description: str = "",
    start_date=None,
    same_threshold: float = THRESHOLD_SAME,
    likely_threshold: float = THRESHOLD_LIKELY,
):
    """
    Create an Event only if no semantically similar one exists.

    Returns:
        (event, created, band, similarity)
        created=False means an existing event was reused.

    Usage in classify pipeline:
        event, created, band, sim = get_or_create_event_safe(
            "Russia Ukraine War", "Russian invasion of Ukraine"
        )
        # band='same' → no new event; band='new' → fresh event created
    """
    from core.models import Event
    from datetime import date as date_type

    similar, similarity, band = find_similar_event(
        title, description, same_threshold, likely_threshold
    )

    if band in ("same", "likely"):
        return similar, False, band, similarity

    # Genuinely new event — create it
    if start_date is None:
        start_date = date_type.today()

    embedding = embed_event_text(title, description)

    event = Event.objects.create(
        title=title,
        description=description,
        start_date=start_date,
        embedding=embedding,
    )
    logger.info("New Event created: '%s' (id=%s)", event.title, event.pk)
    return event, True, "new", similarity


def embed_and_save_event(event) -> bool:
    """
    Compute and persist the embedding for an existing Event that has none.
    Called by the post_save signal and by the backfill command.
    Returns True if embedding was saved, False on failure.
    """
    if event.embedding is not None:
        return True   # already embedded

    embedding = embed_event_text(event.title, event.description)
    if embedding is None:
        return False

    # Use update() to avoid re-triggering the post_save signal
    from core.models import Event
    Event.objects.filter(pk=event.pk).update(embedding=embedding)
    logger.info("Embedded Event id=%s '%s'", event.pk, event.title)
    return True
