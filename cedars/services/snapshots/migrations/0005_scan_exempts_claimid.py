"""The publication scan leaves out ClaimID, the PAs' claim record key (Jake, 2026-11-12): some PAs' claim IDs embed their own tracking
numbers, which read as account numbers. New settings get it from pii.DEFAULT_EXEMPT; this adds it to settings already saved (unless a
CPUC system administrator listed it already), as an audited settings change.
Taking it out again is a change on the Publication scan page."""

from django.db import migrations

REASON = "ClaimID is the PA's claim record key, not personal information: left out of the publication scan like CEInputID and PrgID."


def exempt_claim_id(apps, schema_editor):
    ScanSettings = apps.get_model("snapshots", "ScanSettings")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    for cfg in ScanSettings.objects.all():
        if any(c.strip().lower() == "claimid" for c in cfg.exempt_columns):
            continue
        before = {"patterns": list(cfg.patterns), "exempt_columns": list(cfg.exempt_columns)}
        cfg.exempt_columns = [*cfg.exempt_columns, "ClaimID"]
        cfg.save(update_fields=["exempt_columns", "changed_at"])
        AuditEvent.objects.create(
            actor_label="system",
            action="snapshot.scan_settings_changed",
            object_type="snapshots.scansettings",
            object_id=str(cfg.pk),
            object_repr="Publication scan settings",
            reason=REASON,
            before=before,
            after={"patterns": list(cfg.patterns), "exempt_columns": cfg.exempt_columns},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("snapshots", "0004_piiscan_certified"),
        ("audit", "0003_us_spelling"),
    ]

    operations = [migrations.RunPython(exempt_claim_id, migrations.RunPython.noop)]
