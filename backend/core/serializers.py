# core/serializers.py

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Country, CountryBloc, Event, Statement, UNResolution, UNVote


class CountryBlocSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = CountryBloc
        fields = ['id', 'name', 'slug', 'description', 'member_count']

    def get_member_count(self, obj):
        return obj.countries.count()


class CountrySerializer(serializers.ModelSerializer):
    blocs = CountryBlocSerializer(many=True, read_only=True)

    class Meta:
        model = Country
        fields = '__all__'


class EventSerializer(serializers.ModelSerializer):
    total_statements = serializers.IntegerField(read_only=True)
    countries_involved = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = '__all__'

    def get_countries_involved(self, obj):
        return obj.statement_set.values('country').distinct().count()


class StatementSerializer(serializers.ModelSerializer):
    country_name = serializers.CharField(
        source='country.name',
        read_only=True
    )

    full_name = serializers.CharField(
        source='country.full_name',
        read_only=True
    )

    country_isoa2 = serializers.CharField(
        source='country.isoa2_code',
        read_only=True
    )

    country_isoa3 = serializers.CharField(
        source='country.isoa3_code',
        read_only=True
    )

    country_flag = serializers.CharField(
        source='country.flag_url',
        read_only=True
    )

    class Meta:
        model = Statement
        fields = '__all__'


class UNResolutionSerializer(serializers.ModelSerializer):
    votes_summary   = serializers.SerializerMethodField()
    all_tags        = serializers.SerializerMethodField()
    # Event linkage
    event_id        = serializers.IntegerField(source='event.id',    read_only=True, allow_null=True)
    event_title     = serializers.CharField(source='event.title',    read_only=True, allow_null=True)
    event_classified = serializers.DateTimeField(
        source='event_classified_at', read_only=True, allow_null=True
    )

    class Meta:
        model = UNResolution
        fields = [
            'id', 'undl_id', 'rcid', 'un_symbol',
            'session', 'vote_date', 'title', 'body',
            # Resolution text (UNDL abstract = "explanation of the vote")
            'resolution_text',
            'short_description',
            # Three-layer tag system
            'topic_tags',          # Layer 1: UNDL subject keywords / Voeten binary
            'symbol_tags',         # Layer 2: UN document symbol prefix
            'ai_tags',             # Layer 3: LLM-generated
            'all_tags',            # Merged + deduplicated
            # LLM explanation
            'explanation',
            'explanation_generated_at',
            # Event linkage
            'event_id', 'event_title', 'event_classified',
            # Vote breakdown
            'votes_summary',
        ]

    @extend_schema_field(serializers.DictField(child=serializers.IntegerField()))
    def get_votes_summary(self, obj):
        """Return count breakdown: yes / no / abstain / absent / not_member."""
        from django.db.models import Count
        qs = obj.votes.values('vote').annotate(count=Count('id'))
        return {row['vote']: row['count'] for row in qs}

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_all_tags(self, obj):
        return obj.all_tags


class UNVoteSerializer(serializers.ModelSerializer):
    country_name   = serializers.CharField(source='country.name',       read_only=True)
    country_isoa3  = serializers.CharField(source='country.isoa3_code', read_only=True)
    country_isoa2  = serializers.CharField(source='country.isoa2_code', read_only=True)
    resolution_symbol = serializers.CharField(source='resolution.un_symbol', read_only=True)
    vote_date      = serializers.DateField(source='resolution.vote_date', read_only=True)
    resolution_title = serializers.CharField(source='resolution.title', read_only=True)
    topic_tags     = serializers.JSONField(source='resolution.topic_tags', read_only=True)

    class Meta:
        model = UNVote
        fields = [
            'id', 'vote',
            'country_name', 'country_isoa3', 'country_isoa2',
            'resolution_id', 'vote_date', 'resolution_title', 'topic_tags',
        ]
