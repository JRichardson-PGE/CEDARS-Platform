from django.core.management.base import BaseCommand

from cedars.modules.impact_profiles import library


class Command(BaseCommand):
    help = (
        "Import legacy CEDARS' impact-profile library (catalog.parquet and hourly.parquet, the engine's layout) "
        "as DEER, TOU and imported profiles."
    )

    def add_arguments(self, parser):
        parser.add_argument("folder", help="the folder holding catalog.parquet and hourly.parquet (the engine's impact_profiles/)")
        parser.add_argument("--limit", type=int, help="import only the first N profiles (a trial run)")

    def handle(self, *args, folder, limit=None, **options):
        s = library.import_library(f"{folder}/catalog.parquet", f"{folder}/hourly.parquet", limit=limit, file_label=folder)
        for k, v in s.items():
            self.stdout.write(f"{k}: {v}")
