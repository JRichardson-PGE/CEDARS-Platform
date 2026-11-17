"""The shared cache's table (settings.CACHES, a DatabaseCache): rate limits count across every web process."""

from django.core.management import call_command
from django.db import migrations


def create(apps, schema_editor):
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


def drop(apps, schema_editor):
    schema_editor.execute("DROP TABLE IF EXISTS cedars_cache")


class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(create, drop)]
