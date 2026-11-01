from django.core.management.base import BaseCommand, CommandError

from cedars.services.sandbox import bundle


class Command(BaseCommand):
    help = (
        "Write production's published, non-PII data (programs, reference packs, dictionary, rule sets, snapshots in force) to a bundle "
        "for refresh_sandbox: a folder or s3://bucket/prefix. Never people or the PII vault (BR-11.2)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="A folder, file:// or s3://bucket/prefix")

    def handle(self, *args, to, **opts):
        try:
            manifest = bundle.export(to)
        except bundle.BundleError as e:
            raise CommandError(str(e)) from e
        rows = sum(manifest["tables"].values())
        self.stdout.write(f"Bundle written to {to}: {rows} rows in {len(manifest['tables'])} tables, {len(manifest['files'])} files.")
