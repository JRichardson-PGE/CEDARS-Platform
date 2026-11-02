from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("budgets", "0004_change_letter")]

    operations = [
        migrations.AddField(
            model_name="fundshift",
            name="factors",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Set on submission: {funding PA: {'PrgID year': the weighted allocation factor its portion was split by}}",
            ),
        ),
    ]
