from django.core.management.base import BaseCommand, CommandError

from cedars.modules.data import parity


class Command(BaseCommand):
    help = (
        "Check the dashboard's numbers against totals CEDARS published (see cedars/modules/data/parity.py for the file's columns). "
        "Fails when a number differs beyond the tolerance or is missing, unless the known-differences file explains it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--expected", required=True, help="CSV of published totals")
        parser.add_argument("--known", help="CSV of known differences (dataset, year, pa, metric, reason)")
        parser.add_argument("--report", help="Write every check to this CSV")
        parser.add_argument("--tolerance", type=float, default=parity.REL, help="Relative tolerance (default 0.005 = 0.5%%)")

    def handle(self, *args, **options):
        try:
            with open(options["expected"], encoding="utf-8-sig", newline="") as f:
                expected = parity.read_expected(f)
            known = []
            if options["known"]:
                with open(options["known"], encoding="utf-8-sig", newline="") as f:
                    known = parity.read_known(f)
        except (OSError, parity.ParityError) as e:
            raise CommandError(str(e)) from e
        report = parity.run(expected, known, rel=options["tolerance"])
        if options["report"]:
            with open(options["report"], "w", encoding="utf-8", newline="") as f:
                report.write_csv(f)
        for line in report.lines():
            self.stdout.write(line)
        if not report.ok:
            raise CommandError(f"{report.count('differs')} differ and {report.count('missing')} are missing")
