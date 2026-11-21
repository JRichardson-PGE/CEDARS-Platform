from django.core.management.base import BaseCommand, CommandError

from cedars.services.submissions import queued


class Command(BaseCommand):
    help = (
        "Check filing uploads too large to check while the page waits (queued checks): --loop keeps up to the queued_checks_at_once "
        "limit running, each in a process of its own; --check runs one (what that process and an AWS Batch job run)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep taking queued checks (the local backend's worker)")
        parser.add_argument("--once", action="store_true", help="run the queued checks waiting now, then stop")
        parser.add_argument("--check", help="run this one queued check (by id) here")
        parser.add_argument("--sync", action="store_true", help="AWS Batch: fail checks whose job ended unfinished, submit the next")
        parser.add_argument("--sleep", type=float, default=2.0)

    def handle(self, *args, loop=False, once=False, check=None, sync=False, sleep=2.0, **options):
        if check:
            q = queued.execute(check)
            self.stdout.write(f"{q.pk}: {q.get_status_display()}")
            if q.status != q.Status.SUCCEEDED:
                raise CommandError(q.error or "not checked")
            return
        if sync:
            self.stdout.write(f"{queued.sync_batch()} queued check(s) failed")
            return
        if not (loop or once):
            raise CommandError("say --loop, --once, --check <id> or --sync")
        n = queued.work(once=once, idle=sleep)
        self.stdout.write(f"{n} queued check(s) run")
