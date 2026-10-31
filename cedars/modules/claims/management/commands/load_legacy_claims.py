from django.core.management.base import BaseCommand, CommandError

from cedars.modules.claims import legacy


class Command(BaseCommand):
    help = (
        "Load legacy CEDARS' confirmed claims (2016-2026) from the staged downloads as published, imported versions, one per PA per "
        "claim year (the annual claim), with their claim records, program lines, CEDARS' CET results as filed and the reconciliation; "
        "from 2023 the statewide unallocated claims load as the SW version, outside every total. Re-running skips what is loaded."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", action="append", type=int, choices=list(legacy.YEARS), help="load only this year (repeatable)")
        parser.add_argument("--pa", default="", help="load only this PA's claims (SW: the statewide unallocated claims)")

    def handle(self, *args, year=None, pa="", **options):
        refused = False
        for y in year or list(legacy.YEARS):
            try:
                outcome = legacy.load(y, only=pa.upper())
            except legacy.legacy.LegacyError as e:
                self.stdout.write(f"{y}: not loaded: {e}")
                refused = True
                continue
            for line in outcome.lines():
                self.stdout.write(line)
            refused |= bool(outcome.refused)
        if refused:
            raise CommandError("Some years or PAs were not published; see above.")
