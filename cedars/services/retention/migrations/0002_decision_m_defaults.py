from django.db import migrations

# Decision M (Jake, 2026-10-29): the defaults, each changeable on the Retention page.
DEFAULTS = [
    ("filing_version", "Filing versions (confirmed or published)", None, "confirmation"),
    ("superseded_version", "Versions superseded before the due date", 0, "being superseded (un-confirm keeps one version, so none exist)"),
    ("publication_snapshot", "Publication snapshots", None, "the due date"),
    ("audit_event", "Audit events", None, "the event"),
    ("legacy_year", "Imported legacy CEDARS years", None, "import"),
    ("scratch_run", "Scratch calculation runs (not cited by a filing version)", 30, "the run's end"),
    ("unsubmitted_draft", "Unsubmitted filing drafts", 30, "the draft's creation"),
    ("sandbox_data", "Sandbox data", 30, "its creation"),
    ("vault_pii", "PII in the vault", None, "kept as long as the claim version that cites it"),
    ("notification", "Notifications", 365, "being sent"),
    ("application_log", "Application logs", 365, "being written"),
]


def seed(apps, schema_editor):
    RetentionPolicy = apps.get_model("retention", "RetentionPolicy")
    for code, label, days, counted_from in DEFAULTS:
        RetentionPolicy.objects.get_or_create(record_class=code, defaults={"label": label, "keep_days": days, "counted_from": counted_from})


class Migration(migrations.Migration):
    dependencies = [("retention", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
