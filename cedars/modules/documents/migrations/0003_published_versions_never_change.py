"""A published version and its files never change (BR-15.1, BR-09.6: a replacement is a new version). The database refuses an UPDATE
that changes a published version, any UPDATE of a file, and the DELETE of a published version or of one of its files."""

from django.db import migrations

FORWARD = """
CREATE FUNCTION documents_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.published_at IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'a published document version is never deleted' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.document_id <> OLD.document_id OR NEW.number <> OLD.number OR NEW.reason <> OLD.reason
           OR NEW.organisation_id <> OLD.organisation_id OR NEW.published_at IS DISTINCT FROM OLD.published_at
           OR NEW.published_by_id IS DISTINCT FROM OLD.published_by_id THEN
            RAISE EXCEPTION 'a published document version does not change (add a version)' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;
CREATE TRIGGER documents_documentversion_guard
    BEFORE UPDATE OR DELETE ON documents_documentversion
    FOR EACH ROW EXECUTE FUNCTION documents_version_guard();

CREATE FUNCTION documents_file_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'a document file does not change (add a version)' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (SELECT 1 FROM documents_documentversion v WHERE v.id = OLD.version_id AND v.published_at IS NOT NULL) THEN
        RAISE EXCEPTION 'a published document file is never deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER documents_documentfile_guard
    BEFORE UPDATE OR DELETE ON documents_documentfile
    FOR EACH ROW EXECUTE FUNCTION documents_file_guard();
"""
BACKWARD = """
DROP TRIGGER IF EXISTS documents_documentfile_guard ON documents_documentfile;
DROP FUNCTION IF EXISTS documents_file_guard();
DROP TRIGGER IF EXISTS documents_documentversion_guard ON documents_documentversion;
DROP FUNCTION IF EXISTS documents_version_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("documents", "0002_value_lists")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
