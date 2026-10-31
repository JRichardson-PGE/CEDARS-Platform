from django.core.management.base import BaseCommand, CommandError

from cedars.modules.budgets import legacy


class Command(BaseCommand):
    help = (
        "Load legacy CEDARS' budget filings (2017-2031) from the staged downloads as published, imported versions, one per PA per "
        "filing, with their budget lines, CEDARS' CET results as filed and the reconciliation. Re-running skips what is loaded."
    )

    def add_arguments(self, parser):
        parser.add_argument("--filing", action="append", choices=list(legacy.BY_KEY), help="load only this filing (repeatable)")
        parser.add_argument("--pa", default="", help="load only this PA's filings")

    def handle(self, *args, filing=None, pa="", **options):
        refused = False
        for f in filing or [x.key for x in legacy.FILINGS]:
            try:
                outcome = legacy.load(legacy.BY_KEY[f], only=pa.upper())
            except legacy.legacy.LegacyError as e:
                self.stdout.write(f"{f}: not loaded: {e}")
                refused = True
                continue
            for line in outcome.lines():
                self.stdout.write(line)
            refused |= bool(outcome.refused)
        if refused:
            raise CommandError("Some filings or PAs were not published; see above.")
