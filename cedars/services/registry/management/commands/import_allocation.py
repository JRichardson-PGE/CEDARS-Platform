from pathlib import Path

from django.core.management.base import BaseCommand

from cedars.services.registry import service


class Command(BaseCommand):
    help = "Load a statewide allocation factor file (CEDARS' export layout) as a draft version; --confirm confirms it if it checks out."

    def add_arguments(self, parser):
        parser.add_argument("csv", type=Path)
        parser.add_argument("--label", required=True)
        parser.add_argument("--purpose", choices=["filing", "claims"], required=True)
        parser.add_argument("--cycle", type=int, help="first year of the filing cycle it belongs to, e.g. 2028")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, csv, label, purpose, cycle, confirm, **opts):
        version = service.import_allocation_csv(
            None,
            csv.read_bytes(),
            label=label,
            purpose=purpose,
            source_name=csv.name,
            filing_cycle=service.ensure_cycle(cycle) if cycle else None,
        )
        issues = service.check_allocation(version)
        for i in issues:
            self.stdout.write(f"{'problem' if i.blocking else 'note'}: {i.program_id} {i.year}: {i.message}")
        if confirm:
            service.confirm_allocation(None, version)
        self.stdout.write(f"{version.label}: {version.entries.count()} entries, {version.get_status_display().lower()}")
