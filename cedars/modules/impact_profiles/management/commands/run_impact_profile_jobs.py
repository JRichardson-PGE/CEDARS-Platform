import time

from django.core.management.base import BaseCommand

from cedars.modules.impact_profiles import jobs


class Command(BaseCommand):
    help = "Run queued Impact profiles jobs (upload checks, generation, syncs, library writes), one at a time per PA queue."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep running, checking for new jobs every --sleep seconds")
        parser.add_argument("--sleep", type=float, default=5.0)
        parser.add_argument("--limit", type=int, help="stop after this many jobs")

    def handle(self, *args, loop=False, sleep=5.0, limit=None, **options):
        total = 0
        while True:
            total += jobs.run_pending(limit=None if limit is None else limit - total)
            if not loop or (limit is not None and total >= limit):
                break
            time.sleep(sleep)
        self.stdout.write(f"{total} jobs run")
