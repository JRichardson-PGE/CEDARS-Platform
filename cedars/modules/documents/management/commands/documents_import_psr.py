from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.modules.documents import psr_import
from cedars.modules.documents.service import DocumentError


class Command(BaseCommand):
    help = (
        "Migrate the PSR's projects into the study register (BR-15.10), from a saved /api/projects response or the site itself, with "
        "their free-text lists mapped onto the register's (--map; --write-map lists every value and what it maps to now). Projects "
        "already in the register are skipped. Run the PDA import first, and try --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--projects", type=Path, help="the saved /api/projects response (every project)")
        parser.add_argument("--site", help="the PSR's address (e.g. https://psr.energydataweb.com), when no --projects file is given")
        parser.add_argument("--map", type=Path, help="a CSV of field, psr_value, register_value adding to or overriding the mapping")
        parser.add_argument("--write-map", type=Path, help="write every PSR value and what it maps to now to this CSV, and import nothing")
        parser.add_argument("--dry-run", action="store_true", help="read and map everything, store nothing, and report")
        parser.add_argument("--limit", type=int, help="import only the first N projects (by PSR number)")

    def handle(self, *args, projects=None, site=None, map=None, write_map=None, dry_run=False, limit=None, **o):
        try:
            if projects:
                records = psr_import.records_of(psr_import.load_json(projects))
            elif site:
                records = psr_import.records_of(psr_import.fetch(site, "/api/projects"))
            else:
                raise CommandError("Give --projects or --site: where the projects come from.")
            records = sorted(records, key=lambda r: int(r["projectID"]))[:limit] if limit else records
            mapping = psr_import.Mapping(psr_import.load_map(map))
            if write_map:
                n = psr_import.write_map(records, mapping, write_map)
                self.stdout.write(f"{n} values written to {write_map}: fill in register_value where it is blank, then give it as --map.")
                return
            report = psr_import.run(records, mapping, dry_run=dry_run)
        except DocumentError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(("Dry run: nothing stored.\n" if dry_run else "") + "\n".join(report.lines()))
