from django.core.management.base import BaseCommand

from cedars.modules.calculations import dispatch


class Command(BaseCommand):
    help = "Price queued runs with the local backend: take a run, run `recet worker run` on its job spec, record the receipt, repeat."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="price at most one run, then stop")
        parser.add_argument("--lane", choices=["small", "large", "any"], default="any", help="which queue to serve (default: small first)")

    def handle(self, *args, once=False, lane="any", **options):
        lanes = ("small", "large") if lane == "any" else (lane,)
        n = dispatch.work(once=once, lanes=lanes)
        if once:
            self.stdout.write(f"{n} run(s) priced")
