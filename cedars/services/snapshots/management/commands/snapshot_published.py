from django.core.management.base import BaseCommand

from cedars.services.snapshots import service
from cedars.services.submissions.models import SubmissionVersion


class Command(BaseCommand):
    help = "Snapshot every published version that has measure-level results but no snapshot (after a failed copy, or older data)."

    def handle(self, *args, **options):
        todo = SubmissionVersion.objects.filter(
            status=SubmissionVersion.Status.PUBLISHED, submission__deadline__module__in=list(service.MODULE_DATASETS), snapshot__isnull=True
        ).exclude(origin="imported")
        made = 0
        for v in todo.order_by("published_at", "pk"):
            if service.source_key(v) and service.publish(v.pk):
                made += 1
        self.stdout.write(f"{made} snapshot{'' if made == 1 else 's'} made")
