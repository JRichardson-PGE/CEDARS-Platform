"""Study lead gains CCA (UAT F22): community choice aggregators are program administrators and lead evaluation studies of their
programs, and the list offered only CPUC, IOU and REN. Same pattern as 0005's additions: added once, left alone if already there."""

from django.db import migrations
from django.db.models import Max


def add_cca(apps, schema_editor):
    AttributeList = apps.get_model("documents", "AttributeList")
    AttributeOption = apps.get_model("documents", "AttributeOption")
    al = AttributeList.objects.get(code="study_lead")
    if not al.options.filter(label__iexact="CCA").exists():
        top = al.options.aggregate(m=Max("position"))["m"] or 0
        AttributeOption.objects.create(attribute_list=al, label="CCA", position=top + 1)


class Migration(migrations.Migration):
    dependencies = [("documents", "0014_us_spelling")]
    operations = [migrations.RunPython(add_cca, migrations.RunPython.noop)]
