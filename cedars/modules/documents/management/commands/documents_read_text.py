import shutil
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand

from cedars.core import storage
from cedars.modules.documents import textract
from cedars.modules.documents.models import DocumentFile


class Command(BaseCommand):
    help = "Read the text of document files that have none yet (files stored before text was read, or an import), for search (BR-15.2)."

    def add_arguments(self, parser):
        parser.add_argument("--again", action="store_true", help="read every file again, also those already read")
        parser.add_argument("--limit", type=int, help="stop after this many files")

    def handle(self, *args, again=False, limit=None, **options):
        files = DocumentFile.objects.order_by("pk")
        if not again:
            files = files.filter(text__isnull=True)
        store = storage.store("uploads")
        read = failed = 0
        for f in files[:limit] if limit else files:
            if not textract.readable(f.name):
                textract.save(f, Path("/nonexistent"))  # records why not, so the file is not tried again
                continue
            with tempfile.NamedTemporaryFile(suffix=Path(f.name).suffix) as tmp:
                try:
                    with store.open(f.key) as src:
                        shutil.copyfileobj(src, tmp)
                except Exception as e:
                    self.stderr.write(f"{f.pk} {f.name}: {e}")
                    failed += 1
                    continue
                tmp.flush()
                textract.save(f, Path(tmp.name))
            read += 1
        self.stdout.write(f"{read} files read, {failed} not found in storage")
