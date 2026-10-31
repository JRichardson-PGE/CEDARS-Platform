"""PA types (Jake, 2026-10-31): every program administrator is an IOU, a REN or a CCA, so the dashboards can select PAs by group. This
types the PAs already in the platform whose type is blank and whose code CEDARS uses (the table is a copy of
accounts.service.KNOWN_PA_TYPES as it stood, so the migration does not change when that table does). Each change is an audit event.
A PA under any other code stays blank for a CPUC system administrator to type on its organisation page."""

from django.db import migrations, models

KNOWN_PA_TYPES = {
    "PGE": "IOU",
    "SCE": "IOU",
    "SCG": "IOU",
    "SDGE": "IOU",
    "BAY": "REN",
    "CCR": "REN",
    "IREN": "REN",
    "RRN": "REN",
    "SCR": "REN",
    "SDRN": "REN",
    "TCR": "REN",
    "AVA": "CCA",
    "CPSF": "CCA",
    "EBCE": "CCA",
    "LCE": "CCA",
    "MCE": "CCA",
    "PCE": "CCA",
    "RCEA": "CCA",
    "SCP": "CCA",
    "SJCE": "CCA",
}
REASON = "PA types added: typed from the PA codes CEDARS uses (migration accounts/0003_pa_types)"


def type_pas(apps, schema_editor):
    Organisation = apps.get_model("accounts", "Organisation")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    for org in Organisation.objects.filter(kind="pa", pa_type="", code__in=list(KNOWN_PA_TYPES)).order_by("code"):
        before = {"name": org.name, "pa_type": org.pa_type}
        org.pa_type = KNOWN_PA_TYPES[org.code]
        org.save(update_fields=["pa_type"])
        AuditEvent.objects.create(
            actor_label="system",
            action="organisation.changed",
            object_type="accounts.organisation",
            object_id=str(org.pk),
            object_repr=f"{org.code} ({org.name})"[:300],
            reason=REASON,
            before=before,
            after={"name": org.name, "pa_type": org.pa_type},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_api_tokens"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organisation",
            name="pa_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("IOU", "Investor-owned utility"),
                    ("REN", "Regional energy network"),
                    ("CCA", "Community choice aggregator"),
                ],
                help_text="A program administrator's type, for grouping its data (e.g. all RENs); blank for other organisations",
                max_length=3,
                verbose_name="PA type",
            ),
        ),
        # Leaving it typed on the way back: untyping would itself be a change to audit, and the field stays.
        migrations.RunPython(type_pas, migrations.RunPython.noop),
    ]
