from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.legacy import service


class Command(BaseCommand):
    help = (
        "Check legacy CEDARS' record-level downloads against their manifest (size and SHA-256) and the published layouts, and copy "
        "them to storage for the legacy-year loads. Re-running skips files already staged."
    )

    def add_arguments(self, parser):
        parser.add_argument("manifest", help="manifest.csv: dataset, year, pa, filename, url, bytes, sha256")
        parser.add_argument("folder", help="the folder holding the downloaded zips (searched by file name)")
        parser.add_argument("--dataset", action="append", choices=service.DATASETS, help="stage only this dataset (repeatable)")
        parser.add_argument("--dry-run", action="store_true", help="check every file but copy nothing")

    def handle(self, *args, manifest, folder, dataset=None, dry_run=False, **options):
        report = service.stage(
            None, Path(manifest).read_text(encoding="utf-8"), Path(folder), datasets=tuple(dataset or service.DATASETS), dry_run=dry_run
        )
        for line in report.lines():
            self.stdout.write(line)
        if not report.ok:
            raise CommandError("Some files were missing or refused; nothing else was changed for them.")
