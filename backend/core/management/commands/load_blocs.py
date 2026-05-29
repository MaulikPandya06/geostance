"""
Management command: load_blocs
================================
Loads geopolitical bloc definitions from the bundled JSON fixture and
assigns bloc membership to Country records by ISO-A3 code match.

Usage:
  python manage.py load_blocs
  python manage.py load_blocs --clear    # drops all bloc memberships first

Run this once after running populate_countries.
Re-running is safe (idempotent).
"""

import json
import os

from django.core.management.base import BaseCommand

from core.models import Country, CountryBloc

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "fixtures",
    "country_blocs.json",
)


class Command(BaseCommand):
    help = "Load CountryBloc definitions and assign countries by ISO-A3 code."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            default=False,
            help="Clear all existing bloc memberships before reloading.",
        )

    def handle(self, *args, **options):
        with open(FIXTURE_PATH, encoding="utf-8") as f:
            data = json.load(f)

        blocs_data = data.get("blocs", [])

        if options["clear"]:
            self.stdout.write("  Clearing all bloc memberships …")
            for country in Country.objects.all():
                country.blocs.clear()
            CountryBloc.objects.all().delete()
            self.stdout.write("  Cleared.\n")

        # Pre-load all countries into a lookup by ISO-A3
        country_lookup: dict[str, Country] = {
            c.isoa3_code.upper(): c
            for c in Country.objects.all()
        }

        bloc_created = bloc_updated = 0
        member_added = member_skipped = 0
        not_found: list[str] = []

        for bloc_def in blocs_data:
            name        = bloc_def["name"]
            slug        = bloc_def["slug"]
            description = bloc_def.get("description", "")
            members     = bloc_def.get("members", [])

            # Upsert the bloc itself
            bloc, created = CountryBloc.objects.update_or_create(
                slug=slug,
                defaults={"name": name, "description": description},
            )
            if created:
                bloc_created += 1
                self.stdout.write(f"  + Bloc created: {name}")
            else:
                bloc_updated += 1

            # Wire member countries
            for iso3 in members:
                country = country_lookup.get(iso3.upper())
                if not country:
                    not_found.append(f"{iso3} ({name})")
                    member_skipped += 1
                    continue

                if not bloc.countries.filter(pk=country.pk).exists():
                    bloc.countries.add(country)
                    member_added += 1
                else:
                    member_skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ Blocs done!\n"
                f"  Blocs   → created: {bloc_created} | updated: {bloc_updated}\n"
                f"  Members → added: {member_added} | skipped (existing): {member_skipped}"
            )
        )

        if not_found:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  ⚠ {len(not_found)} ISO-A3 codes not found in DB "
                    f"(run populate_countries first):\n"
                    + "\n".join(f"    {x}" for x in not_found[:20])
                    + ("\n    …" if len(not_found) > 20 else "")
                )
            )
