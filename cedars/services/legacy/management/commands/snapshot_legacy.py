from django.core.management.base import BaseCommand

from cedars.services.legacy import snapshot
from cedars.services.legacy.models import LegacyImport
from cedars.services.snapshots import service as snapshots
from cedars.services.submissions.models import SubmissionVersion


class Command(BaseCommand):
    help = (
        "Give every published legacy import its dashboard snapshot (imports loaded before the loads made them), then one release per "
        "dataset. Statewide unallocated claims get none. Safe to run again."
    )

    def handle(self, *args, **options):
        made, failed, datasets = 0, 0, set()
        todo = LegacyImport.objects.filter(
            version__status=SubmissionVersion.Status.PUBLISHED, version__snapshot__isnull=True, outside_totals=0
        )
        for imp in todo.select_related("version__submission__organisation", "version__submission__deadline").order_by("pk"):
            try:
                if snapshot.publish_snapshot(imp):
                    made += 1
                    datasets.add(snapshots.dataset_of(imp.version))
            except Exception as e:
                failed += 1
                self.stderr.write(f"{imp.version.submission.organisation.code} {imp.period}: {e}")
        snapshot.rebuild(d for d in datasets if d)
        self.stdout.write(f"{made} snapshot{'' if made == 1 else 's'} made, {failed} failed")
