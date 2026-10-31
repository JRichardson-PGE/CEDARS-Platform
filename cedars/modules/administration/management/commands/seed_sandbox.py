import json
from datetime import datetime, time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.dictionary import service as dictionary
from cedars.services.registry import service as registry
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline

# Program administrators for test filings (names only; their administrators are granted in the platform, D6)
ORGANISATIONS = [
    ("PGE", "Pacific Gas and Electric Company", "IOU"),
    ("SCE", "Southern California Edison", "IOU"),
    ("SDGE", "San Diego Gas & Electric", "IOU"),
    ("SCG", "Southern California Gas Company", "IOU"),
    ("BAY", "Bay Area Regional Energy Network (BayREN)", "REN"),  # CEDARS's code for BayREN
    ("MCE", "MCE", "CCA"),
]
DICTIONARY = Path(settings.BASE_DIR) / "data" / "dictionary"


class Command(BaseCommand):
    help = "Fill a sandbox (or a developer database) with organizations, a cycle, periods, test deadlines and the dictionary. Re-runnable."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=timezone.localdate().year + 1, help="program year for the test deadlines")

    def handle(self, year, **opts):
        if settings.ENVIRONMENT not in ("sandbox", "dev", "test"):
            raise CommandError(f"seed_sandbox runs only in the sandbox or a developer database, not in {settings.ENVIRONMENT}.")
        if access.cpuc() is None:
            access.create_organisation(None, code="CPUC", name="California Public Utilities Commission", kind="cpuc")
        for code, name, pa_type in ORGANISATIONS:
            if not Organisation.objects.filter(code=code).exists():
                access.create_organisation(None, code=code, name=name, kind="pa", pa_type=pa_type)
        registry.ensure_cycle(year - (year - 2024) % 4)  # cycles start 2024, 2028, ...
        periods = {(p.kind, p.number): p for p in registry.ensure_periods(year)}
        due = [
            ("claims", "quarterly claim", periods[("quarter", q)], (year if q < 4 else year + 1, (q * 3 + 2) % 12 or 12, 1))
            for q in range(1, 5)
        ]
        due += [("monthly", "monthly report", periods[("month", m)], (year if m < 12 else year + 1, m % 12 + 1, 15)) for m in range(1, 13)]
        due.append(("budgets", "TUAL", periods[("year", 0)], (year - 1, 9, 1)))
        added = 0
        for module, kind, period, (y, m, d) in due:
            if not Deadline.objects.filter(module=module, kind=kind, period=period).exists():
                submissions.add_deadline(
                    None,
                    module=module,
                    kind=kind,
                    period=period,
                    due_at=timezone.make_aware(datetime.combine(datetime(y, m, d), time(23, 59, 59))),
                )
                added += 1
        loaded = ""
        if dictionary.current() is None and DICTIONARY.exists():
            latest = sorted(DICTIONARY.glob("*.json"))[-1]
            dictionary.load_catalog(None, json.loads(latest.read_text()))
            loaded = f", dictionary {latest.name}"
        self.stdout.write(f"Sandbox ready: {Organisation.objects.count()} organizations, {added} deadlines added for {year}{loaded}.")
