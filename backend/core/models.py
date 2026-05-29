from django.db import models
from django.db.models import Q
from pgvector.django import VectorField, HnswIndex


class CountryBloc(models.Model):
    """
    A geopolitical/economic grouping of countries.
    slug is the stable identifier used in API filters (e.g. 'nato', 'brics').
    """
    name = models.CharField(max_length=100, unique=True)   # e.g. "NATO"
    slug = models.SlugField(max_length=30, unique=True)    # e.g. "nato"
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']


class Country(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)
    full_name = models.CharField(max_length=200, null=True, blank=True)
    isoa3_code = models.CharField(max_length=5, unique=True)
    isoa2_code = models.CharField(max_length=5, unique=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    blocs = models.ManyToManyField(
        CountryBloc,
        blank=True,
        related_name='countries',
    )

    def __str__(self):
        return self.name


class Event(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    # Semantic embedding of (title + description) — used for deduplication.
    # Populated automatically via post_save signal; null until first save
    # after the embedding service is available.
    embedding = VectorField(dimensions=1024, null=True, blank=True)

    class Meta:
        indexes = [
            HnswIndex(
                name='event_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            )
        ]

    def __str__(self):
        return self.title


class RawPost(models.Model):
    PLATFORM_CHOICES = [
        ("twitter",  "Twitter/X"),
        ("web",      "Web Scrape"),
        ("pdf",      "PDF Document"),
        ("javascript", "JS Page"),
        ("gdelt",    "GDELT News"),
        ("rss",      "RSS Feed"),
        ("scrape",   "Gov Archive Scrape"),
    ]

    country = models.ForeignKey(Country, on_delete=models.CASCADE)
    platform = models.CharField(max_length=50, choices=PLATFORM_CHOICES)

    # Twitter fields
    account_handle = models.CharField(max_length=255, blank=True)
    post_id = models.CharField(max_length=255, unique=True)
    post_text = models.TextField(blank=True)
    image_text = models.TextField(blank=True)
    combined_text = models.TextField(blank=True)
    media_urls = models.JSONField(default=list)
    image_urls = models.JSONField(default=list)
    ocr_processed = models.BooleanField(default=False)

    # Web scrape fields
    title = models.TextField(blank=True)
    source_url = models.URLField(max_length=500, null=True, blank=True)
    language = models.CharField(max_length=50, blank=True)
    content_type = models.CharField(max_length=50, blank=True)

    posted_at = models.DateTimeField()
    post_url = models.URLField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # AI processing flags
    classify_ai_processed = models.BooleanField(default=False)


class Statement(models.Model):
    STANCE_CHOICES = [
        ('support', 'Support'),
        ('neutral', 'Neutral'),
        ('oppose', 'Oppose'),
    ]

    raw_post = models.OneToOneField(
        RawPost, on_delete=models.CASCADE, null=True, blank=True
    )
    country = models.ForeignKey(Country, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    text = models.TextField()
    stance = models.CharField(max_length=10, choices=STANCE_CHOICES)
    confidence_score = models.FloatField(null=True, blank=True)
    summary = models.TextField(null=True, blank=True)
    topics = models.JSONField(default=list)
    # Normalised source URL — guaranteed non-empty; used as the dedup key.
    source_url = models.URLField(max_length=500, blank=True, default='')
    publish_date = models.DateField()

    class Meta:
        # Prevents the same source article creating two Statements for
        # the same (event, country) pair, even under concurrent workers.
        # source_url='' (empty string) is excluded from uniqueness checks
        # via the application-layer guard in classify_rawposts_with_ai.
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'country', 'source_url'],
                condition=~models.Q(source_url=''),
                name='unique_statement_per_source_url',
            )
        ]

    def __str__(self):
        return f"{self.country.name} - {self.event.title}"


class StatementChunk(models.Model):
    statement = models.ForeignKey(
        Statement, on_delete=models.CASCADE, related_name='chunks'
    )
    chunk_index = models.IntegerField()       # position: 0, 1, 2...
    chunk_text = models.TextField()          # the actual chunk content
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    # NVIDIA NIM models output 1024-dimensional vectors

    class Meta:
        ordering = ['chunk_index']
        indexes = [
            HnswIndex(
                name='chunk_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops']
            )
        ]

    def __str__(self):
        return f"Statement {self.statement_id} | Chunk {self.chunk_index}"


class CountryEventSummary(models.Model):
    country = models.ForeignKey(
        Country,
        on_delete=models.CASCADE
    )

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE
    )

    summary = models.TextField(null=True, blank=True)

    statement_count = models.IntegerField(default=0)

    mwhen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('country', 'event')

    def __str__(self):
        return f"{self.country.name} - {self.event.title}"


# ─────────────────────────────────────────────
# Event Auto-Detection
# ─────────────────────────────────────────────

class EventSuggestion(models.Model):
    """
    Candidate event detected automatically by the AI classifier when an
    article doesn't match any known Event.

    Lifecycle:
      pending  → admin reviews (review_event_suggestions command)
      approved → Event created from this suggestion
      merged   → folded into an existing Event (nearest_event)
      rejected → discarded (e.g. not a geopolitical event)

    The embedding field enables semantic clustering: multiple suggestions
    about "Israel-Hamas ceasefire" collapse into one before promotion.
    """
    STATUS_CHOICES = [
        ('pending',  'Pending Review'),
        ('approved', 'Approved — Event Created'),
        ('merged',   'Merged with Existing Event'),
        ('rejected', 'Rejected'),
    ]

    suggested_name        = models.CharField(max_length=255)
    suggested_description = models.TextField(blank=True)

    # Semantic embedding of suggested_name + suggested_description.
    # Used to cluster near-duplicate suggestions and match against
    # existing Event embeddings.
    embedding = VectorField(dimensions=1024, null=True, blank=True)

    # Evidence: how many distinct articles triggered this suggestion.
    # Incremented each time the classifier encounters another article
    # that maps to this suggestion (by semantic similarity).
    article_count = models.IntegerField(default=1)

    # Most similar existing Event at suggestion time (may be null if
    # genuinely new). similarity_score is cosine similarity ∈ [0, 1].
    nearest_event   = models.ForeignKey(
        Event, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='near_suggestions',
    )
    similarity_score = models.FloatField(null=True, blank=True)

    # Resolution
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    approved_event = models.ForeignKey(
        Event, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='from_suggestions',
    )
    # Raw posts that surfaced this suggestion (evidence trail)
    supporting_posts = models.ManyToManyField(
        'RawPost', blank=True, related_name='event_suggestions',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-article_count', '-created_at']
        indexes = [
            HnswIndex(
                name='eventsugg_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            )
        ]

    def __str__(self):
        return f"[{self.status}] {self.suggested_name} ({self.article_count} articles)"


# ─────────────────────────────────────────────
# UN Voting Records
# ─────────────────────────────────────────────

class UNResolution(models.Model):
    """
    A UN General Assembly or Security Council resolution.

    Primary source: UN Digital Library (https://digitallibrary.un.org)
    Canonical identifier: un_symbol (e.g. "A/RES/79/1")

    The 'explanation of the vote' is stored in resolution_text (UNDL abstract,
    field 520$a) — a plain-English summary of what the resolution proposes.

    The optional 'event' FK links a resolution to a tracked geopolitical Event.
    If no matching Event exists the resolution is saved with event=None and
    classify_resolution_to_event() can be re-run later as the Events list grows.
    """
    BODY_CHOICES = [
        ('UNGA', 'UN General Assembly'),
        ('UNSC', 'UN Security Council'),
        ('UNHRC', 'UN Human Rights Council'),
    ]

    # ── Source identifiers ────────────────────────────────────────────────────
    # UNDL control number from MARC21 field 001 (primary source)
    undl_id = models.CharField(max_length=30, blank=True, db_index=True)
    # Voeten Harvard Dataverse numeric ID — kept for back-compat, nullable
    rcid = models.IntegerField(null=True, blank=True, db_index=True)
    # UN document symbol — canonical cross-source identifier e.g. "A/RES/79/1"
    un_symbol = models.CharField(max_length=100, blank=True, db_index=True)

    # ── Resolution metadata ───────────────────────────────────────────────────
    session = models.IntegerField(null=True, blank=True)
    vote_date = models.DateField(db_index=True)
    title = models.TextField(blank=True)
    body = models.CharField(max_length=10, choices=BODY_CHOICES, default='UNGA')
    # Short description from the Voeten dataset (backward-compat)
    short_description = models.TextField(blank=True)
    # Full abstract from UNDL MARC21 field 520$a — "explanation of the vote"
    resolution_text = models.TextField(blank=True)
    # Meeting verbatim record symbol, from MARC 993$a e.g. "S/PV.10089"
    meeting_record_symbol = models.CharField(max_length=50, blank=True, db_index=True)
    # UN Meetings Coverage press release code, from PV record 993$a e.g. "SC/16274"
    press_release_code = models.CharField(max_length=20, blank=True)

    # ── Event linkage ─────────────────────────────────────────────────────────
    # Null when no matching event found — filled by classify_resolution_to_event
    event = models.ForeignKey(
        'Event',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='un_resolutions',
    )
    event_classified_at = models.DateTimeField(null=True, blank=True)

    # ── Three-layer tag system ────────────────────────────────────────────────
    # Layer 1: subject keywords from UNDL field 610$a / Voeten binary columns
    topic_tags = models.JSONField(default=list)
    # Layer 2: tags derived from the UN document symbol prefix structure
    symbol_tags = models.JSONField(default=list)
    # Layer 3: LLM-generated semantic tags, e.g. ["peacekeeping", "sanctions"]
    ai_tags = models.JSONField(default=list)
    # LLM-generated plain-English paragraph summary
    explanation = models.TextField(blank=True)
    explanation_generated_at = models.DateTimeField(null=True, blank=True)

    @property
    def all_tags(self) -> list:
        """
        Merged, deduplicated list of all three tag layers:
          topic_tags (Voeten binary columns)
          + symbol_tags (UN symbol prefix parsing)
          + ai_tags (LLM-generated)
        Order is preserved; duplicates are removed (case-insensitive).
        """
        seen = set()
        result = []
        for tag in (self.topic_tags or []) + (self.symbol_tags or []) + (self.ai_tags or []):
            key = tag.lower()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result

    def __str__(self):
        return f"[{self.un_symbol or self.rcid}] {self.title[:60] or self.short_description[:60]}"

    class Meta:
        ordering = ['-vote_date']
        constraints = [
            # un_symbol is the canonical dedup key; allow multiple blank values
            models.UniqueConstraint(
                fields=['un_symbol'],
                condition=~Q(un_symbol=''),
                name='unique_unresolution_un_symbol',
            ),
        ]


class UNVote(models.Model):
    """
    How a specific country voted on a specific UN resolution.
    Vote codes match the Voeten dataset encoding.
    """
    VOTE_CHOICES = [
        ('yes',        'Yes'),
        ('no',         'No'),
        ('abstain',    'Abstain'),
        ('absent',     'Absent'),
        ('not_member', 'Not a Member'),
    ]

    # Raw vote code stored alongside the human-readable choice
    # (1=yes, 2=abstain, 3=no, 8=absent, 9=not_member)
    VOTE_CODE_MAP = {1: 'yes', 2: 'abstain', 3: 'no', 8: 'absent', 9: 'not_member'}

    resolution = models.ForeignKey(
        UNResolution,
        on_delete=models.CASCADE,
        related_name='votes',
    )
    country = models.ForeignKey(
        Country,
        on_delete=models.CASCADE,
        related_name='un_votes',
    )
    vote = models.CharField(max_length=15, choices=VOTE_CHOICES)
    # Country's explanation of vote, parsed from UN Meetings Coverage press release
    explanation = models.TextField(blank=True)

    class Meta:
        unique_together = ('resolution', 'country')
        indexes = [
            models.Index(fields=['country', 'vote']),
        ]

    def __str__(self):
        return f"{self.country.name} → {self.vote} on [{self.resolution.rcid}]"
