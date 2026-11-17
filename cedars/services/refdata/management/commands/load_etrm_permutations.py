from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.refdata import etrm


class Command(BaseCommand):
    help = (
        "Load eTRM permutations downloads (CSV) as a draft pack of published permutations, indexed by Measure Detail ID, for the deemed "
        "checks on claims. Publish the pack on its reference-data page before filings use it."
    )

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", type=Path)
        parser.add_argument("--label", default="eTRM published permutations")
        parser.add_argument("--pack-version", dest="pack_version", required=True, help="e.g. the download date, 2026-10-30")
        parser.add_argument("--valid-from", type=date.fromisoformat)
        parser.add_argument("--source", default="")

    def handle(self, files, label, pack_version, valid_from, source, **opts):
        try:
            pack = etrm.load(None, files, label=label, version=pack_version, valid_from=valid_from, source=source)
        except etrm.EtrmError as e:
            raise CommandError(str(e)) from None
        self.stdout.write(f"{pack}: {pack.notes} (pack {pack.pk}, draft)")
