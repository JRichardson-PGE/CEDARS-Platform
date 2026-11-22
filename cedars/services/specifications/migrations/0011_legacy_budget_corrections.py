from django.db import migrations


def load_corrections(apps, schema_editor):
    """The legacy correction versions for budget filings (legacy_corrections/budget_filing-*.json), published; the claims ones are
    there from 0010, and loading skips them."""
    from cedars.services.specifications import legacy_corrections

    legacy_corrections.load(apps)


class Migration(migrations.Migration):
    dependencies = [("specifications", "0010_legacy_corrections")]

    operations = [migrations.RunPython(load_corrections, migrations.RunPython.noop)]
