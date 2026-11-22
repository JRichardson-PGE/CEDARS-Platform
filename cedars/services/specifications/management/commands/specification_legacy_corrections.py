"""Write (or check) the stored documents of the legacy correction versions, legacy_corrections/*.json
(specifications.legacy_corrections)."""

from django.core.management.base import BaseCommand, CommandError

from cedars.services.specifications import legacy_corrections


class Command(BaseCommand):
    help = "Write the legacy correction versions' documents (legacy_corrections/*.json), or check they are current."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true", help="Write the documents (default: only check them)")

    def handle(self, *args, write=False, **options):
        if write:
            for path in legacy_corrections.write():
                self.stdout.write(f"wrote {path.name}")
            return
        if legacy_corrections.stored() != legacy_corrections.documents():
            raise CommandError(
                "The stored legacy correction versions differ from the generator: run manage.py specification_legacy_corrections --write."
            )
        self.stdout.write("The stored legacy correction versions are current.")
