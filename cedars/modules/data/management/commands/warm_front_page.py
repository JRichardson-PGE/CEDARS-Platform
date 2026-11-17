"""Read the front page's portfolio trends before anyone asks for them (data/front.py): after start-up, a deploy or a publication."""

from django.core.management.base import BaseCommand

from cedars.modules.data import front


class Command(BaseCommand):
    help = "Read and keep the published claims the front page's charts are drawn from."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="kept for older scripts: the charts read every year at once")

    def handle(self, *args, **options):
        self.stdout.write(f"Front page: {front.warm():.1f} s")
