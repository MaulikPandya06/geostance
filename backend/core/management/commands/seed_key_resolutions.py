"""
Management command: seed_key_resolutions
=========================================
Directly create the most politically important UN resolutions that are NOT
accessible via OAI-PMH date-range queries (2022-2024 Emergency Special Session
resolutions on Ukraine and Palestine / Gaza).

WHY THIS COMMAND EXISTS
-----------------------
UNDL's OAI-PMH feed only contains:
  • Records re-indexed as part of an ongoing historical digitisation project
    (currently pushing 1979-1981 records — all 2022-2024 windows return
    noRecordsMatch)
  • Brand-new 2025-2026 records as they are voted

This means the landmark Emergency Special Session resolutions on
  • Ukraine     (A/RES/ES-11/*)  — 2022-2024
  • Palestine   (A/RES/ES-10/*)  — 2023-2024

are simply not reachable from UNDL OAI-PMH.  This command seeds them
directly with verified data so the event classifier can link them correctly.

DATA ACCURACY
-------------
All vote counts and dates come from official UN sources.  The short_description
and resolution_text fields are left intentionally sparse — the AI enrichment
step (``enrich_un_resolutions``) will populate them from the LLM using the
symbol and title.

Usage
-----
  python manage.py seed_key_resolutions
  python manage.py seed_key_resolutions --dry-run
  python manage.py seed_key_resolutions --inline-enrich     # enrich + classify
"""

import time
import time
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from core.ingestion.un_library import fetch_country_votes, tags_from_symbol
from core.models import Country, UNResolution, UNVote

# ── UNDL numeric record IDs ───────────────────────────────────────────────────
# These IDs come from https://digitallibrary.un.org/record/{id} pages found via
# web search.  They are needed so fetch_country_votes() can scrape per-country
# vote breakdowns.  Resolutions without a known UNDL ID will have undl_id=""
# and country-vote scraping will be skipped for them.
#
# Notes on confidence:
#   ✓ = symbol explicitly in PDF URL or record title exactly matches
#   ~ = best-effort match; verify at digitallibrary.un.org/record/{id}
_UNDL_IDS: dict[str, str] = {
    "A/RES/ES-11/1":  "3959039",   # ✓ Aggression against Ukraine
    "A/RES/ES-11/2":  "3966630",   # ✓ Humanitarian consequences of the aggression
    "A/RES/ES-11/3":  "3967950",   # ✓ Territorial integrity of Ukraine
    "A/RES/ES-11/4":  "3990673",   # ✓ Furtherance of remedy and reparation
    "A/RES/ES-11/5":  "4004933",   # ✓ Principles of the Charter for peace in Ukraine
    "A/RES/ES-11/6":  "3994481",   # ~ Intl register of damage (Furtherance ES-11/6)
    "A/RES/ES-11/7":  "4076916",   # ✓ Peaceful settlement of the question of Ukraine
    "A/RES/ES-10/21": "4025940",   # ✓ Protection of civilians (Oct 2023)
    "A/RES/ES-10/22": "4031196",   # ~ Immediate humanitarian ceasefire (Dec 2023)
    "A/RES/ES-10/23": "4048289",   # ✓ UNRWA mandate (May 2024)
    # ES-10/24 UNDL ID not yet found — country votes will be skipped
    "S/RES/2712":     "4027705",   # ✓ Humanitarian pauses in Gaza (Nov 2023)
    "S/RES/2720":     "4031189",   # ✓ Humanitarian coordinator for Gaza (Dec 2023)
    "S/RES/2728":     "4042188",   # ✓ Ceasefire during Ramadan (Mar 2024)
}

# ── Verified resolution data ──────────────────────────────────────────────────
# Format: (un_symbol, title, vote_date, body, votes_yes, votes_no, votes_abstain,
#           short_description)

_UKRAINE_RESOLUTIONS = [
    (
        "A/RES/ES-11/1",
        "Aggression against Ukraine",
        date(2022, 3, 2),
        "UNGA",
        141, 5, 35,
        "Demands Russia immediately cease military operations in Ukraine and "
        "withdraw all forces, deploring its aggression against Ukraine.",
    ),
    (
        "A/RES/ES-11/2",
        "Humanitarian consequences of the aggression against Ukraine",
        date(2022, 3, 24),
        "UNGA",
        140, 5, 38,
        "Deplores the dire humanitarian consequences of Russia's aggression "
        "against Ukraine and calls for urgent humanitarian access and corridors.",
    ),
    (
        "A/RES/ES-11/3",
        "Territorial integrity of Ukraine: defending the principles of the "
        "Charter of the United Nations",
        date(2022, 10, 12),
        "UNGA",
        143, 5, 35,
        "Declares null and void Russia's attempted annexation of Donetsk, "
        "Kherson, Luhansk and Zaporizhzhia oblasts of Ukraine.",
    ),
    (
        "A/RES/ES-11/4",
        "Furtherance of remedy and reparation for aggression against Ukraine",
        date(2022, 11, 14),
        "UNGA",
        94, 14, 73,
        "Calls for the establishment of an international mechanism for "
        "reparations for damage caused by Russia's aggression against Ukraine.",
    ),
    (
        "A/RES/ES-11/5",
        "Principles of the Charter of the United Nations underlying a "
        "comprehensive, just and lasting peace in Ukraine",
        date(2023, 2, 23),
        "UNGA",
        141, 7, 32,
        "Stresses the importance of a comprehensive, just and lasting peace "
        "in Ukraine consistent with the UN Charter on the first anniversary of "
        "Russia's full-scale invasion.",
    ),
    (
        "A/RES/ES-11/6",
        "Furtherance of remedy and reparation for aggression against Ukraine: "
        "establishment of an international register of damage caused by the "
        "aggression of the Russian Federation against Ukraine",
        date(2023, 4, 26),
        "UNGA",
        99, 10, 73,
        "Establishes an international register to document evidence and claims "
        "for damage, loss or injury caused by Russia's aggression against Ukraine.",
    ),
    (
        "A/RES/ES-11/7",
        "Peaceful settlement of the question of Ukraine",
        date(2024, 2, 23),
        "UNGA",
        93, 18, 65,
        "Calls for a comprehensive, just and lasting peace in Ukraine on the "
        "second anniversary of Russia's full-scale invasion, reaffirming UN "
        "Charter principles.",
    ),
]

_PALESTINE_RESOLUTIONS = [
    (
        "A/RES/ES-10/21",
        "Protection of civilians and upholding legal and humanitarian "
        "obligations",
        date(2023, 10, 27),
        "UNGA",
        121, 14, 44,
        "Calls for an immediate, durable and sustained humanitarian truce "
        "in Gaza, demands unimpeded access for humanitarian aid, and condemns "
        "all violence against civilians following the October 7 Hamas attack "
        "and Israel's military campaign in Gaza.",
    ),
    (
        "A/RES/ES-10/22",
        "Immediate humanitarian ceasefire",
        date(2023, 12, 12),
        "UNGA",
        153, 10, 23,
        "Demands an immediate humanitarian ceasefire in Gaza, the immediate "
        "and unconditional release of all hostages, and unimpeded humanitarian "
        "access throughout the Gaza Strip.",
    ),
    (
        "A/RES/ES-10/23",
        "United Nations Relief and Works Agency for Palestine Refugees in "
        "the Near East",
        date(2024, 5, 10),
        "UNGA",
        159, 9, 11,
        "Reaffirms the mandate of UNRWA and calls on all states to maintain "
        "and increase their contributions to UNRWA following funding suspension "
        "by several donors over allegations regarding staff conduct.",
    ),
    (
        "A/RES/ES-10/24",
        "Access to and activities of the United Nations Relief and Works "
        "Agency for Palestine Refugees in the Near East",
        date(2024, 9, 18),
        "UNGA",
        124, 14, 43,
        "Demands that Israel allow unimpeded access for UNRWA to fulfil its "
        "mandate in Gaza and the occupied Palestinian territory, following "
        "Israeli legislation restricting UNRWA operations.",
    ),
]

# Also seed a sample of important SC resolutions (these were adopted — not vetoed)
# SC resolutions on Ukraine are mostly vetoed by Russia; SC resolutions on Gaza
# by the US.  Include the few that actually passed.
_SC_RESOLUTIONS = [
    (
        "S/RES/2712",
        "Security Council resolution 2712 (2023) [on humanitarian pauses in "
        "Gaza]",
        date(2023, 11, 15),
        "UNSC",
        12, 0, 3,
        "Calls for extended humanitarian pauses in the Gaza conflict to allow "
        "the safe and unimpeded delivery of humanitarian assistance and the "
        "release of hostages.",
    ),
    (
        "S/RES/2720",
        "Security Council resolution 2720 (2023) [on humanitarian access to "
        "Gaza]",
        date(2023, 12, 22),
        "UNSC",
        13, 0, 2,
        "Establishes a UN Senior Humanitarian and Reconstruction Coordinator "
        "for Gaza and calls for scaling up of humanitarian aid delivery.",
    ),
    (
        "S/RES/2728",
        "Security Council resolution 2728 (2024) [on immediate ceasefire in "
        "Gaza during Ramadan]",
        date(2024, 3, 25),
        "UNSC",
        14, 0, 1,
        "Demands an immediate ceasefire in Gaza for the month of Ramadan, "
        "the release of all hostages, and the lifting of barriers to "
        "humanitarian aid delivery.",
    ),
]

ALL_RESOLUTIONS = _UKRAINE_RESOLUTIONS + _PALESTINE_RESOLUTIONS + _SC_RESOLUTIONS


class Command(BaseCommand):
    help = (
        "Seed the database with key 2022-2024 Ukraine and Palestine/Gaza UN "
        "resolutions that are not accessible via UNDL OAI-PMH date-range queries."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Show what would be created without writing to the database.",
        )
        parser.add_argument(
            "--inline-enrich", action="store_true", default=False,
            help=(
                "After seeding, run AI enrichment + event classification inline "
                "on every newly created resolution."
            ),
        )
        parser.add_argument(
            "--ukraine-only", action="store_true", default=False,
            help="Only seed the Ukraine ES-11 resolutions.",
        )
        parser.add_argument(
            "--palestine-only", action="store_true", default=False,
            help="Only seed the Palestine ES-10 resolutions.",
        )
        parser.add_argument(
            "--backfill-votes", action="store_true", default=False,
            help=(
                "Scrape country-level vote data from UNDL for all seeded "
                "resolutions that have a known UNDL ID.  Can be run on its "
                "own after the initial seed to add vote breakdowns."
            ),
        )

    def handle(self, *args, **options):
        dry_run        = options["dry_run"]
        inline_enrich  = options["inline_enrich"]
        ukraine_only   = options["ukraine_only"]
        palestine_only = options["palestine_only"]
        backfill_votes = options["backfill_votes"]

        if ukraine_only and palestine_only:
            self.stderr.write(self.style.ERROR(
                "--ukraine-only and --palestine-only are mutually exclusive."
            ))
            return

        if ukraine_only:
            batch = _UKRAINE_RESOLUTIONS
        elif palestine_only:
            batch = _PALESTINE_RESOLUTIONS
        else:
            batch = ALL_RESOLUTIONS

        if dry_run:
            self.stdout.write(self.style.WARNING("[DRY RUN] No data will be written."))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== Seed Key Resolutions ===\n"
            f"  Total to process : {len(batch)}\n"
            f"  Dry run          : {dry_run}"
        ))

        created = updated = skipped = 0
        created_pks: list[int] = []

        for (symbol, title, vote_date, body,
             _votes_yes, _votes_no, _votes_abstain, short_description) in batch:

            symbol_tags = tags_from_symbol(symbol)

            defaults = dict(
                title=title,
                vote_date=vote_date,
                body=body,
                short_description=short_description,
                resolution_text="",   # AI enrichment will populate this
                topic_tags=[],
                symbol_tags=symbol_tags,
                undl_id=_UNDL_IDS.get(symbol, ""),
                session=None,
            )
            # Note: vote tallies (yes/no/abstain) are stored on UNVote rows,
            # not on UNResolution.  Use --backfill-votes to scrape per-country
            # votes from UNDL for all resolutions that have a known UNDL ID.

            if dry_run:
                exists = UNResolution.objects.filter(un_symbol=symbol).exists()
                status = "update" if exists else "create"
                self.stdout.write(
                    f"  [{status}] {symbol:35s} {str(vote_date):12s} {title[:50]}"
                )
                if not exists:
                    created += 1
                else:
                    updated += 1
                continue

            try:
                with transaction.atomic():
                    obj, was_created = UNResolution.objects.update_or_create(
                        un_symbol=symbol, defaults=defaults
                    )
                if was_created:
                    created += 1
                    created_pks.append(obj.pk)
                    self.stdout.write(self.style.SUCCESS(
                        f"  [created] pk={obj.pk:5d} {symbol:35s} {str(vote_date):12s}"
                    ))
                else:
                    updated += 1
                    self.stdout.write(
                        f"  [updated] pk={obj.pk:5d} {symbol:35s} {str(vote_date):12s}"
                    )
            except Exception as exc:
                skipped += 1
                self.stdout.write(self.style.ERROR(
                    f"  [error]  {symbol}: {exc}"
                ))

        style = self.style.SUCCESS if skipped == 0 else self.style.WARNING
        self.stdout.write(style(
            f"\n=== Done ==="
            f"\n  Created  : {created}"
            f"\n  Updated  : {updated}"
            f"\n  Skipped  : {skipped}"
        ))

        # Patch UNDL IDs on existing records that were seeded before IDs were known
        if not dry_run:
            self._patch_undl_ids(batch)

        if not dry_run and inline_enrich and created_pks:
            self._run_inline_enrich(created_pks)

        if not dry_run and backfill_votes:
            self._backfill_votes(batch)

    def _patch_undl_ids(self, batch) -> None:
        """
        Update undl_id on any existing DB record that still has undl_id=""
        but now has a known ID in _UNDL_IDS.  Safe to call multiple times.
        """
        patched = 0
        for (symbol, *_) in batch:
            undl_id = _UNDL_IDS.get(symbol, "")
            if not undl_id:
                continue
            updated = UNResolution.objects.filter(
                un_symbol=symbol, undl_id=""
            ).update(undl_id=undl_id)
            if updated:
                patched += updated
        if patched:
            self.stdout.write(f"  Patched UNDL IDs on {patched} existing record(s).")

    def _backfill_votes(self, batch) -> None:
        """
        For every resolution in batch that has a UNDL ID, scrape the UNDL
        record page for country-level vote data and save UNVote rows.
        """
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Backfilling country votes from UNDL ==="
        ))
        country_lookup: dict[str, Country] = {
            c.isoa3_code.upper(): c for c in Country.objects.all()
        }

        total_votes = 0
        for (symbol, *_) in batch:
            undl_id = _UNDL_IDS.get(symbol, "")
            if not undl_id:
                self.stdout.write(f"  {symbol:35s} — no UNDL ID, skipping")
                continue

            try:
                obj = UNResolution.objects.get(un_symbol=symbol)
            except UNResolution.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  {symbol} — not in DB, run seed first"
                ))
                continue

            existing_count = UNVote.objects.filter(resolution=obj).count()
            if existing_count > 0:
                self.stdout.write(
                    f"  {symbol:35s} — {existing_count} votes already exist, skipping"
                )
                continue

            self.stdout.write(f"  {symbol:35s} — scraping UNDL record {undl_id} ...")
            votes = fetch_country_votes(undl_id)

            if not votes:
                self.stdout.write(self.style.WARNING(
                    f"    No vote data returned (page may be JS-rendered or ID wrong)"
                ))
                continue

            new_rows = []
            for iso3, vote_str in votes.items():
                country = country_lookup.get(iso3.upper())
                if country:
                    new_rows.append(UNVote(
                        resolution=obj, country=country, vote=vote_str
                    ))

            # Sanity check: UNGA/UNSC resolutions should have many voters.
            # Fewer than 10 country votes almost certainly means the scraper
            # found country names mentioned in the text (not in the vote table)
            # and produced garbage data.  Skip rather than save bad rows.
            MIN_EXPECTED_VOTES = 10
            if len(new_rows) < MIN_EXPECTED_VOTES:
                self.stdout.write(self.style.WARNING(
                    f"    Only {len(new_rows)} valid country votes found "
                    f"(threshold={MIN_EXPECTED_VOTES}) — skipping to avoid bad data.\n"
                    f"    The UNDL record {undl_id} is likely the PDF/text record,\n"
                    f"    not the Voting Data record.  Country votes unavailable."
                ))
                continue

            with transaction.atomic():
                UNVote.objects.bulk_create(new_rows, ignore_conflicts=True)
            total_votes += len(new_rows)
            self.stdout.write(self.style.SUCCESS(
                f"    Saved {len(new_rows)} country votes"
            ))

            time.sleep(1.5)  # polite delay between UNDL scrape requests

        self.stdout.write(self.style.SUCCESS(
            f"\n  Total country votes saved: {total_votes}"
        ))

    def _run_inline_enrich(self, pks: list[int]) -> None:
        """Run AI enrichment then event classification on the given PKs."""
        from core.tasks import classify_resolution_to_event, enrich_resolution_with_ai

        self.stdout.write(
            f"\n  Running inline enrich -> classify on {len(pks)} resolution(s) ..."
        )
        enriched = linked = failed = 0

        for pk in pks:
            try:
                enrich_resolution_with_ai(pk)
                enriched += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"    enrich pk={pk}: {exc}"))
                failed += 1
                continue

            try:
                result = classify_resolution_to_event(pk)
                if result.get("linked"):
                    linked += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"    pk={pk} -> event_id={result['event_id']} "
                        f"(conf={result['confidence']:.3f})"
                    ))
                else:
                    reason = result.get("reason", "")
                    self.stdout.write(
                        f"    pk={pk} -> no match ({reason[:80]})"
                    )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"    classify pk={pk}: {exc}"))
                failed += 1

        self.stdout.write(self.style.SUCCESS(
            f"  Inline: enriched={enriched} linked={linked} failed={failed}"
        ))
