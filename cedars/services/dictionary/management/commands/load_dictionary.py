import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from cedars.services.dictionary import service


class Command(BaseCommand):
    help = (
        "Publish a field catalog version from an exported catalog file (data/dictionary/recet-<version>.json), with the platform's "
        "own records added (every column of the tables the platform keeps: dictionary/platform.py) unless --engine-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--label")
        parser.add_argument("--engine-only", action="store_true", help="Only the engine's fields, without the platform's records")

    def handle(self, path, label, engine_only=False, **opts):
        try:
            v = service.load_catalog(None, json.loads(path.read_text(encoding="utf-8")), label=label, platform=not engine_only)
        except IntegrityError as e:
            raise CommandError(f"A catalog version with that label is already published; give another with --label. ({e})") from e
        records = v.fields.filter(kind="record").count()
        self.stdout.write(f"published {v.label}: {v.fields.count()} fields ({records} of them platform records)")
