import json
from pathlib import Path

from django.core.management.base import BaseCommand

from cedars.services.refdata import service
from cedars.services.refdata.models import PackVersion


class Command(BaseCommand):
    help = "Record an acceptance check on a data pack, from a reconciliation script's or the reference calculation's JSON report."

    def add_arguments(self, parser):
        parser.add_argument("pack", type=int)
        parser.add_argument("--check", required=True)
        parser.add_argument("--passed", action="store_true", help="the check passed (otherwise it is recorded as failed)")
        parser.add_argument("--precision", type=float)
        parser.add_argument("--largest-difference", type=float)
        parser.add_argument("--engine-release", default="")
        parser.add_argument("--summary", default="")
        parser.add_argument("--report", type=Path, help="JSON report kept with the result")
        parser.add_argument("--report-uri", default="")

    def handle(self, pack, check, passed, precision, largest_difference, engine_release, summary, report, report_uri, **opts):
        t = service.record_tie_out(
            None,
            PackVersion.objects.get(pk=pack),
            check=check,
            passed=passed,
            precision=precision,
            largest_difference=largest_difference,
            engine_release=engine_release,
            summary=summary,
            report=json.loads(report.read_text()) if report else None,
            report_uri=report_uri,
        )
        self.stdout.write(str(t))
