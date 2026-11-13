from django.core.management.base import BaseCommand, CommandError

from cedars.services.specifications import service


class Command(BaseCommand):
    help = (
        "Compare each specification's stored starting document (cedars/services/specifications/starting/<code>.json) with the one the "
        "filing modules' code makes today, or write it again (--write). Until filings read their version, a module's layout and its "
        "specification must agree; a change is published as a new version."
    )

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true", help="Write the documents the code makes today")
        parser.add_argument("codes", nargs="*", help="Specifications to compare or write (default: all)")

    def handle(self, *args, **options):
        codes = options["codes"] or [layout.code for layout in service.layouts()]
        failed = []
        for code in codes:
            if options["write"]:
                service.starting_path(code).write_text(service.dumps(service.starting_document(code)), encoding="utf-8")
                self.stdout.write(f"{code}: written")
                continue
            if not service.starting_path(code).exists():
                failed.append(code)
                self.stdout.write(f"{code}: no stored document")
                continue
            diffs = service.starting_differences(code)
            if diffs:
                failed.append(code)
                self.stdout.write(f"{code}: {len(diffs)} differences")
                for d in diffs[:50]:
                    self.stdout.write(f"  {d}")
            else:
                self.stdout.write(f"{code}: the code and the stored document agree")
        if failed:
            raise CommandError(f"Not in step: {', '.join(failed)}")
