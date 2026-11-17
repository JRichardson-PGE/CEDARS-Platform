from django.db import migrations

REQUIREMENTS = [
    (
        "acc_electric",
        "source_workbook",
        "Every hourly avoided cost reconciles to the published ACC workbook (each IOU, climate zone and year).",
    ),
    (
        "acc_electric",
        "reference_measures",
        "A reference set of measures priced by the independent reference calculation matches the engine.",
    ),
    ("acc_gas", "source_workbook", "Every monthly gas avoided cost reconciles to the published gas ACC workbook."),
    (
        "acc_gas",
        "reference_measures",
        "A reference set of gas measures priced by the independent reference calculation matches the engine.",
    ),
    ("engine_release", "reference_measures", "The release prices the reference measures as the independent reference calculation does."),
    ("impact_profiles", "profile_checks", "Every profile is 8,760 hours and each shape sums to 1 (the module's four validation stages)."),
]

# A published pack's identifying fields never change, and a published or withdrawn pack is never deleted, whoever runs the SQL.
FORWARD = """
CREATE FUNCTION refdata_pack_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'draft' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'a published data pack is never deleted' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.kind <> OLD.kind OR NEW.label <> OLD.label OR NEW.version <> OLD.version OR NEW.manifest <> OLD.manifest
           OR NEW.fingerprint <> OLD.fingerprint OR NEW.storage_uri <> OLD.storage_uri OR NEW.status = 'draft' THEN
            RAISE EXCEPTION 'a published data pack does not change (register a new version)' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;
CREATE TRIGGER refdata_packversion_guard
    BEFORE UPDATE OR DELETE ON refdata_packversion
    FOR EACH ROW EXECUTE FUNCTION refdata_pack_guard();
"""
BACKWARD = """
DROP TRIGGER IF EXISTS refdata_packversion_guard ON refdata_packversion;
DROP FUNCTION IF EXISTS refdata_pack_guard();
"""


def seed(apps, schema_editor):
    TieOutRequirement = apps.get_model("refdata", "TieOutRequirement")
    for kind, check, description in REQUIREMENTS:
        TieOutRequirement.objects.get_or_create(kind=kind, name=check, defaults={"description": description})


class Migration(migrations.Migration):
    dependencies = [("refdata", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop), migrations.RunSQL(FORWARD, BACKWARD)]
