# core/urls.py

from django.urls import path

from .views import (
    # Existing
    ChatbotView,
    CountryDetailView,
    CountryEventSummaryView,
    CountryListCreateView,
    EventCountryStatementsView,
    EventDetailView,
    EventListCreateView,
    EventStatementsView,
    StatementDetailView,
    StatementListCreateView,
    # Heatmap (now supports ?bloc= filter)
    EventHeatmapBlocFilteredView,
    # Blocs
    CountryBlocListView,
    CountryBlocDetailView,
    # UN Voting
    EventResolutionsView,
    UNResolutionListView,
    UNResolutionDetailView,
    UNResolutionVoteMapView,
    UNVotesByCountryView,
    UNVoteAlignmentView,
    # Ask GeoStance AI
    GenerateResolutionReportView,
    AskGeoStanceView,
)

urlpatterns = [
    # ── Events ────────────────────────────────────────────────────────────────
    path("events/",                   EventListCreateView.as_view()),
    path("events/<int:pk>/",          EventDetailView.as_view()),
    path("events/<int:pk>/statements/", EventStatementsView.as_view()),

    # Heatmap — now accepts optional ?bloc=nato query param
    path("events/<int:pk>/heatmap/",  EventHeatmapBlocFilteredView.as_view()),

    path(
        "events/<int:event_id>/countries/<str:country_code>/statements/",
        EventCountryStatementsView.as_view(),
    ),

    # ── Countries ─────────────────────────────────────────────────────────────
    path("countries/",           CountryListCreateView.as_view()),
    path("countries/<int:pk>/",  CountryDetailView.as_view()),

    # ── Statements ────────────────────────────────────────────────────────────
    path("statements/",          StatementListCreateView.as_view()),
    path("statements/<int:pk>/", StatementDetailView.as_view()),

    # ── Country Blocs ─────────────────────────────────────────────────────────
    path("blocs/",               CountryBlocListView.as_view()),
    path("blocs/<slug:slug>/",   CountryBlocDetailView.as_view()),

    # ── UN Voting ─────────────────────────────────────────────────────────────
    # Resolutions for an event:               GET /api/events/{pk}/resolutions/
    path("events/<int:pk>/resolutions/",       EventResolutionsView.as_view()),
    # List / filter resolutions:              GET /api/un-resolutions/?year=2022&topic=Nuclear
    path("un-resolutions/",                    UNResolutionListView.as_view()),
    # Single resolution + all votes:          GET /api/un-resolutions/9576/
    path("un-resolutions/<int:rcid>/",         UNResolutionDetailView.as_view()),
    # Vote-map for D3 rendering:              GET /api/un-resolutions/{pk}/vote-map/
    path("un-resolutions/<int:pk>/vote-map/",  UNResolutionVoteMapView.as_view()),
    # All votes by a country:                 GET /api/un-votes/country/IND/
    path("un-votes/country/<str:iso3>/",       UNVotesByCountryView.as_view()),
    # Voting alignment between two countries: GET /api/un-votes/alignment/?country_a=IND&country_b=CHN
    path("un-votes/alignment/",               UNVoteAlignmentView.as_view()),

    # ── AI Endpoints ──────────────────────────────────────────────────────────
    path("chatbot/",  ChatbotView.as_view(),              name="chatbot"),
    path("summary/",  CountryEventSummaryView.as_view(),  name="summary"),
    # Report generation — POST with resolution_ids + feature → .docx download
    path("un-resolutions/generate-report/", GenerateResolutionReportView.as_view(), name="generate-report"),
    # Chat endpoint — POST with resolution_ids + feature → JSON answer
    path("un-resolutions/ask/", AskGeoStanceView.as_view(), name="ask-ai"),
]
