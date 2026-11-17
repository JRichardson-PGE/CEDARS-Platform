from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from cedars.services.accounts.service import AccessDenied
from cedars.services.snapshots import service
from cedars.services.snapshots.models import PIIScan


class Command(BaseCommand):
    help = (
        "Run the publication scan again, with the settings and data dictionary as they are now, on every held publication (after the "
        "scan changed, e.g. when numeric columns stopped being read as phone numbers). Clear ones join the release; the rest stay "
        "held with new findings. Each rescan is audited under the CPUC system administrator named by --by."
    )

    def add_arguments(self, parser):
        parser.add_argument("--by", required=True, help="The email of the CPUC system administrator the rescans are recorded under")
        parser.add_argument("--dry-run", action="store_true", help="List the held publications only")

    def handle(self, *args, by, dry_run, **options):
        actor = get_user_model().objects.filter(email__iexact=by.strip(), is_active=True).first()
        if actor is None:
            raise CommandError(f"No active user {by}.")
        held = list(service.holds().filter(outcome=PIIScan.Outcome.HELD).order_by("scanned_at"))
        cleared = 0
        for scan in held:
            name = service.label(scan.snapshot.version)
            if dry_run:
                self.stdout.write(f"held: {name} ({scan.records} records)")
                continue
            try:
                after = service.rescan(actor, scan.snapshot)
            except AccessDenied as e:
                raise CommandError(str(e)) from None
            cleared += not after.held
            self.stdout.write(f"{'cleared' if not after.held else 'still held'}: {name}")
        if dry_run:
            self.stdout.write(f"{len(held)} held")
        else:
            self.stdout.write(f"{len(held)} rescanned, {cleared} cleared, {len(held) - cleared} still held")
