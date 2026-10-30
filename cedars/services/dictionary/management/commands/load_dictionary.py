import json
from pathlib import Path

from django.core.management.base import BaseCommand

from cedars.services.dictionary import service


class Command(BaseCommand):
    help = "Publish a field catalog version from an exported catalog file (data/dictionary/recet-<version>.json)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--label")

    def handle(self, path, label, **opts):
        v = service.load_catalog(None, json.loads(path.read_text(encoding="utf-8")), label=label)
        self.stdout.write(f"published {v.label}: {v.fields.count()} fields")
