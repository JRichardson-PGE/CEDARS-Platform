"""The document types CEDARS' DEER Resources and program documents have beyond the first lists (0002): the DEER sections the public
pages showed on 2026-10-31 (ex ante review memos, tools, the DEER database change log, help) and the budget filing appendix, so the
migration of CEDARS' documents (cedars_import) can file each one. CPUC ED keeps the lists from here."""

from django.db import migrations
from django.db.models import Max

ADDITIONS = {
    "deer": ["Ex ante review memo", "Tool", "DEER database change log", "Help"],
    "program": ["Budget filing appendix"],
}


def seed(apps, schema_editor):
    AttributeList = apps.get_model("documents", "AttributeList")
    AttributeOption = apps.get_model("documents", "AttributeOption")
    for collection, labels in ADDITIONS.items():
        al = AttributeList.objects.filter(type_of=collection).first()
        if al is None:
            continue
        top = al.options.aggregate(m=Max("position"))["m"] or 0
        for j, label in enumerate(labels, start=1):
            if not al.options.filter(label__iexact=label).exists():
                AttributeOption.objects.create(attribute_list=al, label=label, position=top + j)


class Migration(migrations.Migration):
    dependencies = [("documents", "0011_cedars_migration")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
