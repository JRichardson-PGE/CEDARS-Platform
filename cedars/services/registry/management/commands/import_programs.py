from pathlib import Path

from django.core.management.base import BaseCommand

from cedars.services.registry.importers import import_cedars_programs


class Command(BaseCommand):
    help = "Add programs from a CEDARS Programs list export (CSV, e.g. ReCET's data/programs/cedars_programs_20261003.csv)."

    def add_arguments(self, parser):
        parser.add_argument("csv", type=Path)

    def handle(self, csv, **opts):
        counts = import_cedars_programs(csv.read_bytes())
        self.stdout.write(", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in counts.items()))
