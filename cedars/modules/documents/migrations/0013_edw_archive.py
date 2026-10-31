"""The EM&V site's home-page files (2006-2013, www.energydataweb.com/cpuc): the deliverable types its project status spreadsheets and
program information need beside Quarterly Meeting Materials (0002), so the migration of those files (cedars_import) can file each one;
and the CEDARS record key's help names the site. CPUC ED keeps the lists from here."""

from django.db import migrations, models
from django.db.models import Max

ADDITIONS = {"emv": ["Project Status Report", "Program Information"]}


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
    dependencies = [("documents", "0012_cedars_types")]
    operations = [
        migrations.AlterField(
            model_name="document",
            name="cedars_key",
            field=models.CharField(
                blank=True,
                help_text="<collection>:<key> of the CEDARS document (or the 2006-2013 EM&V site's file, emv:edw/...) it was migrated from",
                max_length=120,
                null=True,
                unique=True,
                verbose_name="CEDARS record",
            ),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
