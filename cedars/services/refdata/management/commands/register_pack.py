from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand

from cedars.services.refdata import service
from cedars.services.refdata.models import PackVersion


class Command(BaseCommand):
    help = "Register a folder (or one file) as a draft data pack: every file's SHA-256 goes into its manifest."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--kind", required=True, choices=PackVersion.Kind.values)
        parser.add_argument("--label", required=True, help='e.g. "2026 ACC"')
        parser.add_argument("--pack-version", dest="pack_version", default="")
        parser.add_argument("--acc-year", type=int)
        parser.add_argument("--valid-from", type=date.fromisoformat)
        parser.add_argument("--storage-uri", help="where the files are stored (default: the path given)")
        parser.add_argument("--source", default="")

    def handle(self, path, kind, label, pack_version, acc_year, valid_from, storage_uri, source, **opts):
        pack = service.register_pack(
            None,
            kind=kind,
            label=label,
            version=pack_version,
            acc_year=acc_year,
            valid_from=valid_from,
            manifest=service.manifest_for(path),
            storage_uri=storage_uri or path.resolve().as_uri(),
            source=source,
        )
        self.stdout.write(f"{pack}: {pack.files} files, fingerprint {pack.fingerprint} (pack {pack.pk}, draft)")
