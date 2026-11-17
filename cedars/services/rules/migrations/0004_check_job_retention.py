from django.db import migrations


def seed(apps, schema_editor):
    RetentionPolicy = apps.get_model("retention", "RetentionPolicy")
    RetentionPolicy.objects.get_or_create(
        record_class="check_job",
        defaults={
            "label": "Check jobs (the copies of the tables a worker checked, and its findings)",
            "keep_days": 30,
            "counted_from": "the job",
        },
    )


class Migration(migrations.Migration):
    dependencies = [("rules", "0003_check_jobs"), ("retention", "0002_decision_m_defaults")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
