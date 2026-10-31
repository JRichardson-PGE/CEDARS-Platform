import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from cedars.modules.calculations import dispatch

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Bring AWS Batch runs' records up to date: progress and receipts from storage, jobs Batch ended. "
        "Run it every minute, or as a service with --loop."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep running, syncing every --sleep seconds")
        parser.add_argument("--sleep", type=float, default=60.0)
        parser.add_argument("--limit", type=int, help="with --loop: stop after this many passes")

    def handle(self, *args, loop=False, sleep=60.0, limit=None, **options):
        if not loop:
            self.stdout.write(f"{dispatch.sync_batch()} run(s) ended")
            return
        passes = 0
        while limit is None or passes < limit:
            close_old_connections()
            try:
                ended = dispatch.sync_batch()
                if ended:
                    log.info("calculations sync: %s run(s) ended", ended)
            except Exception:  # noqa: BLE001 - a failed pass (Batch or S3 throttling, a lost connection) is logged; the next one retries
                log.exception("calculations sync pass failed")
            passes += 1
            if limit is None or passes < limit:
                time.sleep(sleep)
        self.stdout.write(f"{passes} pass(es)")
