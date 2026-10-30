from django.core.management.base import BaseCommand, CommandError

from cedars.modules.impact_profiles import etrm


class Command(BaseCommand):
    help = (
        "Pull draft measure packages and permutations from eTRM (run nightly). "
        "Uses ETRM_DATABASE_URL, --export, or --fixture / ETRM_FIXTURE."
    )

    def add_arguments(self, parser):
        parser.add_argument("--fixture", help="a JSON file in the fixture layout instead of the eTRM database")
        parser.add_argument("--export", nargs="+", metavar="CSV", help="eTRM permutations downloads instead of the eTRM database")

    def handle(self, *args, fixture=None, export=None, **options):
        source = etrm.ExportSource(export) if export else etrm.FileSource(fixture) if fixture else None
        run = etrm.sync(None, source)
        msg = (
            f"{run.get_status_display()}: {run.packages_seen} packages, {run.imported} imported, "
            f"{run.unchanged} unchanged, {run.excluded} excluded"
        )
        if run.status == run.Status.FAILED:
            raise CommandError(f"{msg}. {run.error}")
        self.stdout.write(msg)
