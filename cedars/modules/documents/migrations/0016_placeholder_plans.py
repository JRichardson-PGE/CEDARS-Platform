"""Placeholder implementation plans (BR-13.6): a version can be filed as a placeholder, the full plan to come as a later version. Whether
a published version is a placeholder never changes, like the rest of it (the version guard of 0003 and 0007 now checks the flag too)."""

from django.db import migrations, models

GUARD = """
CREATE OR REPLACE FUNCTION documents_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.published_at IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'a published document version is never deleted' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.document_id <> OLD.document_id OR NEW.number <> OLD.number OR NEW.reason <> OLD.reason
           OR NEW.organisation_id <> OLD.organisation_id OR NEW.published_at IS DISTINCT FROM OLD.published_at
           OR NEW.published_by_id IS DISTINCT FROM OLD.published_by_id OR NEW.stage <> OLD.stage
           OR NEW.stage_number IS DISTINCT FROM OLD.stage_number OR NEW.responding_pa_id IS DISTINCT FROM OLD.responding_pa_id
           {extra}THEN
            RAISE EXCEPTION 'a published document version does not change (add a version)' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;
"""
FORWARD = GUARD.replace("{extra}", "OR NEW.placeholder <> OLD.placeholder ")
BACKWARD = GUARD.replace("{extra}", "")


class Migration(migrations.Migration):
    dependencies = [("documents", "0015_study_lead_cca")]

    operations = [
        migrations.AddField(
            model_name="documentversion",
            name="placeholder",
            field=models.BooleanField(
                default=False,
                help_text="An implementation plan filed as a placeholder: the full plan is still to come, as a later version (BR-13.6)",
            ),
        ),
        migrations.RunSQL(FORWARD, BACKWARD),
    ]
