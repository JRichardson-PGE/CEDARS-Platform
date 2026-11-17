from django.core.management.base import BaseCommand

from cedars.modules.calculations import dispatch


class Command(BaseCommand):
    help = (
        "Price queued runs and run check jobs with the local backend: take a check job of this worker's release (`recet worker check`) "
        "if one waits, else a run (`recet worker run` on its job spec), record the receipt, repeat."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="price at most one run, then stop")
        parser.add_argument("--lane", choices=["small", "large", "any"], default="any", help="which queue to serve (default: small first)")
        parser.add_argument(
            "--checks", choices=["also", "only", "never"], default="also", help="check jobs: also (first, the default), only, or never"
        )

    def handle(self, *args, once=False, lane="any", checks="also", **options):
        lanes = ("small", "large") if lane == "any" else (lane,)
        n = dispatch.work(once=once, lanes=lanes, take_checks=checks != "never", price=checks != "only")
        if once:
            self.stdout.write(f"{n} run(s) priced")
