"""
core/utils/report_context.py
============================
Assembles all structured data needed to generate a resolution analysis report.
Pure DB queries — no LLM calls here.

The top-20 country selection is deterministic:
  Priority 1: All "No" voters (most geopolitically significant position)
  Priority 2: "Abstain" voters ranked by significance tier
  Priority 3: "Yes" voters ranked by significance tier
  Capped at 20 total.

Significance tiers (descending):
  T1: P5 permanent SC members
  T2: G7 members
  T3: G20 members
  T4: BRICS members
  T5: Everyone else (alphabetical)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from core.models import Country, CountryBloc, UNResolution, UNVote

# ── Significance tiers for country ranking ────────────────────────────────────
_P5   = {"USA", "GBR", "FRA", "CHN", "RUS"}
_G7   = {"USA", "GBR", "FRA", "DEU", "ITA", "JPN", "CAN"}
_G20  = {
    "USA", "GBR", "FRA", "DEU", "ITA", "JPN", "CAN",
    "CHN", "RUS", "IND", "BRA", "ZAF", "SAU", "ARG",
    "AUS", "KOR", "MEX", "IDN", "TUR", "ARE",
}
_BRICS = {"CHN", "RUS", "IND", "BRA", "ZAF", "EGY", "ETH", "IRN", "ARE", "SAU"}


def _significance_tier(iso3: str) -> int:
    if iso3 in _P5:
        return 1
    if iso3 in _G7:
        return 2
    if iso3 in _G20:
        return 3
    if iso3 in _BRICS:
        return 4
    return 5


# ── Data containers ───────────────────────────────────────────────────────────

@dataclass
class CountryVoteRow:
    iso3:        str
    name:        str
    vote:        str          # yes | no | abstain | absent | not_member
    blocs:       list[str]    # e.g. ["NATO", "EU", "G7"]
    # per-resolution votes when multiple resolutions are selected
    # {un_symbol: vote_str}
    multi_votes: dict[str, str] = field(default_factory=dict)


@dataclass
class BlocRow:
    name:     str
    slug:     str
    total:    int
    yes:      int
    no:       int
    abstain:  int
    absent:   int
    pct_yes:  float   # yes / (yes+no+abstain) * 100


@dataclass
class ResolutionMeta:
    pk:               int
    un_symbol:        str
    title:            str
    vote_date:        str          # ISO string
    body:             str          # UNGA | UNSC | UNHRC
    resolution_text:  str
    explanation:      str          # LLM-generated
    ai_tags:          list[str]
    topic_tags:       list[str]
    votes_yes:        int
    votes_no:         int
    votes_abstain:    int
    votes_absent:     int
    event_title:      str


@dataclass
class ReportContext:
    resolutions:     list[ResolutionMeta]
    country_rows:    list[CountryVoteRow]    # top-20 (or custom) countries
    bloc_rows:       list[BlocRow]
    # only populated when 2+ resolutions: {iso3: {symbol: vote}}
    vote_matrix:     dict[str, dict[str, str]]
    # merged/deduped tags across all resolutions
    all_tags:        list[str]
    is_multi:        bool                    # True if 2+ resolutions selected


# ── Public API ────────────────────────────────────────────────────────────────

def build_report_context(
    resolution_ids:   list[int],
    top_n:            int = 20,
    custom_countries: list[str] | None = None,
) -> ReportContext:
    """
    Fetch all data needed for the report from the DB and return a ReportContext.

    Args:
        resolution_ids:   PKs of UNResolution rows to include.
        top_n:            Max countries to include (default 20).
        custom_countries: If provided (Feature 6 / custom), use exactly these
                          countries (matched by name or ISO-3) instead of top_n.
    """
    resolutions = list(
        UNResolution.objects
        .filter(pk__in=resolution_ids)
        .select_related("event")
        .order_by("vote_date")
    )
    if not resolutions:
        raise ValueError("No resolutions found for given IDs.")

    # ── Build per-resolution vote dicts ───────────────────────────────────────
    # {resolution_pk: {iso3: vote_str}}
    res_votes: dict[int, dict[str, str]] = {}
    for res in resolutions:
        votes_qs = (
            UNVote.objects
            .filter(resolution=res)
            .select_related("country")
        )
        res_votes[res.pk] = {
            v.country.isoa3_code.upper(): v.vote
            for v in votes_qs
        }

    # ── Build country→blocs lookup ────────────────────────────────────────────
    country_blocs: dict[str, list[str]] = defaultdict(list)
    for bloc in CountryBloc.objects.prefetch_related("countries"):
        for country in bloc.countries.all():
            country_blocs[country.isoa3_code.upper()].append(bloc.name)

    # ── Determine primary resolution (first chronologically for single; ────────
    #    first selected for multi)
    primary_res    = resolutions[0]
    primary_votes  = res_votes[primary_res.pk]
    is_multi       = len(resolutions) > 1

    # ── Select countries ──────────────────────────────────────────────────────
    if custom_countries:
        selected_iso3s = _resolve_custom_countries(custom_countries, primary_votes)
    else:
        selected_iso3s = _top_n_countries(primary_votes, top_n)

    # ── Build CountryVoteRow list ─────────────────────────────────────────────
    # Pre-fetch Country objects for selected iso3s
    country_objs: dict[str, Country] = {
        c.isoa3_code.upper(): c
        for c in Country.objects.filter(isoa3_code__in=selected_iso3s)
    }

    country_rows: list[CountryVoteRow] = []
    for iso3 in selected_iso3s:
        country = country_objs.get(iso3)
        if not country:
            continue
        multi_votes = {
            res.un_symbol: res_votes[res.pk].get(iso3, "—")
            for res in resolutions
        }
        country_rows.append(CountryVoteRow(
            iso3=iso3,
            name=country.name,
            vote=primary_votes.get(iso3, "absent"),
            blocs=country_blocs.get(iso3, []),
            multi_votes=multi_votes,
        ))

    # ── Build BlocRow list ────────────────────────────────────────────────────
    bloc_rows = _build_bloc_rows(primary_votes, country_blocs)

    # ── Vote matrix for multi-resolution comparison ───────────────────────────
    vote_matrix: dict[str, dict[str, str]] = {}
    if is_multi:
        for iso3 in selected_iso3s:
            vote_matrix[iso3] = {
                res.un_symbol: res_votes[res.pk].get(iso3, "—")
                for res in resolutions
            }

    # ── Merged tags ───────────────────────────────────────────────────────────
    all_tags = _merge_tags(resolutions)

    # ── Resolution metadata ───────────────────────────────────────────────────
    res_metas = [_to_meta(r, res_votes[r.pk]) for r in resolutions]

    return ReportContext(
        resolutions=res_metas,
        country_rows=country_rows,
        bloc_rows=bloc_rows,
        vote_matrix=vote_matrix,
        all_tags=all_tags,
        is_multi=is_multi,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _top_n_countries(votes: dict[str, str], n: int) -> list[str]:
    """
    Pick top-n countries ranked by:
      1. Vote priority: No > Abstain > Yes > Absent
      2. Within each vote group: significance tier (P5 > G7 > G20 > BRICS > rest)
      3. Within same tier: alphabetical ISO-3
    """
    vote_priority = {"no": 0, "abstain": 1, "yes": 2, "absent": 3, "not_member": 4}

    ranked = sorted(
        votes.items(),
        key=lambda kv: (
            vote_priority.get(kv[1], 5),
            _significance_tier(kv[0]),
            kv[0],
        ),
    )
    return [iso3 for iso3, _ in ranked[:n]]


def _resolve_custom_countries(
    names: list[str],
    votes: dict[str, str],
) -> list[str]:
    """
    Resolve custom country names/ISO-3 codes to ISO-3 codes that exist in votes.
    Matches case-insensitively against both ISO-3 and country name.
    """
    all_countries = {
        c.isoa3_code.upper(): c
        for c in Country.objects.filter(isoa3_code__in=votes.keys())
    }
    name_map = {c.name.lower(): iso3 for iso3, c in all_countries.items()}
    name_map.update({iso3.lower(): iso3 for iso3 in all_countries})

    resolved = []
    for raw in names:
        key = raw.strip().lower()
        iso3 = name_map.get(key)
        if iso3 and iso3 not in resolved:
            resolved.append(iso3)
    return resolved


def _build_bloc_rows(
    votes: dict[str, str],
    country_blocs: dict[str, list[str]],
) -> list[BlocRow]:
    bloc_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "yes": 0, "no": 0, "abstain": 0, "absent": 0}
    )

    for iso3, vote in votes.items():
        for bloc_name in country_blocs.get(iso3, []):
            bc = bloc_counts[bloc_name]
            bc["total"] += 1
            if vote in bc:
                bc[vote] += 1
            else:
                bc["absent"] += 1

    rows = []
    for bloc_name, bc in sorted(bloc_counts.items()):
        voted = bc["yes"] + bc["no"] + bc["abstain"]
        pct = round(bc["yes"] / voted * 100, 1) if voted else 0.0
        # Get slug from DB
        bloc_obj = CountryBloc.objects.filter(name=bloc_name).first()
        rows.append(BlocRow(
            name=bloc_name,
            slug=bloc_obj.slug if bloc_obj else bloc_name.lower(),
            total=bc["total"],
            yes=bc["yes"],
            no=bc["no"],
            abstain=bc["abstain"],
            absent=bc["absent"],
            pct_yes=pct,
        ))

    # Sort by most members with actual votes
    rows.sort(key=lambda r: -(r.yes + r.no + r.abstain))
    return rows


def _merge_tags(resolutions: list[UNResolution]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for res in resolutions:
        for tag in (res.ai_tags or []) + (res.topic_tags or []):
            key = tag.lower()
            if key not in seen:
                seen.add(key)
                merged.append(tag)
    return merged


def _to_meta(res: UNResolution, votes: dict[str, str]) -> ResolutionMeta:
    yes     = sum(1 for v in votes.values() if v == "yes")
    no      = sum(1 for v in votes.values() if v == "no")
    abstain = sum(1 for v in votes.values() if v == "abstain")
    absent  = sum(1 for v in votes.values() if v in ("absent", "not_member"))
    return ResolutionMeta(
        pk=res.pk,
        un_symbol=res.un_symbol,
        title=res.title,
        vote_date=res.vote_date.strftime("%d %B %Y") if res.vote_date else "—",
        body=res.body,
        resolution_text=res.resolution_text or "",
        explanation=res.explanation or "",
        ai_tags=res.ai_tags or [],
        topic_tags=res.topic_tags or [],
        votes_yes=yes,
        votes_no=no,
        votes_abstain=abstain,
        votes_absent=absent,
        event_title=res.event.title if res.event else "—",
    )
