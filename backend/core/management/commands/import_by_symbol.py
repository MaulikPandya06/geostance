"""
Management command: import_by_symbol
=====================================
Import specific UN resolutions by their document symbol (e.g. A/RES/ES-11/1),
bypassing the OAI-PMH date-range queries that only cover recently re-indexed
records.

Strategy
--------
  1. Search UNDL HTML for each requested symbol to discover its numeric UNDL
     record ID (the number that appears in the record URL, e.g. /record/3872625).
  2. Use the OAI-PMH ``GetRecord`` verb with that specific identifier —
     ``oai:digitallibrary.un.org:{id}`` — to fetch the MARC21 XML record.
     GetRecord bypasses date-range filtering and works for any record in UNDL
     regardless of when it was indexed.
  3. Parse the MARC21 XML with the same logic as the regular OAI-PMH harvester.
  4. Upsert the UNResolution in the database.

Why not use date-range OAI-PMH?
---------------------------------
UNDL's OAI-PMH date-range windows only cover records that were indexed (or
re-indexed) AFTER the OAI-PMH system was activated.  Resolutions adopted in
2022–2024 were already in UNDL's catalog before the OAI service was set up, so
they have no recent OAI datestamp and return ``noRecordsMatch`` for every
date range.

Usage
-----
  # Import specific symbols
  python manage.py import_by_symbol A/RES/ES-11/1 A/RES/ES-11/2 A/RES/ES-10/21

  # Use the built-in preset for Ukraine Emergency Special Session resolutions
  python manage.py import_by_symbol --preset ukraine

  # Use the built-in preset for Israel-Gaza Emergency Special Session resolutions
  python manage.py import_by_symbol --preset israel-gaza

  # Import all presets at once
  python manage.py import_by_symbol --preset all

  # Dry-run: show what would be imported
  python manage.py import_by_symbol --preset ukraine --dry-run

  # Skip country-vote scraping (faster — metadata only)
  python manage.py import_by_symbol --preset all --skip-country-votes
"""

import re
import time
from xml.etree import ElementTree as ET

import requests
from django.core.management.base import BaseCommand

from core.ingestion.un_library import (
    HEADERS,
    MARC21_NS,
    OAI_BASE,
    OAI_NS,
    fetch_country_votes,
    normalize_un_symbol,
    tags_from_symbol,
    _parse_marc_record,
    _is_resolution_record,
    _inject_ns,
    _get_session,
)
from core.models import Country, UNResolution, UNVote

# ── Known symbol presets ──────────────────────────────────────────────────────
# Emergency Special Session 11 — Russia's aggression against Ukraine
PRESET_UKRAINE = [
    "A/RES/ES-11/1",  # 2022-03-02  Aggression against Ukraine (141–5–35)
    "A/RES/ES-11/2",  # 2022-03-24  Humanitarian consequences of the aggression (140–5–38)
    "A/RES/ES-11/3",  # 2022-10-12  Territorial integrity of Ukraine (143–5–35)
    "A/RES/ES-11/4",  # 2022-11-14  Remedy and reparation for aggression (94–14–73)
    "A/RES/ES-11/5",  # 2023-02-23  Principles for a comprehensive peace (141–7–32)
    "A/RES/ES-11/6",  # 2023-04-26  International register of damage (99–10–73)
    "A/RES/ES-11/7",  # 2024-02-23  Peaceful settlement of the question of Ukraine (93–18–65)
]

# Emergency Special Session 10 — Illegal Israeli actions in Occupied East Jerusalem / Palestine
PRESET_ISRAEL_GAZA = [
    "A/RES/ES-10/21",  # 2023-10-27  Protection of civilians — Gaza (121–14–44)
    "A/RES/ES-10/22",  # 2023-12-12  Immediate humanitarian ceasefire (153–10–23)
    "A/RES/ES-10/23",  # 2024-05-10  UNRWA mandate (159–9–11)
    "A/RES/ES-10/24",  # 2024-09-18  UNRWA access (124–14–43)
]

# Key regular GA resolutions that are directly related to these conflicts
PRESET_RELATED_GA = [
    "A/RES/77/229",   # 2022-12-23  Human rights in Ukraine stemming from Russian aggression
    "A/RES/78/264",   # 2023-12-22  Situation of human rights in Ukraine
    "A/RES/79/55",    # 2024-11-20  Situation of human rights in the Occupied Palestinian Territory
]

PRESETS: dict[str, list[str]] = {
    "ukraine":      PRESET_UKRAINE,
    "israel-gaza":  PRESET_ISRAEL_GAZA,
    "related-ga":   PRESET_RELATED_GA,
    "all":          PRESET_UKRAINE + PRESET_ISRAEL_GAZA + PRESET_RELATED_GA,
}

UNDL_SEARCH = "https://digitallibrary.un.org/search"
SCRAPE_DELAY = 1.5


class Command(BaseCommand):
    help = (
        "Import specific UN resolutions by symbol using UNDL HTML search + "
        "OAI-PMH GetRecord — bypasses OAI date-range gaps for 2022-2024 resolutions."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "symbols", nargs="*", type=str,
            help=(
                "One or more UN document symbols to import, e.g. "
                "A/RES/ES-11/1 A/RES/ES-10/21"
            ),
        )
        parser.add_argument(
            "--preset", type=str, choices=list(PRESETS.keys()), default=None,
            help=(
                "Import a named preset group of resolutions. "
                "Choices: " + ", ".join(PRESETS.keys())
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Fetch and parse records but do NOT write to the database.",
        )
        parser.add_argument(
            "--skip-country-votes", action="store_true", default=False,
            help="Skip country-vote scraping — import metadata only (much faster).",
        )
        parser.add_argument(
            "--inline-enrich", action="store_true", default=False,
            help="Run AI enrichment + event classification inline after import.",
        )

    def handle(self, *args, **options):
        symbols_raw = list(options["symbols"])
        preset      = options["preset"]
        dry_run     = options["dry_run"]
        skip_votes  = options["skip_country_votes"]
        inline      = options["inline_enrich"]

        if preset:
            symbols_raw = list(PRESETS[preset]) + symbols_raw

        if not symbols_raw:
            self.stderr.write(self.style.ERROR(
                "No symbols specified.  Pass symbols as arguments or use --preset."
            ))
            return

        # Normalise
        symbols = [normalize_un_symbol(s) for s in symbols_raw]

        if dry_run:
            self.stdout.write(self.style.WARNING("[DRY RUN] No data will be written."))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== Import by Symbol ===\n"
            f"  Symbols  : {len(symbols)}\n"
            f"  Dry run  : {dry_run}\n"
            f"  Skip votes: {skip_votes}"
        ))

        country_lookup: dict[str, Country] = {
            c.isoa3_code.upper(): c for c in Country.objects.all()
        }
        self.stdout.write(f"  Countries in DB: {len(country_lookup)}")

        created = updated = skipped = 0
        vote_rows_created = 0

        for symbol in symbols:
            self.stdout.write(f"\n  Processing {symbol!r} ...")

            # ── Step 1: find UNDL record ID ────────────────────────────────────
            undl_id = self._find_undl_id(symbol)
            if not undl_id:
                self.stdout.write(self.style.WARNING(
                    f"    Could not find UNDL ID for {symbol!r} — skipping."
                ))
                skipped += 1
                continue

            self.stdout.write(f"    Found UNDL ID: {undl_id}")

            # ── Step 2: fetch MARC21 via OAI GetRecord ─────────────────────────
            marc_xml = self._oai_get_record(undl_id)
            if not marc_xml:
                self.stdout.write(self.style.WARNING(
                    f"    OAI GetRecord failed for undl_id={undl_id} — skipping."
                ))
                skipped += 1
                continue

            # ── Step 3: parse MARC21 ───────────────────────────────────────────
            rec = self._parse_oai_get_record(marc_xml, symbol)
            if rec is None:
                self.stdout.write(self.style.WARNING(
                    f"    Could not parse MARC21 for {symbol!r} — skipping."
                ))
                skipped += 1
                continue

            self.stdout.write(
                f"    Parsed: {rec.un_symbol!r} | "
                f"title={rec.title[:50]!r} | date={rec.vote_date}"
            )

            if dry_run:
                self.stdout.write(self.style.SUCCESS(
                    f"    [DRY RUN] would upsert {rec.un_symbol!r}"
                ))
                created += 1
                continue

            # ── Step 4: upsert resolution ──────────────────────────────────────
            obj, was_created = self._upsert(rec)
            if obj is None:
                skipped += 1
                continue

            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"    Created pk={obj.pk}"))
            else:
                updated += 1
                self.stdout.write(f"    Updated pk={obj.pk}")

            # ── Step 5: country votes ──────────────────────────────────────────
            if not skip_votes:
                votes = self._import_votes(obj, rec, country_lookup)
                vote_rows_created += votes
                self.stdout.write(f"    Votes: {votes}")
                time.sleep(SCRAPE_DELAY)

        # ── Summary ────────────────────────────────────────────────────────────
        style = self.style.SUCCESS if skipped == 0 else self.style.WARNING
        self.stdout.write(style(
            f"\n=== Done ==="
            f"\n  Created : {created}"
            f"\n  Updated : {updated}"
            f"\n  Skipped : {skipped}"
            f"\n  Votes   : {vote_rows_created}"
        ))

        if not dry_run and (created + updated) > 0 and inline:
            self._post_import_inline()

    # ── UNDL ID discovery ──────────────────────────────────────────────────────

    def _find_undl_id(self, symbol: str) -> str | None:
        """
        Search UNDL HTML for the given symbol and return its numeric record ID.

        Tries two methods:
          1. UNDL search page — p=symbol:"SYMBOL" — and look for /record/NNN links.
          2. OAI-PMH ListIdentifiers with set=Voting+Data filtered by symbol
             (not all UNDL servers support this but worth trying).
        """
        session = _get_session()

        # Method 1: UNDL HTML search
        undl_id = self._search_html(session, symbol)
        if undl_id:
            return undl_id

        # Method 2: try the "all records" search without the cc filter
        undl_id = self._search_html(session, symbol, collection=None)
        if undl_id:
            return undl_id

        return None

    def _search_html(
        self, session: requests.Session, symbol: str, collection: str | None = "Voting Data"
    ) -> str | None:
        """GET the UNDL search page for `symbol` and extract the first record ID."""
        params: dict = {
            "p": f'symbol:"{symbol}"',
            "action_search": "Search",
            "of": "hb",      # HTML brief format
        }
        if collection:
            params["cc"] = collection

        for attempt in range(1, 4):
            try:
                resp = session.get(
                    UNDL_SEARCH, params=params, timeout=30, allow_redirects=True
                )
            except requests.RequestException as exc:
                self.stdout.write(f"    Search attempt {attempt} error: {exc}")
                time.sleep(5)
                continue

            # 202 = WAF / async challenge — retry after short wait
            if resp.status_code == 202:
                self.stdout.write(f"    Search returned 202 (attempt {attempt}) — retrying...")
                time.sleep(5)
                continue

            if not resp.ok:
                self.stdout.write(f"    Search HTTP {resp.status_code}")
                return None

            # Parse HTML and look for /record/{id} links
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")

            # Try href="/record/{id}" links
            for link in soup.find_all("a", href=re.compile(r"/record/\d+")):
                m = re.search(r"/record/(\d+)", link.get("href", ""))
                if m:
                    return m.group(1)

            # Fallback: search raw HTML for /record/digits patterns
            m = re.search(r"/record/(\d+)", resp.text)
            if m:
                return m.group(1)

            self.stdout.write(f"    Search returned HTML but no /record/NNN link found.")
            return None

        return None

    # ── OAI-PMH GetRecord ──────────────────────────────────────────────────────

    def _oai_get_record(self, undl_id: str) -> str | None:
        """
        Fetch a single MARC21 record via OAI-PMH GetRecord verb.
        Returns the raw XML text, or None on failure.
        """
        identifier = f"oai:digitallibrary.un.org:{undl_id}"
        params = {
            "verb":           "GetRecord",
            "identifier":     identifier,
            "metadataPrefix": "marcxml",
        }
        session = _get_session()

        for attempt in range(1, 4):
            try:
                resp = session.get(OAI_BASE, params=params, timeout=60)
            except requests.RequestException as exc:
                self.stdout.write(f"    OAI GetRecord error (attempt {attempt}): {exc}")
                time.sleep(10 * attempt)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 15 * attempt
                self.stdout.write(
                    f"    OAI GetRecord HTTP {resp.status_code} (attempt {attempt}) "
                    f"— retrying in {wait}s"
                )
                time.sleep(wait)
                continue

            if not resp.ok:
                self.stdout.write(f"    OAI GetRecord non-retryable HTTP {resp.status_code}")
                return None

            body = resp.text.strip()
            if "<html" in body[:300].lower():
                self.stdout.write(
                    f"    OAI GetRecord returned HTML instead of XML (attempt {attempt})"
                )
                time.sleep(10 * attempt)
                continue

            return body

        return None

    # ── MARC21 parsing ─────────────────────────────────────────────────────────

    def _parse_oai_get_record(self, xml_text: str, expected_symbol: str):
        """
        Parse an OAI-PMH GetRecord response and return a ResolutionRecord.
        Returns None if the record cannot be parsed or isn't a resolution.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            self.stdout.write(f"    XML parse error: {exc}")
            return None

        ns_oai  = {"oai":  OAI_NS}
        ns_marc = {"m":    MARC21_NS}

        # Check for OAI error
        error_el = root.find("oai:error", ns_oai)
        if error_el is not None:
            code = error_el.get("code", "unknown")
            msg  = (error_el.text or "").strip()
            self.stdout.write(f"    OAI error {code}: {msg}")
            return None

        # Locate the MARC21 <record> element (inside GetRecord > record > metadata)
        metadata_el = root.find(".//oai:metadata", ns_oai)
        if metadata_el is None:
            self.stdout.write("    No <metadata> element in GetRecord response")
            return None

        marc_el = (
            metadata_el.find(f"{{{MARC21_NS}}}record")
            or metadata_el.find("record")
        )
        if marc_el is None:
            self.stdout.write("    No MARC21 <record> element in metadata")
            return None

        # Inject namespace if missing
        if not marc_el.tag.startswith("{"):
            marc_el = _inject_ns(marc_el)

        # Get OAI datestamp for fallback vote_date
        datestamp = ""
        header_el = root.find(".//oai:header", ns_oai)
        if header_el is not None:
            ds_el = header_el.find("oai:datestamp", ns_oai)
            if ds_el is not None and ds_el.text:
                datestamp = ds_el.text.strip()

        rec = _parse_marc_record(marc_el, ns_marc, oai_datestamp=datestamp)
        if rec is None:
            self.stdout.write("    _parse_marc_record returned None (b-code filter?)")
            return None

        if not _is_resolution_record(rec):
            self.stdout.write(
                f"    Not a resolution (symbol={rec.un_symbol!r}) — skipping"
            )
            return None

        # Patch symbol if MARC is missing it (some older records omit 191$a)
        if not rec.un_symbol and expected_symbol:
            rec.un_symbol = expected_symbol

        return rec

    # ── DB upsert ─────────────────────────────────────────────────────────────

    def _upsert(self, rec) -> tuple:
        """Create or update UNResolution. Returns (instance, created_bool)."""
        from django.db import transaction

        if not rec.vote_date:
            self.stdout.write(self.style.WARNING("    No vote_date — skipping DB write"))
            return None, False

        symbol_tags = tags_from_symbol(rec.un_symbol)
        defaults = dict(
            undl_id=rec.undl_id,
            title=rec.title or "",
            vote_date=rec.vote_date,
            session=rec.session,
            body=rec.body,
            short_description=rec.short_description or "",
            resolution_text=rec.resolution_text or "",
            topic_tags=rec.topic_tags,
            symbol_tags=symbol_tags,
        )
        try:
            with transaction.atomic():
                if rec.un_symbol:
                    obj, created = UNResolution.objects.update_or_create(
                        un_symbol=rec.un_symbol, defaults=defaults
                    )
                else:
                    obj, created = UNResolution.objects.get_or_create(
                        undl_id=rec.undl_id, defaults={**defaults, "un_symbol": ""}
                    )
            return obj, created
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"    DB error: {exc}"))
            return None, False

    # ── Country votes ──────────────────────────────────────────────────────────

    def _import_votes(self, obj: UNResolution, rec, country_lookup: dict) -> int:
        """Scrape and save country votes for this resolution. Returns count saved."""
        from django.db import transaction

        country_votes = rec.country_votes or fetch_country_votes(rec.undl_id)
        if not country_votes:
            return 0

        existing = set(UNVote.objects.filter(resolution=obj).values_list("country_id", flat=True))
        new_votes = []
        for iso3, vote_str in country_votes.items():
            country = country_lookup.get(iso3.upper())
            if not country:
                continue
            if country.pk in existing:
                continue
            new_votes.append(UNVote(resolution=obj, country=country, vote=vote_str))
            existing.add(country.pk)

        if new_votes:
            with transaction.atomic():
                UNVote.objects.bulk_create(new_votes, ignore_conflicts=True)
        return len(new_votes)

    # ── Post-import inline enrich+classify ────────────────────────────────────

    def _post_import_inline(self) -> None:
        """Run AI enrichment and event classification inline on newly imported records."""
        from core.tasks import classify_resolution_to_event, enrich_resolution_with_ai

        qs = UNResolution.objects.filter(event__isnull=True, explanation__isnull=True)
        total = qs.count()
        self.stdout.write(
            f"\n  Running inline enrich -> classify on {total} resolution(s) ..."
        )
        enriched = linked = failed = 0
        for res in qs:
            try:
                enrich_resolution_with_ai(res.pk)
                enriched += 1
                result = classify_resolution_to_event(res.pk)
                if result.get("linked"):
                    linked += 1
                    self.stdout.write(
                        f"    pk={res.pk} {res.un_symbol} -> event_id={result['event_id']} "
                        f"(conf={result['confidence']:.3f})"
                    )
                else:
                    self.stdout.write(
                        f"    pk={res.pk} {res.un_symbol} -> no match "
                        f"(reason={result.get('reason', '?')[:80]})"
                    )
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"    pk={res.pk} error: {exc}"))

        self.stdout.write(self.style.SUCCESS(
            f"  Inline: enriched={enriched} linked={linked} failed={failed}"
        ))
