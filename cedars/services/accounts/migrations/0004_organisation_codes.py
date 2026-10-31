"""Organisation codes with their history (Jake, 2026-10-31: "AVA used to be EBCE ... keep the data traceable as the same party").

Adds OrganisationCode (every code an organisation has had, and when) and Organisation.successor (an organisation recorded twice, merged
into the other). Each organisation already in the platform gets its code as its current one, in use from the start. Then the renames
CEDARS has made (a copy of accounts.service.KNOWN_RENAMES as it stood, so the migration does not change when that table does) are
applied to what is here, each an audit event:
  - only the old code's organisation: renamed to the new code from the rename's first day;
  - only the new code's organisation: given the old code as a former code, up to that day;
  - both (e.g. legacy EBCE files loaded beside AVA's): the old one merged into the new, unless it has members (left for a CPUC system
    administrator, who moves them first);
  - neither: nothing (a later load under either code finds or makes the one organisation).
"""

from datetime import date

import django.contrib.postgres.constraints
import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models

import cedars.services.accounts.models

KNOWN_RENAMES = {
    "EBCE": (
        "AVA",
        "Ava Community Energy",
        date(2024, 1, 1),
        "As of 2024, East Bay Community Energy (EBCE) changed its name to Ava Community Energy (AVA): budget filings, claims and monthly "
        "reports before 2024 are under EBCE, from 2024 under AVA (CEDARS changelog, 2025-02-27)",
    ),
}


def _audit(AuditEvent, org, action, before, after, reason):
    AuditEvent.objects.create(
        actor_label="system",
        action=action,
        object_type="accounts.organisation",
        object_id=str(org.pk),
        object_repr=f"{org.code} ({org.name})"[:300],
        reason=reason,
        before=before,
        after=after,
    )


def record_codes(apps, schema_editor):
    Organisation = apps.get_model("accounts", "Organisation")
    OrganisationCode = apps.get_model("accounts", "OrganisationCode")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    for org in Organisation.objects.order_by("pk"):
        OrganisationCode.objects.get_or_create(organisation=org, valid_to=None, defaults={"code": org.code})
    for old_code, (new_code, new_name, first_day, reason) in KNOWN_RENAMES.items():
        old = Organisation.objects.filter(code=old_code).first()
        new = Organisation.objects.filter(code=new_code).first()
        if old is None and new is None:
            continue
        if new is None:
            before = {"code": old.code, "name": old.name}
            OrganisationCode.objects.filter(organisation=old, valid_to=None).update(valid_to=first_day)
            OrganisationCode.objects.create(organisation=old, code=new_code, valid_from=first_day, reason=reason)
            old.code = new_code
            if old.name.strip().upper() == old_code:
                old.name = new_name
            old.save(update_fields=["code", "name"])
            after = {"code": old.code, "name": old.name, "former_code": old_code, "effective": first_day.isoformat()}
            _audit(AuditEvent, old, "organisation.renamed", before, after, reason)
        elif old is None:
            if OrganisationCode.objects.filter(code=old_code).exists():
                continue
            row = OrganisationCode.objects.get(organisation=new, valid_to=None)
            if row.valid_from is not None and row.valid_from < first_day:
                continue
            row.valid_from = first_day
            row.save(update_fields=["valid_from"])
            OrganisationCode.objects.create(organisation=new, code=old_code, valid_to=first_day, reason=reason)
            after = {"codes": [f"{old_code} (until {date(first_day.year - 1, 12, 31)})", f"{new_code} (from {first_day})"]}
            _audit(AuditEvent, new, "organisation.code_added", {"codes": [new_code]}, after, reason)
        elif old.successor_id is None and not old.memberships.filter(ended_at__isnull=True).exists():
            OrganisationCode.objects.filter(organisation=old, valid_to=None).update(valid_to=first_day)
            OrganisationCode.objects.filter(organisation=new, valid_to=None, valid_from=None).update(valid_from=first_day)
            before = {"source": {"code": old.code, "active": old.active}}
            old.successor_id, old.merged_on, old.active = new.pk, first_day, False
            old.save(update_fields=["successor", "merged_on", "active"])
            after = {"source": {"code": old.code, "active": False, "successor": new.code, "merged_on": first_day.isoformat()}}
            _audit(AuditEvent, old, "organisation.merged", before, after, reason)
            _audit(AuditEvent, new, "organisation.merged", before, after, reason)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_pa_types"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        # The exclusion constraints compare codes and organisation ids inside a GiST index (registry's migration 0001 adds it too).
        migrations.RunSQL("CREATE EXTENSION IF NOT EXISTS btree_gist", migrations.RunSQL.noop),
        migrations.AddField(
            model_name="organisation",
            name="merged_on",
            field=models.DateField(blank=True, help_text="When the successor took over", null=True),
        ),
        migrations.AddField(
            model_name="organisation",
            name="successor",
            field=models.ForeignKey(
                blank=True,
                help_text="Set when it was merged into another organisation as the same party; its data stays its own and counts under it",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="predecessors",
                to="accounts.organisation",
            ),
        ),
        migrations.CreateModel(
            name="OrganisationCode",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("code", models.CharField(max_length=20)),
                (
                    "valid_from",
                    models.DateField(
                        blank=True,
                        help_text="First day in use; blank: from the start",
                        null=True,
                    ),
                ),
                (
                    "valid_to",
                    models.DateField(
                        blank=True,
                        help_text="First day no longer in use; blank: the current code",
                        null=True,
                    ),
                ),
                ("reason", models.TextField(blank=True)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organisation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="codes",
                        to="accounts.organisation",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": [
                    "organisation__code",
                    models.OrderBy(models.F("valid_from"), nulls_first=True),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("valid_from__isnull", True),
                            ("valid_to__isnull", True),
                            ("valid_from__lt", models.F("valid_to")),
                            _connector="OR",
                        ),
                        name="accounts_code_range_not_empty",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("code", django.db.models.functions.text.Upper("code"))),
                        name="accounts_code_upper_case",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("valid_to__isnull", True)),
                        fields=("organisation",),
                        name="accounts_one_current_code",
                    ),
                    django.contrib.postgres.constraints.ExclusionConstraint(
                        expressions=[("code", "="), ("organisation", "<>")],
                        name="accounts_code_one_organisation",
                    ),
                    django.contrib.postgres.constraints.ExclusionConstraint(
                        expressions=[
                            ("organisation", "="),
                            (
                                cedars.services.accounts.models.CodeRange("valid_from", "valid_to"),
                                "&&",
                            ),
                        ],
                        name="accounts_codes_do_not_overlap",
                    ),
                ],
            },
        ),
        # Leaving the history on the way back would be meaningless without its table; the codes themselves stay as renamed.
        migrations.RunPython(record_codes, migrations.RunPython.noop),
    ]
