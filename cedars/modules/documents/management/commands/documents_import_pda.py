import json
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.modules.documents import pda_import


def _records(data):
    """The PDA's list, bare or under a key (a saved response may wrap it)."""
    if isinstance(data, dict):
        for key in ("data", "deliverables", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        raise CommandError("The deliverables file holds no list of documents.")
    return data


class Command(BaseCommand):
    help = (
        "Migrate the PDA's published documents, their files, work orders, comment windows, comments and responses (BR-15.10), from saved "
        "API responses and a folder of files, or from the site itself. Already imported documents are skipped. Try --dry-run first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--deliverables", type=Path, help="the saved /api/deliverables response (every published document)")
        parser.add_argument("--work-orders", type=Path, help="the saved /api/work-orders response")
        parser.add_argument("--comments", type=Path, help="a folder of saved /api/deliverables-comments responses, <deliverableID>.json")
        parser.add_argument("--files", type=Path, help="a folder of the files, <deliverableID>/<file name>")
        parser.add_argument("--site", help="the PDA's address (e.g. https://pda.energydataweb.com): what no file above gives is read there")
        parser.add_argument("--dry-run", action="store_true", help="read and map everything, store nothing, and report")
        parser.add_argument("--limit", type=int, help="import only the first N documents (by PDA id)")

    def handle(self, *args, deliverables=None, work_orders=None, comments=None, files=None, site=None, dry_run=False, limit=None, **o):
        if files is None and site is None:
            raise CommandError("Give --files or --site: where the documents' files come from.")
        with tempfile.TemporaryDirectory() as work:
            web = pda_import.SiteSource(site, Path(work)) if site else None
            source = pda_import.FolderSource(files) if files else web
            if deliverables:
                records = _records(pda_import.load_json(deliverables))
            elif web:
                records = _records(web.get_json("/api/deliverables?searchOn=title&searchQuery="))
            else:
                raise CommandError("Give --deliverables or --site: where the document records come from.")
            records = sorted(records, key=lambda r: int(r[pda_import.FIELDS["id"]]))[:limit] if limit else records

            def comments_for(pda_id):
                if comments:
                    path = comments / f"{pda_id}.json"
                    return _records(pda_import.load_json(path)) if path.is_file() else []
                if web:
                    return _records(web.get_json(f"/api/deliverables-comments?deliverableID={pda_id}"))
                return []

            report = pda_import.Report()
            rows = pda_import.load_json(work_orders) if work_orders else (web.get_json("/api/work-orders") if web else [])
            pda_import.import_work_orders(_records(rows) if rows else [], report, dry_run=dry_run)
            pda_import.run(records, source, comments_for=comments_for, dry_run=dry_run, report=report)
        self.stdout.write(("Dry run: nothing stored.\n" if dry_run else "") + "\n".join(report.lines()))
        if o.get("verbosity", 1) > 1:
            self.stdout.write(json.dumps({"untyped": report.untyped, "no_main_file": report.no_main_file}, indent=1))
