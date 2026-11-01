from django.core.management.base import BaseCommand, CommandError

from cedars.core import revisions


class Command(BaseCommand):
    help = (
        "List the user guides and the FAQ whose text changed since the release they were recorded for (BR-12.5); with --update, record "
        "them as revised for the release being built (cedars.__version__), or for --release."
    )

    def add_arguments(self, parser):
        parser.add_argument("--update", action="store_true", help="record the changed guides in core/help_revisions.json")
        parser.add_argument("--release", default="", help="the release to record them for (default: cedars.__version__)")

    def handle(self, *args, update=False, release="", **options):
        if release and not revisions.version_key(release):
            raise CommandError(f"{release!r} is not a release number like 0.3.0")
        if update:
            changed = revisions.update(release or None)
            self.stdout.write("\n".join(changed) if changed else "Every guide is recorded as it stands.")
            return
        stale = revisions.stale()
        self.stdout.write("\n".join(stale) if stale else "Every guide is recorded as it stands.")
        if stale:
            raise CommandError("Run `manage.py help_revisions --update` to record these guides as revised for this release.")
