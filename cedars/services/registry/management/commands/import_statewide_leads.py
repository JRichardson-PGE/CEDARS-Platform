import csv
import io
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.accounts import service as access
from cedars.services.registry import service
from cedars.services.registry.models import Program

CODE = re.compile(r"\(([A-Za-z0-9_&]+)\)\s*$")


class Command(BaseCommand):
    help = (
        "Record statewide programs' lead PAs from CEDARS' statewide programs download (columns LeadPA, e.g. 'Pacific Gas & Electric "
        "(PGE)', and PrgID), as a system change with a reason. Programs not in the registry, or not statewide, are listed and skipped. "
        "Without --apply it only says what it would change."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path, metavar="csv")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, csv_path, reason, apply, **opts):
        rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"))))
        if not rows or not {"LeadPA", "PrgID"} <= set(rows[0]):
            raise CommandError("The file needs LeadPA and PrgID columns (CEDARS' statewide programs download).")
        changed = skipped = 0
        for r in rows:
            prg, lead_text = r["PrgID"].strip(), r["LeadPA"].strip()
            m = CODE.search(lead_text)
            lead = access.by_code(m.group(1)) if m else None
            program = Program.objects.filter(prg_id__iexact=prg).first()
            if lead is None or program is None or not program.statewide:
                why = "lead PA not found" if lead is None else "not in the registry" if program is None else "not a statewide program"
                self.stdout.write(f"skipped {prg}: {why} ({lead_text})")
                skipped += 1
                continue
            if program.lead_id == lead.pk:
                continue
            self.stdout.write(f"{prg}: {program.lead.code if program.lead_id else 'no lead'} -> {lead.code}")
            if apply:
                service.set_lead(None, program, lead, reason)
            changed += 1
        verb = "changed" if apply else "would change"
        self.stdout.write(f"{verb} {changed} lead{'s' if changed != 1 else ''}; skipped {skipped}")
