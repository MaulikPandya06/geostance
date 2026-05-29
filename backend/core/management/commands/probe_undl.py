"""
Management command: probe_undl
==============================
Directly probe the UNDL OAI-PMH endpoint and show exactly what it returns
for a date range — BEFORE any B-code, symbol, or voting-record filters are
applied.  Use this to diagnose missing resolutions.

Outputs per monthly window:
  • total records returned by UNDL
  • how many pass / fail each filter stage
  • the symbol, b-codes, and vote_date of every record found (with --verbose)

Usage
-----
  # Show raw UNDL counts for 2026 (Voting Data set, default)
  python manage.py probe_undl --from 2026-01-01 --until 2026-05-31

  # Same but without the Voting Data set restriction
  python manage.py probe_undl --from 2026-01-01 --until 2026-05-31 --all-records

  # Show every record found (verbose)
  python manage.py probe_undl --from 2026-01-01 --until 2026-05-31 --verbose --all-records
"""

import time
from datetime import date
from xml.etree import ElementTree as ET

import requests
from django.core.management.base import BaseCommand

from core.ingestion.un_library import (
    MARC21_NS,
    OAI_NS,
    OAI_BASE,
    OAI_MAX_RETRIES,
    OAI_RETRY_BASE,
    REQUEST_DELAY,
    HEADERS,
    _discover_voting_set,
    _get_session,
    _monthly_windows,
    _oai_get_with_retry,
    _get_all_subfields,
    _get_subfield,
    _inject_ns,
    _parse_undl_date,
    normalize_un_symbol,
)

_RESOLUTION_CODES = {"B01", "B04", "B06", "B08"}

# NOTE: verified against the live 2026 OAI feed — B01 is used for ADOPTED
# RESOLUTIONS of every body (GA, SC, HRC alike).  The body is read from the
# 191$a symbol prefix, not the b-code.  B04/B06/B08 show up on report/letter
# documents, not resolutions, so they are NOT reliable "resolution" markers.
_BODY_LABELS = {
    "B01": "RESOLUTION",   # adopted resolution (any body)
    "B02": "draft",
    "B03": "decision/verbatim",
    "B04": "report/letter",
    "B06": "report/letter",
    "B08": "draft/working",
    "B15": "letter",
    "B16": "report",
    "B18": "letter/note",
    "B22": "vote-record",  # untyped, no symbol — per-vote metadata stubs
}


class Command(BaseCommand):
    help = (
        "Probe the UNDL OAI-PMH endpoint and show raw record counts for a date "
        "range, before any filtering.  Diagnoses missing resolution imports."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--from", dest="from_date", required=True,
            help="OAI harvest start date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--until", dest="until_date", default=None,
            help="OAI harvest end date (YYYY-MM-DD). Default: today.",
        )
        parser.add_argument(
            "--all-records", action="store_true", default=False,
            help="Bypass the Voting Data set restriction (query full UNDL catalog).",
        )
        parser.add_argument(
            "--verbose", action="store_true", default=False,
            help="Print symbol, b-codes, and vote_date for every record found.",
        )

    def handle(self, *args, **options):
        from_date  = options["from_date"]
        until_date = options["until_date"] or date.today().isoformat()
        all_records = options["all_records"]
        verbose     = options["verbose"]

        set_name = None if all_records else _discover_voting_set()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== UNDL OAI-PMH Probe ==="
            f"\n  Range      : {from_date} → {until_date}"
            f"\n  OAI set    : {set_name!r} ({'no filter' if set_name is None else 'Voting Data'})"
            f"\n  all-records: {all_records}"
        ))

        session = _get_session()
        windows = _monthly_windows(from_date, until_date)
        today   = date.today().isoformat()

        grand_total = grand_res = grand_non_res = grand_no_sym = 0

        for w_from, w_until in windows:
            if w_from > today:
                break

            params: dict = {
                "verb":           "ListRecords",
                "metadataPrefix": "marcxml",
                "from":           w_from,
                "until":          w_until,
            }
            if set_name:
                params["set"] = set_name

            page = 0
            win_total = win_res = win_non_res = 0
            self.stdout.write(f"\n── {w_from} → {w_until} ──")

            while True:
                resp = _oai_get_with_retry(session, params)
                if resp is None:
                    self.stdout.write(self.style.ERROR(
                        f"  [FAILED] gave up after {OAI_MAX_RETRIES} retries"
                    ))
                    break

                raw_xml = resp.text
                ns_oai  = {"oai": OAI_NS}
                ns_marc = {"m": MARC21_NS}

                try:
                    root = ET.fromstring(raw_xml)
                except ET.ParseError as exc:
                    self.stdout.write(self.style.ERROR(f"  XML parse error: {exc}"))
                    break

                error_el = root.find("oai:error", ns_oai)
                if error_el is not None:
                    code = error_el.get("code", "unknown")
                    if code == "noRecordsMatch":
                        self.stdout.write("  → noRecordsMatch (0 records in UNDL for this window)")
                    else:
                        self.stdout.write(self.style.ERROR(f"  OAI error: {code}"))
                    break

                records_in_page = 0
                for record_el in root.findall(".//oai:record", ns_oai):
                    header = record_el.find("oai:header", ns_oai)
                    if header is not None and header.get("status") == "deleted":
                        continue

                    metadata_el = record_el.find("oai:metadata", ns_oai)
                    if metadata_el is None:
                        continue

                    marc_el = (
                        metadata_el.find(f"{{{MARC21_NS}}}record")
                        or metadata_el.find("record")
                    )
                    if marc_el is None:
                        continue

                    # Inject namespace if the inner record omits it
                    if not marc_el.tag.startswith("{"):
                        marc_el = _inject_ns(marc_el)

                    records_in_page += 1
                    win_total       += 1
                    grand_total     += 1

                    # Extract key fields (no filtering — just inspection)
                    cf = marc_el.find("m:controlfield[@tag='001']", ns_marc)
                    undl_id = cf.text.strip() if cf is not None and cf.text else "?"

                    b_codes = [s.upper() for s in _get_all_subfields(marc_el, ns_marc, "089", "b") if s]
                    un_symbol = normalize_un_symbol(_get_subfield(marc_el, ns_marc, "191", "a"))

                    date_raw  = _get_subfield(marc_el, ns_marc, "992", "a")
                    vote_date = _parse_undl_date(date_raw)
                    if not vote_date:
                        date_raw  = _get_subfield(marc_el, ns_marc, "269", "a")
                        vote_date = _parse_undl_date(date_raw)

                    is_resolution = bool(b_codes and (set(b_codes) & _RESOLUTION_CODES))
                    has_res_sym   = "RES/" in un_symbol if un_symbol else False

                    if is_resolution or has_res_sym:
                        win_res     += 1
                        grand_res   += 1
                        tag = "RES"
                    elif not un_symbol:
                        grand_no_sym += 1
                        tag = "NO-SYM"
                    else:
                        win_non_res  += 1
                        grand_non_res += 1
                        tag = "skip"

                    if verbose:
                        bc_str  = ",".join(b_codes) if b_codes else "none"
                        bc_desc = ",".join(_BODY_LABELS.get(c, c) for c in b_codes) if b_codes else "?"
                        self.stdout.write(
                            f"  [{tag:6}] {un_symbol or '(no sym)':35} "
                            f"b={bc_str}({bc_desc}) date={vote_date} undl={undl_id}"
                        )

                token_el = root.find(".//oai:resumptionToken", ns_oai)
                token = None
                if token_el is not None and token_el.text and token_el.text.strip():
                    token = token_el.text.strip()

                self.stdout.write(
                    f"  page {page}: raw={records_in_page}  "
                    f"resolution={win_res}  other={win_non_res}  "
                    f"{'(more pages)' if token else '(done)'}"
                )
                page += 1

                if not token:
                    break

                params = {"verb": "ListRecords", "resumptionToken": token}
                time.sleep(REQUEST_DELAY)

            time.sleep(REQUEST_DELAY)

        # ── Grand summary ─────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            f"\n=== Summary ==="
            f"\n  Total UNDL records in range : {grand_total}"
            f"\n  Resolutions (B01/B04/B06/B08 or RES/ symbol) : {grand_res}"
            f"\n  Other document types (skipped by B-code)     : {grand_non_res}"
        ))
        if grand_total == 0:
            self.stdout.write(self.style.WARNING(
                "\n  ⚠ UNDL returned 0 records for this date range.\n"
                "  Possible causes:\n"
                "    1. Resolutions not indexed yet (UNDL lag is 2–8 weeks after vote)\n"
                "    2. Wrong date range — try --from one year earlier\n"
                "    3. OAI set name mismatch — try --all-records\n"
                "  → Check UNDL directly: https://digitallibrary.un.org/search?cc=Voting+Data&p=year:2026"
            ))
        elif grand_res == 0:
            self.stdout.write(self.style.WARNING(
                f"\n  ⚠ Found {grand_total} UNDL records but 0 resolutions.\n"
                "  The records exist but none have B01/B04/B06/B08 codes or RES/ symbols.\n"
                "  Run with --verbose to inspect individual records."
            ))
