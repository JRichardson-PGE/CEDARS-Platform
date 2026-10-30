from django.db import migrations

# The audit trail is append-only in the database itself, not just in the code: any UPDATE or DELETE on a row fails, whoever runs it.
# (TRUNCATE is left alone: it needs table ownership, and the test runner empties tables with it.)
FORWARD = """
CREATE FUNCTION audit_refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are append-only (% refused)', TG_OP USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE TRIGGER audit_auditevent_append_only
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_refuse_change();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS audit_auditevent_append_only ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_refuse_change();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
