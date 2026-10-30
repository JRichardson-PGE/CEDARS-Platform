"""A published version's revision stage never changes either (BR-15.11), and comments and responses are a public record (BR-15.4): a
response is never changed or deleted, and a comment is never deleted and changes only once, when its author withdraws it."""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION documents_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.published_at IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'a published document version is never deleted' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.document_id <> OLD.document_id OR NEW.number <> OLD.number OR NEW.reason <> OLD.reason
           OR NEW.organisation_id <> OLD.organisation_id OR NEW.published_at IS DISTINCT FROM OLD.published_at
           OR NEW.published_by_id IS DISTINCT FROM OLD.published_by_id OR NEW.stage <> OLD.stage
           OR NEW.stage_number IS DISTINCT FROM OLD.stage_number OR NEW.responding_pa_id IS DISTINCT FROM OLD.responding_pa_id THEN
            RAISE EXCEPTION 'a published document version does not change (add a version)' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE FUNCTION documents_comment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a comment is never deleted (its author may withdraw it)' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.withdrawn_at IS NOT NULL OR NEW.withdrawn_at IS NULL
       OR (to_jsonb(NEW) - 'withdrawn_at') <> (to_jsonb(OLD) - 'withdrawn_at') THEN
        RAISE EXCEPTION 'a comment does not change, except once when its author withdraws it' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER documents_comment_guard
    BEFORE UPDATE OR DELETE ON documents_comment
    FOR EACH ROW EXECUTE FUNCTION documents_comment_guard();

CREATE FUNCTION documents_response_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a response to a comment is never changed or deleted' USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE TRIGGER documents_commentresponse_guard
    BEFORE UPDATE OR DELETE ON documents_commentresponse
    FOR EACH ROW EXECUTE FUNCTION documents_response_guard();
"""
BACKWARD = """
DROP TRIGGER IF EXISTS documents_commentresponse_guard ON documents_commentresponse;
DROP FUNCTION IF EXISTS documents_response_guard();
DROP TRIGGER IF EXISTS documents_comment_guard ON documents_comment;
DROP FUNCTION IF EXISTS documents_comment_guard();
CREATE OR REPLACE FUNCTION documents_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
"""


class Migration(migrations.Migration):
    dependencies = [("documents", "0006_deliverables_and_comments")]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
