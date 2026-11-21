import logging
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection

from cedars.modules.calculations import dispatch, reference_folder
from cedars.services.submissions import queued


def _queued_checks() -> int:
    """With queued checks on AWS Batch too: fail those whose job ended unfinished and submit the next ones."""
    return queued.sync_batch() if queued.backend() == "batch" else 0


log = logging.getLogger(__name__)


def _reference_folder() -> None:
    """Bring the workers' shared reference folder up to date with the data packs, where this deployment keeps one."""
    if reference_folder.folder() is None:
        return
    try:
        done = reference_folder.sync()
    except Exception:  # noqa: BLE001 - logged; the next pass retries
        log.exception("reference folder sync failed")
        return
    finally:
        connection.close()  # run in a thread of its own by --loop
    if done["copied"] or done["removed"] or done["failed"]:
        log.info("reference folder: %s copied, %s removed, %s failed", len(done["copied"]), len(done["removed"]), len(done["failed"]))


class Command(BaseCommand):
    help = (
        "Bring AWS Batch runs', check jobs' and queued checks' records up to date: progress and receipts from storage, jobs Batch ended. "
        "Also hands fast-lane work to AWS Batch when no fast worker is ready, and keeps the workers' shared reference folder up to date "
        "(CALCULATIONS_REFERENCE_FOLDER). Run it as a service with --loop (every 10 seconds), or every minute."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep running, syncing every --sleep seconds")
        parser.add_argument("--sleep", type=float, default=10.0)
        parser.add_argument("--limit", type=int, help="with --loop: stop after this many passes")

    def handle(self, *args, loop=False, sleep=10.0, limit=None, **options):
        if not loop:
            _reference_folder()
            self.stdout.write(
                f"{dispatch.hand_over_fast_work()} fast-lane job(s) handed over, "
                f"{dispatch.sync_batch()} run(s) ended, {dispatch.sync_batch_checks()} check job(s) ended, "
                f"{_queued_checks()} queued check(s) failed"
            )
            return
        passes, folder, synced = 0, None, None
        every = getattr(settings, "CALCULATIONS_REFERENCE_SYNC_SECONDS", 300)
        while limit is None or passes < limit:
            close_old_connections()
            # the folder is filled in a thread of its own: copying a new profile library takes minutes, and runs must keep syncing
            if (folder is None or not folder.is_alive()) and (synced is None or time.monotonic() - synced >= every):
                synced = time.monotonic()
                folder = threading.Thread(target=_reference_folder, name="reference-folder", daemon=True)
                folder.start()
            try:
                handed = dispatch.hand_over_fast_work()
                if handed:
                    log.info("calculations sync: %s fast-lane job(s) handed to AWS Batch or failed", handed)
                ended, checked = dispatch.sync_batch(), dispatch.sync_batch_checks()
                _queued_checks()
                if ended or checked:
                    log.info("calculations sync: %s run(s) and %s check job(s) ended", ended, checked)
            except Exception:  # noqa: BLE001 - a failed pass (Batch or S3 throttling, a lost connection) is logged; the next one retries
                log.exception("calculations sync pass failed")
            passes += 1
            if limit is None or passes < limit:
                time.sleep(sleep)
        if folder is not None:
            folder.join()
        self.stdout.write(f"{passes} pass(es)")
