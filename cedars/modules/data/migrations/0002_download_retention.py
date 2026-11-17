from django.db import migrations


def seed(apps, schema_editor):
    RetentionPolicy = apps.get_model("retention", "RetentionPolicy")
    RetentionPolicy.objects.get_or_create(
        record_class="data_download",
        defaults={"label": "Record downloads made in the background", "keep_days": 7, "counted_from": "the file being made"},
    )


class Migration(migrations.Migration):
    dependencies = [("data", "0001_initial"), ("retention", "0002_decision_m_defaults")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
