"""
core/utils/report_llm.py
========================
LLM prompt construction and generation for each report section.
Uses NVIDIA NIM (Llama-3.3-70B) — same free-tier API used throughout the codebase.

Rate limit: ~10 RPM on free tier → 6 s delay between calls.
Each public function returns a plain string (the LLM's response).
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from core.utils.report_context import BlocRow, CountryVoteRow, ReportContext, ResolutionMeta

logger = logging.getLogger(__name__)

_CALL_DELAY = 6.5   # seconds between LLM calls (stays under 10 RPM)
_MODEL      = "meta/llama-3.3-70b-instruct"
_MAX_TOKENS = 1200  # per section — keeps each call fast


def _client():
    from core.services.rag_service import get_nvidia_client
    return get_nvidia_client()


def _call(system: str, user: str) -> str:
    """Single LLM call with retry on rate-limit."""
    for attempt in range(3):
        try:
            resp = _client().chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=_MAX_TOKENS,
                temperature=0.3,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            msg = str(exc).lower()
            if "rate" in msg or "429" in msg:
                wait = (attempt + 1) * 30
                logger.warning("LLM rate limit (attempt %d) — waiting %ds", attempt + 1, wait)
                time.sleep(wait)
            else:
                logger.error("LLM call failed: %s", exc)
                return "[LLM generation failed — please retry.]"
    return "[LLM unavailable after retries.]"


# ── Context formatters ────────────────────────────────────────────────────────

def _fmt_resolution(r: ResolutionMeta) -> str:
    return (
        f"Resolution: {r.un_symbol}\n"
        f"Title: {r.title}\n"
        f"Date: {r.vote_date}  |  Body: {r.body}  |  Event: {r.event_title}\n"
        f"Vote: {r.votes_yes} In Favour / {r.votes_no} Against / "
        f"{r.votes_abstain} Abstaining / {r.votes_absent} Absent\n"
        f"Summary: {r.explanation or r.resolution_text or 'No summary available.'}\n"
        f"Tags: {', '.join((r.ai_tags + r.topic_tags)[:10])}"
    )


def _fmt_countries(rows: list[CountryVoteRow]) -> str:
    lines = ["Country | Vote | Blocs"]
    for r in rows:
        blocs = ", ".join(r.blocs[:3]) or "—"
        lines.append(f"{r.name} ({r.iso3}) | {r.vote.upper()} | {blocs}")
    return "\n".join(lines)


def _fmt_countries_multi(rows: list[CountryVoteRow], symbols: list[str]) -> str:
    header = "Country | " + " | ".join(symbols)
    lines = [header]
    for r in rows:
        votes = " | ".join(r.multi_votes.get(sym, "—").upper() for sym in symbols)
        lines.append(f"{r.name} ({r.iso3}) | {votes}")
    return "\n".join(lines)


def _fmt_blocs(bloc_rows: list[BlocRow]) -> str:
    lines = ["Bloc | Members Voted | Yes | No | Abstain | % Yes"]
    for b in bloc_rows:
        lines.append(
            f"{b.name} | {b.yes + b.no + b.abstain} | "
            f"{b.yes} | {b.no} | {b.abstain} | {b.pct_yes}%"
        )
    return "\n".join(lines)


# ── Section generators ────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a senior UN geopolitical analyst producing a formal intelligence "
    "report. Write in clear, professional prose. Be specific and factual. "
    "Do not add caveats or disclaimers. "
    "Base every claim strictly on the resolution data provided — do not draw on "
    "external training knowledge."
)

# Stricter variant used for the interactive chat endpoint (/ask/).
# The model must refuse questions that fall outside the provided resolution data.
_SYSTEM_CHAT = (
    "You are a UN geopolitical analyst assistant. "
    "Your ONLY source of information is the resolution data block supplied in each message. "
    "Follow these rules without exception:\n"
    "1. Answer ONLY questions that can be answered from the provided resolution data.\n"
    "2. Never use training knowledge, general world facts, or information not present "
    "in the data block below.\n"
    "3. If the question cannot be answered from the provided data, respond exactly: "
    "\"I can only answer questions about the selected resolutions. "
    "Please ask something about their voting patterns, countries, blocs, or themes.\"\n"
    "4. If the question is entirely unrelated to the selected resolutions, respond: "
    "\"This question is outside the scope of the selected resolutions.\"\n"
    "5. Do not speculate beyond what the data shows."
)


def generate_overview(ctx: ReportContext) -> str:
    """Section 1 — Resolution Overview & Analysis."""
    res_blocks = "\n\n".join(_fmt_resolution(r) for r in ctx.resolutions)
    country_block = _fmt_countries(ctx.country_rows)

    user = f"""Write a structured resolution overview covering:
1. What this resolution does and why it matters geopolitically.
2. The overall voting outcome and what it signals.
3. Key observations about the voting split among the top countries below.

RESOLUTION DATA:
{res_blocks}

TOP {len(ctx.country_rows)} COUNTRIES AND THEIR VOTES:
{country_block}

Format:
- 2–3 paragraphs of analysis
- Use country names naturally in the text
- End with a one-sentence geopolitical significance statement"""

    time.sleep(_CALL_DELAY)
    return _call(_SYSTEM, user)


def generate_voting_behavior(ctx: ReportContext) -> str:
    """Section 2 — Voting Behavior per Country."""
    if ctx.is_multi:
        symbols = [r.un_symbol for r in ctx.resolutions]
        country_block = _fmt_countries_multi(ctx.country_rows, symbols)
        prompt_extra = (
            f"Multiple resolutions are being compared: {', '.join(symbols)}.\n"
            "Highlight countries that changed their vote between resolutions."
        )
    else:
        country_block = _fmt_countries(ctx.country_rows)
        prompt_extra = ""

    user = f"""Analyze the voting behavior of each of the following countries on the resolution(s).
For each country write 1–2 sentences explaining:
- What position they took (In Favour / Against / Abstaining)
- The likely geopolitical reasoning behind their position
- Any notable alignments or contradictions with their usual stance

{prompt_extra}

COUNTRIES AND VOTES:
{country_block}

RESOLUTION CONTEXT:
{_fmt_resolution(ctx.resolutions[0])}

Format: For each country use the format:
**[Country Name]** ([Vote]): [analysis sentence(s)]"""

    time.sleep(_CALL_DELAY)
    return _call(_SYSTEM, user)


def generate_bloc_analysis(ctx: ReportContext) -> str:
    """Section 3 — Bloc Alignments."""
    country_block = _fmt_countries(ctx.country_rows)
    bloc_block    = _fmt_blocs(ctx.bloc_rows)
    res_block     = _fmt_resolution(ctx.resolutions[0])

    user = f"""Analyze the voting bloc alignments for this resolution.
Cover:
1. Which blocs voted cohesively and which were divided.
2. Notable exceptions within blocs (e.g. a NATO member abstaining).
3. The geopolitical meaning of these alignments — what do the splits reveal?

RESOLUTION:
{res_block}

BLOC VOTING SUMMARY:
{bloc_block}

TOP COUNTRIES (for exceptions and examples):
{country_block}

Format: 3–4 structured paragraphs, one per major bloc group or theme."""

    time.sleep(_CALL_DELAY)
    return _call(_SYSTEM, user)


def generate_trends(ctx: ReportContext) -> str:
    """Section 4 — Voting Trends Over Time (multi-resolution only)."""
    if not ctx.is_multi:
        return "Select multiple resolutions to enable trend analysis."

    symbols  = [r.un_symbol for r in ctx.resolutions]
    dates    = [r.vote_date for r in ctx.resolutions]
    matrix   = _fmt_countries_multi(ctx.country_rows, symbols)

    user = f"""Analyze how the voting positions of these countries shifted across the following
resolutions over time. The resolutions are ordered chronologically.

Resolutions (oldest → newest):
{chr(10).join(f"  {sym} ({dt})" for sym, dt in zip(symbols, dates))}

COUNTRY VOTE MATRIX:
{matrix}

Cover:
1. Countries that consistently voted the same way (stable positions).
2. Countries that shifted — and the direction of that shift (softening / hardening).
3. What these trends reveal about evolving diplomatic alignments.

Format: 3 paragraphs — one for each point above. Be specific with country names."""

    time.sleep(_CALL_DELAY)
    return _call(_SYSTEM, user)


def generate_themes(ctx: ReportContext) -> str:
    """Section 5 — Key Themes and Topics."""
    res_blocks = "\n\n".join(_fmt_resolution(r) for r in ctx.resolutions)
    tags       = ", ".join(ctx.all_tags[:20]) or "—"

    user = f"""Extract and explain the key geopolitical themes and topics across the following
resolution(s).

RESOLUTION DATA:
{res_blocks}

SUBJECT TAGS: {tags}

Produce:
1. A prioritised list of the top 5–7 themes (bold heading + 2-sentence explanation each).
2. A short paragraph on how these themes connect to broader UN agenda priorities.

Format: Use **Theme Name**: explanation format for the list."""

    time.sleep(_CALL_DELAY)
    return _call(_SYSTEM, user)


def generate_custom(ctx: ReportContext, question: str) -> str:
    """Chat-style custom question scoped strictly to the provided resolutions."""
    symbols       = ", ".join(r.un_symbol for r in ctx.resolutions)
    res_blocks    = "\n\n".join(_fmt_resolution(r) for r in ctx.resolutions)
    country_block = _fmt_countries(ctx.country_rows)
    bloc_block    = _fmt_blocs(ctx.bloc_rows)

    user = (
        f"The user is asking about these specific UN resolutions: {symbols}\n\n"
        f"USER QUESTION: \"{question}\"\n\n"
        "Answer using ONLY the data below. "
        "If the question is not answerable from this data or is unrelated to these "
        "resolutions, say so explicitly — do not guess or use outside knowledge.\n\n"
        f"RESOLUTION DATA:\n{res_blocks}\n\n"
        f"COUNTRY VOTES:\n{country_block}\n\n"
        f"BLOC ALIGNMENTS:\n{bloc_block}\n\n"
        f"TAGS: {', '.join(ctx.all_tags[:15])}"
    )

    time.sleep(_CALL_DELAY)
    return _call(_SYSTEM_CHAT, user)


# ── Orchestrator ──────────────────────────────────────────────────────────────

FeatureType = Literal["all", "analyze", "compare", "blocs", "timeline", "themes", "custom"]


def generate_all_sections(ctx: ReportContext) -> dict[str, str]:
    """Generate all 5 sections for Feature 1 (full report)."""
    return {
        "overview":  generate_overview(ctx),
        "behavior":  generate_voting_behavior(ctx),
        "blocs":     generate_bloc_analysis(ctx),
        "trends":    generate_trends(ctx),
        "themes":    generate_themes(ctx),
    }


def generate_single_section(
    feature: FeatureType,
    ctx: ReportContext,
    question: str = "",
) -> dict[str, str]:
    """Generate only the section matching `feature`."""
    if feature == "analyze":
        return {"overview": generate_overview(ctx)}
    if feature == "compare":
        return {"behavior": generate_voting_behavior(ctx)}
    if feature == "blocs":
        return {"blocs": generate_bloc_analysis(ctx)}
    if feature == "timeline":
        return {"trends": generate_trends(ctx)}
    if feature == "themes":
        return {"themes": generate_themes(ctx)}
    if feature == "custom":
        return {"custom": generate_custom(ctx, question)}
    return generate_all_sections(ctx)
