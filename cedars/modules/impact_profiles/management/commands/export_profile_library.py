from django.core.management.base import BaseCommand

from cedars.modules.impact_profiles import library


class Command(BaseCommand):
    help = "Write the current profile catalog in the engine's library format as the next numbered export."

    def handle(self, *args, **options):
        e = library.export(None)
        self.stdout.write(f"Export {e.number}: {e.profiles} profiles, {e.catalog_rows} catalog rows, {e.catalog_name}, {e.hourly_name}")
