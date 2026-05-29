# core/views.py

import json
import logging

from django.db.models import Count, Q
from django.http import FileResponse, HttpResponseBadRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

from core.services.rag_service import answer_question

from .models import (
    Country, CountryBloc, CountryEventSummary,
    Event, Statement, UNResolution, UNVote,
)
from .permissions import IsAdminOrReadOnly
from .serializers import (
    CountryBlocSerializer, CountrySerializer,
    EventSerializer, StatementSerializer,
    UNResolutionSerializer, UNVoteSerializer,
)


# GET + POST
class EventListCreateView(generics.ListCreateAPIView):
    # queryset = Event.objects.all()
    serializer_class = EventSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        return (
            Event.objects
            .annotate(total_statements=Count("statement"))
            .order_by("-total_statements")
        )


# GET + PATCH + DELETE
class EventDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Event.objects.all()
    serializer_class = EventSerializer
    permission_classes = [IsAdminOrReadOnly]


# GET + POST
class CountryListCreateView(generics.ListCreateAPIView):
    queryset = Country.objects.all()
    serializer_class = CountrySerializer
    permission_classes = [IsAdminOrReadOnly]


# GET + PATCH + DELETE
class CountryDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Country.objects.all()
    serializer_class = CountrySerializer
    permission_classes = [IsAdminOrReadOnly]


# GET + POST
class StatementListCreateView(generics.ListCreateAPIView):
    queryset = Statement.objects.all()
    serializer_class = StatementSerializer
    permission_classes = [IsAdminOrReadOnly]


# GET + PATCH + DELETE
class StatementDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Statement.objects.all()
    serializer_class = StatementSerializer
    permission_classes = [IsAdminOrReadOnly]


# GET statements by event
class EventStatementsView(generics.ListAPIView):
    serializer_class = StatementSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return Statement.objects.filter(event_id=self.kwargs['pk'])\
            .annotate(total_statements=Count("statement"))\
            .order_by("-total_statements")


# GET statements by event + country
class EventCountryStatementsView(APIView):
    serializer_class = StatementSerializer
    permission_classes = [AllowAny]

    def get(self, request, event_id, country_code):
        statements = (
            Statement.objects
            .filter(
                event_id=event_id,
                country__isoa3_code=country_code
            )
            .select_related("country")
            .order_by("-publish_date")
        )

        country = Country.objects.filter(
            isoa3_code=country_code
        ).first()

        if not country:
            return Response(
                {"detail": "Country not found"},
                status=404
            )

        return Response({
            "country": {
                "full_name": country.full_name,
                "country_name": country.name,
                "isoa2_code": country.isoa2_code,
                "isoa3_code": country.isoa3_code,
            },

            "statements": StatementSerializer(
                statements,
                many=True
            ).data
        })


# Heatmap
class EventHeatmapView(APIView):
    def get(self, request, pk):
        heatmap = Statement.objects.filter(event_id=pk)\
            .values(
                'country__name',
                'country__isoa3_code',
                'country__isoa2_code',
                'country__full_name',
                'country__lat',
                'country__lng'
            )\
            .annotate(statement_count=Count('id'))\
            .order_by('-statement_count')

        return Response([
            {
                "country": i['country__name'],
                "isoa3_code": i['country__isoa3_code'],
                "isoa2_code": i['country__isoa2_code'],
                "full_name": i['country__full_name'],
                "statement_count": i['statement_count']
            }
            for i in heatmap
        ])


@method_decorator(csrf_exempt, name='dispatch')
class ChatbotView(View):

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
            question = data.get('question', '').strip()
            country = data.get('country', '').strip()
            event = data.get('event', '').strip()

            if not all([question, country, event]):
                return JsonResponse(
                    {"error": "question, country, and event are all required"},
                    status=400
                )

            answer = (answer_question(query=question, country=country,
                                      event=event))
            return JsonResponse({"answer": answer})

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


class CountryEventSummaryView(APIView):

    permission_classes = [AllowAny]

    def get(self, request):

        country = request.GET.get("country")
        event = request.GET.get("event")

        summary = CountryEventSummary.objects.filter(
            country__name=country,
            event__title=event
        ).first()

        if not summary:
            return Response(
                {"summary": None},
                status=404
            )

        return Response({
            "summary": summary.summary,
            "statement_count": summary.statement_count,
            "mwhen": summary.mwhen
        })


# ─────────────────────────────────────────────────────────────────────────────
# Country Blocs
# ─────────────────────────────────────────────────────────────────────────────

class CountryBlocListView(generics.ListAPIView):
    """
    GET /api/blocs/
    Returns all geopolitical blocs with member counts.
    """
    queryset = CountryBloc.objects.all()
    serializer_class = CountryBlocSerializer
    permission_classes = [AllowAny]


class CountryBlocDetailView(generics.RetrieveAPIView):
    """
    GET /api/blocs/<slug>/
    Returns a single bloc with all its member countries.
    """
    queryset = CountryBloc.objects.prefetch_related('countries')
    serializer_class = CountryBlocSerializer
    lookup_field = 'slug'
    permission_classes = [AllowAny]

    def retrieve(self, request, *args, **kwargs):
        bloc = self.get_object()
        data = CountryBlocSerializer(bloc).data
        data['members'] = CountrySerializer(
            bloc.countries.all(), many=True
        ).data
        return Response(data)


# ─────────────────────────────────────────────────────────────────────────────
# Event → Resolutions
# ─────────────────────────────────────────────────────────────────────────────

class EventResolutionsView(APIView):
    """
    GET /api/events/{pk}/resolutions/
    Returns UNResolutions linked to this event that have at least one vote record,
    ordered by vote_date asc.
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        resolutions = (
            UNResolution.objects
            .filter(event_id=pk)
            .annotate(vote_count=Count('votes'))
            .filter(vote_count__gt=0)
            .order_by('vote_date')
            .only('id', 'un_symbol', 'title', 'vote_date', 'body')
        )
        data = [
            {
                'id':         r.id,
                'un_symbol':  r.un_symbol,
                'title':      r.title,
                'vote_date':  r.vote_date,
                'body':       r.body,
            }
            for r in resolutions
        ]
        return Response(data)


# ─────────────────────────────────────────────────────────────────────────────
# UN Voting Records
# ─────────────────────────────────────────────────────────────────────────────

class UNResolutionListView(generics.ListAPIView):
    """
    GET /api/un-resolutions/
    Optional filters: ?year=2022  ?topic=Nuclear  ?body=UNGA
    """
    serializer_class = UNResolutionSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = UNResolution.objects.all()
        year  = self.request.query_params.get('year')
        topic = self.request.query_params.get('topic')
        body  = self.request.query_params.get('body')

        if year:
            qs = qs.filter(vote_date__year=year)
        if topic:
            # topic_tags is a JSON list — filter by containment
            qs = qs.filter(topic_tags__contains=[topic])
        if body:
            qs = qs.filter(body__iexact=body)

        return qs.order_by('-vote_date')


class UNResolutionDetailView(generics.RetrieveAPIView):
    """
    GET /api/un-resolutions/<rcid>/
    Full resolution detail including per-country vote breakdown.
    """
    serializer_class = UNResolutionSerializer
    permission_classes = [AllowAny]
    lookup_field = 'rcid'

    def get_queryset(self):
        return UNResolution.objects.prefetch_related('votes__country')

    def retrieve(self, request, *args, **kwargs):
        resolution = self.get_object()
        data = UNResolutionSerializer(resolution).data

        # Full per-country vote list
        votes = UNVote.objects.filter(
            resolution=resolution
        ).select_related('country').order_by('country__name')

        data['votes'] = UNVoteSerializer(votes, many=True).data
        return Response(data)


class UNResolutionVoteMapView(APIView):
    """
    GET /api/un-resolutions/{pk}/vote-map/
    Returns a flat {ISO3: vote} dict for D3 map coloring, grouped country
    lists per vote category, vote summary counts, and full resolution detail.

    vote values: "yes" | "no" | "abstain" | "absent"
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        try:
            resolution = UNResolution.objects.get(pk=pk)
        except UNResolution.DoesNotExist:
            return Response({"detail": "Not found"}, status=404)

        votes = (
            UNVote.objects
            .filter(resolution=resolution)
            .select_related('country')
            .order_by('country__name')
        )

        vote_map: dict = {}
        by_category: dict = {"yes": [], "no": [], "abstain": [], "absent": []}

        for v in votes:
            iso3     = v.country.isoa3_code.upper()
            vote_key = v.vote if v.vote in by_category else "absent"
            vote_map[iso3] = vote_key
            by_category[vote_key].append({
                "name":  v.country.name,
                "isoa3": iso3,
                "isoa2": v.country.isoa2_code,
                "vote":  vote_key,
            })

        votes_summary = {cat: len(lst) for cat, lst in by_category.items()}

        return Response({
            "resolution":    UNResolutionSerializer(resolution).data,
            "vote_map":      vote_map,
            "votes_summary": votes_summary,
            "by_category":   by_category,
        })


class UNVotesByCountryView(APIView):
    """
    GET /api/un-votes/country/<iso3>/
    All UN votes cast by a specific country.

    Optional filters:
      ?year=2022
      ?vote=yes|no|abstain|absent
      ?topic=Nuclear
      ?page_size=50  (default 100)
    """
    permission_classes = [AllowAny]

    def get(self, request, iso3):
        country = Country.objects.filter(
            isoa3_code=iso3.upper()
        ).first()

        if not country:
            return Response(
                {"detail": f"Country '{iso3}' not found"},
                status=404,
            )

        qs = UNVote.objects.filter(
            country=country
        ).select_related('resolution').order_by('-resolution__vote_date')

        # Filters
        year  = request.query_params.get('year')
        vote  = request.query_params.get('vote')
        topic = request.query_params.get('topic')

        if year:
            qs = qs.filter(resolution__vote_date__year=year)
        if vote:
            qs = qs.filter(vote=vote.lower())
        if topic:
            qs = qs.filter(resolution__topic_tags__contains=[topic])

        # Pagination
        try:
            page_size = min(int(request.query_params.get('page_size', 100)), 500)
        except ValueError:
            page_size = 100

        total = qs.count()
        votes = qs[:page_size]

        # Voting pattern summary
        summary = (
            UNVote.objects
            .filter(country=country)
            .values('vote')
            .annotate(count=Count('id'))
        )
        vote_summary = {row['vote']: row['count'] for row in summary}

        return Response({
            "country": {
                "name":      country.name,
                "full_name": country.full_name,
                "isoa3":     country.isoa3_code,
                "isoa2":     country.isoa2_code,
            },
            "total_votes":   total,
            "vote_summary":  vote_summary,
            "votes":         UNVoteSerializer(votes, many=True).data,
        })


class UNVoteAlignmentView(APIView):
    """
    GET /api/un-votes/alignment/?country_a=IND&country_b=CHN
    Computes voting alignment score between two countries (% of shared votes).

    Optional: ?year=2022  ?topic=Nuclear
    """
    permission_classes = [AllowAny]

    def get(self, request):
        iso_a = (request.query_params.get('country_a') or '').upper()
        iso_b = (request.query_params.get('country_b') or '').upper()

        if not iso_a or not iso_b:
            return Response(
                {"detail": "Both country_a and country_b ISO-A3 codes are required"},
                status=400,
            )

        country_a = Country.objects.filter(isoa3_code=iso_a).first()
        country_b = Country.objects.filter(isoa3_code=iso_b).first()

        if not country_a:
            return Response({"detail": f"Country '{iso_a}' not found"}, status=404)
        if not country_b:
            return Response({"detail": f"Country '{iso_b}' not found"}, status=404)

        # Optional filters
        year  = request.query_params.get('year')
        topic = request.query_params.get('topic')

        votes_a_qs = UNVote.objects.filter(country=country_a).select_related('resolution')
        votes_b_qs = UNVote.objects.filter(country=country_b).select_related('resolution')

        if year:
            votes_a_qs = votes_a_qs.filter(resolution__vote_date__year=year)
            votes_b_qs = votes_b_qs.filter(resolution__vote_date__year=year)
        if topic:
            votes_a_qs = votes_a_qs.filter(resolution__topic_tags__contains=[topic])
            votes_b_qs = votes_b_qs.filter(resolution__topic_tags__contains=[topic])

        # Build lookup: rcid → vote
        votes_a = {v.resolution.rcid: v.vote for v in votes_a_qs}
        votes_b = {v.resolution.rcid: v.vote for v in votes_b_qs}

        common_rcids = set(votes_a.keys()) & set(votes_b.keys())

        if not common_rcids:
            return Response({
                "country_a": iso_a,
                "country_b": iso_b,
                "common_resolutions": 0,
                "alignment_score": None,
                "detail": "No common resolutions found",
            })

        # Count matching votes on shared resolutions
        agreed  = sum(1 for r in common_rcids if votes_a[r] == votes_b[r])
        total   = len(common_rcids)
        score   = round(agreed / total * 100, 2)

        # Disagreement breakdown (where they diverged)
        disagreements = [
            {
                "rcid":    r,
                f"{iso_a}_vote": votes_a[r],
                f"{iso_b}_vote": votes_b[r],
            }
            for r in common_rcids
            if votes_a[r] != votes_b[r]
        ][:20]  # cap at 20 for response size

        return Response({
            "country_a":           {"name": country_a.name, "isoa3": iso_a},
            "country_b":           {"name": country_b.name, "isoa3": iso_b},
            "common_resolutions":  total,
            "agreed":              agreed,
            "alignment_score":     score,
            "filters_applied":     {"year": year, "topic": topic},
            "top_disagreements":   disagreements,
        })


class EventHeatmapBlocFilteredView(APIView):
    """
    GET /api/events/<pk>/heatmap/?bloc=nato
    Same as EventHeatmapView but optionally filtered to a geopolitical bloc.
    Also returns UN voting summary per country if available.
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        bloc_slug = request.query_params.get('bloc', '').lower().strip()

        country_filter = Q(event_id=pk)

        if bloc_slug:
            bloc = CountryBloc.objects.filter(slug=bloc_slug).first()
            if not bloc:
                return Response(
                    {"detail": f"Bloc '{bloc_slug}' not found"},
                    status=404,
                )
            country_filter &= Q(country__blocs__slug=bloc_slug)

        heatmap = (
            Statement.objects
            .filter(country_filter)
            .values(
                'country__name',
                'country__isoa3_code',
                'country__isoa2_code',
                'country__full_name',
                'country__lat',
                'country__lng',
            )
            .annotate(statement_count=Count('id'))
            .order_by('-statement_count')
        )

        return Response([
            {
                "country":         row['country__name'],
                "isoa3_code":      row['country__isoa3_code'],
                "isoa2_code":      row['country__isoa2_code'],
                "full_name":       row['country__full_name'],
                "statement_count": row['statement_count'],
            }
            for row in heatmap
        ])


# ─────────────────────────────────────────────────────────────────────────────
# Ask GeoStance AI — Resolution Report Generation
# ─────────────────────────────────────────────────────────────────────────────

class GenerateResolutionReportView(APIView):
    """
    POST /api/un-resolutions/generate-report/

    Generates a formatted .docx intelligence report for one or more UN
    resolutions using structured DB data + LLM-generated analysis.

    Request body (JSON):
    {
        "resolution_ids": [1, 2],           // required — UNResolution PKs
        "feature": "all",                   // required — see below
        "question": "...",                  // required only for "custom"
        "countries": ["France", "China"]    // optional — only for "custom"
    }

    feature values:
        "all"       — Full 5-section report (top 20 countries by significance)
        "analyze"   — Section 1: Resolution Overview only
        "compare"   — Section 2: Voting Behavior only
        "blocs"     — Section 3: Bloc Alignments only
        "timeline"  — Section 4: Voting Trends only
        "themes"    — Section 5: Key Themes only
        "custom"    — All sections scoped to specified countries + free question

    Returns: application/vnd.openxmlformats-officedocument.wordprocessingml.document
    """
    permission_classes = [AllowAny]

    _VALID_FEATURES = {"all", "analyze", "compare", "blocs", "timeline", "themes", "custom"}

    def post(self, request, *args, **kwargs):
        # ── Parse + validate request ─────────────────────────────────────────
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, AttributeError):
            return HttpResponseBadRequest("Invalid JSON body.")

        resolution_ids = body.get("resolution_ids", [])
        feature        = body.get("feature", "all").lower().strip()
        question       = body.get("question", "").strip()
        countries      = body.get("countries", [])   # list[str], for "custom"

        if not resolution_ids or not isinstance(resolution_ids, list):
            return HttpResponseBadRequest("'resolution_ids' must be a non-empty list.")
        if feature not in self._VALID_FEATURES:
            return HttpResponseBadRequest(
                f"'feature' must be one of: {', '.join(sorted(self._VALID_FEATURES))}"
            )
        if feature == "custom" and not question and not countries:
            return HttpResponseBadRequest(
                "Provide 'question' or 'countries' when feature='custom'."
            )

        # ── Build context (DB queries, no LLM) ──────────────────────────────
        from core.utils.report_context import build_report_context
        from core.utils.report_llm import generate_all_sections, generate_single_section
        from core.utils.report_builder import build_docx

        try:
            ctx = build_report_context(
                resolution_ids=resolution_ids,
                top_n=20,
                custom_countries=countries if (feature == "custom" and countries) else None,
            )
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except Exception as exc:
            logger.exception("report_context failed: %s", exc)
            return HttpResponseBadRequest("Failed to load resolution data.")

        # ── Generate LLM sections ────────────────────────────────────────────
        try:
            if feature == "all":
                sections = generate_all_sections(ctx)
            else:
                sections = generate_single_section(feature, ctx, question=question)
        except Exception as exc:
            logger.exception("LLM generation failed: %s", exc)
            return HttpResponseBadRequest("LLM generation failed — please retry.")

        # ── Build .docx ──────────────────────────────────────────────────────
        try:
            docx_buf = build_docx(ctx, sections, feature, question=question)
        except Exception as exc:
            logger.exception("docx build failed: %s", exc)
            return HttpResponseBadRequest("Document generation failed.")

        # ── Filename ─────────────────────────────────────────────────────────
        symbol_slug = (
            ctx.resolutions[0].un_symbol
            .replace("/", "-").replace(" ", "_").replace("(", "").replace(")", "")
        )
        filename = f"GeoStance_{feature}_{symbol_slug}.docx"
        if len(ctx.resolutions) > 1:
            filename = f"GeoStance_{feature}_multi_{len(ctx.resolutions)}_resolutions.docx"

        return FileResponse(
            docx_buf,
            as_attachment=True,
            filename=filename,
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Ask GeoStance AI — Chat Endpoint (JSON, not .docx)
# ─────────────────────────────────────────────────────────────────────────────

class AskGeoStanceView(APIView):
    """
    POST /api/un-resolutions/ask/
    Chat-style companion to GenerateResolutionReportView.
    Runs the same LLM pipeline but returns {answer, feature} JSON so the
    frontend can display the response inline rather than downloading a file.
    """
    permission_classes = [AllowAny]
    _VALID_FEATURES = {"analyze", "compare", "blocs", "timeline", "themes", "custom"}

    def post(self, request, *args, **kwargs):
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, AttributeError):
            return HttpResponseBadRequest("Invalid JSON body.")

        resolution_ids = body.get("resolution_ids", [])
        feature        = body.get("feature", "analyze").lower().strip()
        question       = body.get("question", "").strip()
        countries      = body.get("countries", [])

        if not resolution_ids or not isinstance(resolution_ids, list):
            return HttpResponseBadRequest("'resolution_ids' must be a non-empty list.")
        if feature not in self._VALID_FEATURES:
            return HttpResponseBadRequest(
                f"'feature' must be one of: {', '.join(sorted(self._VALID_FEATURES))}"
            )
        if feature == "custom" and not question:
            return HttpResponseBadRequest("Provide 'question' when feature='custom'.")

        from core.utils.report_context import build_report_context
        from core.utils.report_llm import generate_single_section

        try:
            ctx = build_report_context(
                resolution_ids=resolution_ids,
                top_n=20,
                custom_countries=countries if (feature == "custom" and countries) else None,
            )
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except Exception as exc:
            logger.exception("build_report_context failed: %s", exc)
            return HttpResponseBadRequest("Failed to load resolution data.")

        try:
            sections = generate_single_section(feature, ctx, question=question)
            answer   = next(iter(sections.values()), "[No response generated.]")
        except Exception as exc:
            logger.exception("LLM generation failed: %s", exc)
            return HttpResponseBadRequest("LLM generation failed — please retry.")

        return Response({"answer": answer, "feature": feature})
