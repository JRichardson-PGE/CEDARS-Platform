"""Write (or check) the stored documents of legacy CEDARS' layouts, historic/*.json, from the crosswalk (specifications.historic)."""

from django.core.management.base import BaseCommand, CommandError

from cedars.services.specifications import historic


class Command(BaseCommand):
    help = "Write the legacy CEDARS layouts' version documents (historic/*.json) from the crosswalk, or check they are current."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true", help="Write the documents (default: only check them)")

    def handle(self, *args, write=False, **options):
        if write:
            for path in historic.write():
                self.stdout.write(f"wrote {path.name}")
            return
        if historic.stored() != historic.documents():
            raise CommandError("The stored legacy layouts differ from the crosswalk: run manage.py specification_historic --write.")
        self.stdout.write("The stored legacy layouts match the crosswalk.")
