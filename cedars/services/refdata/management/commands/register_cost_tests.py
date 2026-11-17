from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cedars.services.refdata import cost_tests


class Command(BaseCommand):
    help = (
        "Register a cost-test definition set (a ReCET definition.yaml) as a draft data pack: the file goes to the reference-data store. "
        "Record its engine_check tie-out (record_tie_out) and publish it on its page."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "path", type=Path, help="the set's definition.yaml (e.g. ReCET's recet/data/cost_tests/EE-2026/definition.yaml)"
        )
        parser.add_argument("--source", default="", help="where the file came from (e.g. the ReCET release that ships it)")

    def handle(self, path, source, **opts):
        try:
            pack = cost_tests.register_set(None, path.read_bytes(), source=source or path.name)
        except cost_tests.SetError as e:
            raise CommandError(f"{path} is not a cost-test set: {e}") from None
        self.stdout.write(f"{pack}: {cost_tests.years_text(pack)}, SHA-256 {cost_tests.sha256(pack)} (pack {pack.pk}, draft)")
