import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from cedars.modules.impact_profiles import jobs

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run queued Impact profiles jobs (upload checks, generation, syncs, library writes), one at a time per PA queue."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep running, checking for new jobs every --sleep seconds")
        parser.add_argument("--sleep", type=float, default=5.0)
        parser.add_argument("--limit", type=int, help="stop after this many jobs")
        parser.add_argument("--passes", type=int, help="with --loop: stop after this many checks for jobs")

    def handle(self, *args, loop=False, sleep=5.0, limit=None, passes=None, **options):
        total = checks = 0
        while True:
            close_old_connections()
            try:
                total += jobs.run_pending(limit=None if limit is None else limit - total)
            except Exception:  # noqa: BLE001 - with --loop a lost connection must not end the worker; a job's own failure is on the job
                if not loop:
                    raise
                log.exception("impact profiles worker: a pass failed")
            checks += 1
            if not loop or (limit is not None and total >= limit) or (passes is not None and checks >= passes):
                break
            time.sleep(sleep)
        self.stdout.write(f"{total} jobs run")
