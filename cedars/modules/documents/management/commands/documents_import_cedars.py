from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.modules.documents import cedars_import
from cedars.modules.documents.service import DocumentError


class Command(BaseCommand):
    help = (
        "Migrate CEDARS' Reports & Documents, DEER Resources and program documents (BR-15.10) from a manifest (one row per file of a "
        "version; see documents/cedars_import.py) and a folder of the files, with their versions, dates and old addresses. Already "
        "imported records are skipped. Try --dry-run first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--manifest", type=Path, required=True, help="the manifest CSV")
        parser.add_argument("--files", type=Path, required=True, help="the folder the manifest's file paths are under")
        parser.add_argument("--dry-run", action="store_true", help="read and check everything, store nothing, and report")
        parser.add_argument("--limit", type=int, help="import only the first N records")

    def handle(self, *args, manifest, files, dry_run=False, limit=None, **o):
        try:
            report = cedars_import.run(cedars_import.read_manifest(manifest), files, dry_run=dry_run, limit=limit)
        except DocumentError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(("Dry run: nothing stored.\n" if dry_run else "") + "\n".join(report.lines()))
