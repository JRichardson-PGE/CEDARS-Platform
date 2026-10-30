from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.rules import service


class Command(BaseCommand):
    help = "Run the rules in force on a Measure or ProgramCost file and write the findings as CSV (the pre-upload checker from a shell)."

    def add_arguments(self, parser):
        parser.add_argument("file", type=Path)
        parser.add_argument("--rules", default="claims", help="rule set code: claims or budget_filing")
        parser.add_argument("--table", default="Measure", choices=["Measure", "ProgramCost"])
        parser.add_argument("--filing-year", type=int)
        parser.add_argument("--out", type=Path, help="findings CSV (default: print a summary only)")

    def handle(self, file, rules, table, filing_year, out, **opts):
        rs = service.in_force(rules)
        if rs is None:
            raise CommandError(f"no published {rules} rule set is in force")
        findings, notes = service.check(rs, table, service.read_table(file.read_bytes()), filing_year=filing_year)
        rejects = len({f.record_id or f.row for f in findings if f.severity == "reject"})
        self.stdout.write(f"{rs}: {len(findings)} findings, {rejects} records would be rejected, {len(notes)} rules not checked")
        if out:
            out.write_text(service.findings_csv(findings), encoding="utf-8")
