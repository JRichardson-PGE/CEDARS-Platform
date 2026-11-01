from django.db import migrations


def seed(apps, schema_editor):
    RetentionPolicy = apps.get_model("retention", "RetentionPolicy")
    RetentionPolicy.objects.get_or_create(
        record_class="feedback",
        defaults={"label": "Feedback messages (closed ones)", "keep_days": 730, "counted_from": "being sent"},
    )


class Migration(migrations.Migration):
    dependencies = [("feedback", "0001_initial"), ("retention", "0002_decision_m_defaults")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
