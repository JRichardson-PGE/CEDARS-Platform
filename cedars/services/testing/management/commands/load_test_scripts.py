import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.testing import service


class Command(BaseCommand):
    help = (
        "Load the test scripts (the uat.json the script builder writes) as a new version for Testing. The scripts are data on this "
        "site only and are never part of the code. A version is never replaced: load an edited file under a new label."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="The scripts file (JSON)")
        parser.add_argument("--label", required=True, help="A name for this version, e.g. 2026-11-12")

    def handle(self, *args, file, label, **options):
        path = Path(file)
        if not path.is_file():
            raise CommandError(f"{file} is not a file.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise CommandError(f"{file} is not a JSON file: {e}") from e
        try:
            version = service.load_scripts(None, data, label)
        except service.TestingRefused as e:
            raise CommandError(str(e)) from e
        tests = sum(s.tests.count() for s in version.scripts.all())
        self.stdout.write(f"Loaded version {version.label}: {version.scripts.count()} scripts, {tests} tests.")
        if not service.enabled():
            self.stdout.write("Testing is switched off on this site (CEDARS_TESTING): the scripts are kept but no page shows them.")
