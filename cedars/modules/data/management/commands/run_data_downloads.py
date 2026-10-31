import time

from django.core.management.base import BaseCommand

from cedars.modules.data import jobs


class Command(BaseCommand):
    help = "Make queued record downloads (BR-07.2) and tell each person theirs is ready. Several workers can share the queue."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep running, checking for new downloads every --sleep seconds")
        parser.add_argument("--sleep", type=float, default=5.0)
        parser.add_argument("--limit", type=int, help="stop after this many downloads")

    def handle(self, *args, loop=False, sleep=5.0, limit=None, **options):
        total = 0
        while True:
            total += jobs.run_pending(limit=None if limit is None else limit - total)
            if not loop or (limit is not None and total >= limit):
                break
            time.sleep(sleep)
        self.stdout.write(f"{total} downloads made")
