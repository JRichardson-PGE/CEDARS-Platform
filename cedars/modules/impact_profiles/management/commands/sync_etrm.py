from django.core.management.base import BaseCommand, CommandError

from cedars.modules.impact_profiles import etrm


class Command(BaseCommand):
    help = (
        "Pull draft measure packages and permutations from eTRM (run nightly). "
        "Uses ETRM_DATABASE_URL, --export, or --fixture / ETRM_FIXTURE. "
        "A failed sync is tried again after each wait in ETRM_SYNC_RETRY_MINUTES; if the last attempt fails, administrators are alerted."
    )

    def add_arguments(self, parser):
        parser.add_argument("--fixture", help="a JSON file in the fixture layout instead of the eTRM database")
        parser.add_argument("--export", nargs="+", metavar="CSV", help="eTRM permutations downloads instead of the eTRM database")
        parser.add_argument("--no-retry", action="store_true", help="one attempt only (administrators are still alerted if it fails)")

    def handle(self, *args, fixture=None, export=None, no_retry=False, **options):
        source = etrm.ExportSource(export) if export else etrm.FileSource(fixture) if fixture else None
        run = etrm.nightly(source, waits=[] if no_retry else None)
        msg = (
            f"{run.get_status_display()}: {run.packages_seen} packages, {run.imported} imported, "
            f"{run.unchanged} unchanged, {run.excluded} excluded"
        )
        if run.status == run.Status.FAILED:
            raise CommandError(f"{msg} (attempt {run.attempt}). {run.error}")
        self.stdout.write(msg + (f" (attempt {run.attempt})" if run.attempt > 1 else ""))
