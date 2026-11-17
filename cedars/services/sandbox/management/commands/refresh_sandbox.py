from django.core.management.base import BaseCommand, CommandError

from cedars.services.sandbox import bundle


class Command(BaseCommand):
    help = (
        "Add production's published, non-PII data from a bundle (export_sandbox_bundle) to the sandbox: what it lacks is added, what "
        "it has is kept, clashes with its own test records are listed. Runs only in the sandbox (or a developer database)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="source", required=True, help="The bundle: a folder, file:// or s3://bucket/prefix")

    def handle(self, *args, source, **opts):
        try:
            refresh = bundle.load(source)
        except bundle.BundleError as e:
            raise CommandError(str(e)) from e
        added = sum(c["added"] for c in refresh.counts.values())
        skipped = sum(c["skipped"] for c in refresh.counts.values())
        made = f"{refresh.bundle_made_at:%Y-%m-%d %H:%M}" if refresh.bundle_made_at else "an unknown date"
        self.stdout.write(f"Sandbox refreshed from the bundle of {made}: {added} rows added, {skipped} left out.")
        for s in refresh.skipped[:20]:
            self.stdout.write(f"  left out {s['table']} {s['key']}: {s['why']}")
