"""Every deadline already on the calendar is pinned to the current version of its module's filing specification (the starting
versions), except a legacy CEDARS year imported as published (never checked, so it has none) and the 13th-month report (generated, not
uploaded)."""

from django.db import migrations

NOTHING_TO_FILE = {("monthly", "13th-month report")}


def pin(apps, schema_editor):
    Deadline = apps.get_model("submissions", "Deadline")
    Version = apps.get_model("specifications", "SpecificationVersion")
    for spec_version in [
        Version.objects.filter(specification=s, status="published").order_by("-published_at", "-pk").first()
        for s in apps.get_model("specifications", "Specification").objects.all()
    ]:
        if spec_version is None:
            continue
        module = spec_version.specification.module
        for d in Deadline.objects.filter(module=module, specification_version__isnull=True):
            if (module, d.kind) in NOTHING_TO_FILE or d.submissions.filter(versions__origin="imported").exists():
                continue
            d.specification_version = spec_version
            d.save(update_fields=["specification_version"])


def unpin(apps, schema_editor):
    apps.get_model("submissions", "Deadline").objects.update(specification_version=None)


class Migration(migrations.Migration):
    dependencies = [
        ("submissions", "0010_specification_version"),
        ("specifications", "0002_starting_versions"),
    ]

    operations = [migrations.RunPython(pin, unpin)]
