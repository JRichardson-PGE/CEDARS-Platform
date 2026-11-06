"""Make the front page's program-cycle cards before anyone asks for them (data/front.py): after start-up, a deploy or a publication."""

from django.core.management.base import BaseCommand

from cedars.modules.data import front


class Command(BaseCommand):
    help = "Make and keep the front page's cards for the default program cycle (--all: every cycle)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="every program cycle, not only the one the front page opens on")

    def handle(self, *args, **options):
        made = front.warm(all_cycles=options["all"])
        if not made:
            self.stdout.write("No program cycle is set up yet: nothing to make.")
        for cycle, seconds in made:
            self.stdout.write(f"{cycle}: {seconds:.1f} s")
