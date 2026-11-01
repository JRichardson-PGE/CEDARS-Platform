from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.rules import precheck, service
from cedars.services.rules.models import RuleSet


class Command(BaseCommand):
    help = (
        "Run a module's rule-set step on a Measure or ProgramCost file, as its upload does, and write the findings as CSV (the "
        "pre-upload checker from a shell)."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", type=Path)
        parser.add_argument("--rules", default="claims", help="the module's rules: claims or budget_filing (or the module: budgets)")
        parser.add_argument("--rule-set", type=int, dest="rule_set", help="a published rule set's id (default the version in force)")
        parser.add_argument("--table", default="Measure", choices=list(precheck.TABLES))
        parser.add_argument("--filing-year", type=int)
        parser.add_argument("--out", type=Path, help="findings CSV (default: print a summary only)")

    def handle(self, file, rules, rule_set, table, filing_year, out, **opts):
        rs = None
        if rule_set is not None:
            rs = RuleSet.objects.filter(pk=rule_set, status=RuleSet.Status.PUBLISHED).first()
            if rs is None:
                raise CommandError(f"no published rule set {rule_set}")
        checker = precheck.checker_for(module=rules) or precheck.checker_for(code=rules)
        if checker is None and rs is None:
            raise CommandError(f"no module checks files under {rules!r}")
        try:
            result = precheck.check(None if rs else checker.module, table, file.read_bytes(), rule_set=rs, filing_year=filing_year)
        except precheck.CheckError as e:
            raise CommandError(str(e)) from None
        self.stdout.write(
            f"{result.rule_set}: {len(result.findings)} findings, {result.rejected} records would be rejected, {len(result.notes)} notes"
        )
        for note in result.notes:
            self.stdout.write(f"  {note}")
        if out:
            out.write_text(service.findings_csv(result.findings), encoding="utf-8")
