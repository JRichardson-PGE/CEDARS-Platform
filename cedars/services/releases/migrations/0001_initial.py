import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

# A published release note never changes and is never deleted, whoever runs the SQL (BR-12.1): only its withdrawal may be recorded,
# once. A published note's changes are fixed with it.
FORWARD = """
CREATE FUNCTION releases_note_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.published_at IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'a published release note is never deleted (withdraw it)' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.title <> OLD.title OR NEW.version <> OLD.version OR NEW.summary <> OLD.summary OR NEW.effective_on <> OLD.effective_on
           OR NEW.short_notice_reason <> OLD.short_notice_reason OR NEW.published_at IS DISTINCT FROM OLD.published_at
           OR (NEW.published_by_id IS DISTINCT FROM OLD.published_by_id AND NEW.published_by_id IS NOT NULL)
           OR (OLD.withdrawn_at IS NOT NULL AND (NEW.withdrawn_at IS DISTINCT FROM OLD.withdrawn_at
               OR NEW.withdrawn_reason <> OLD.withdrawn_reason)) THEN
            RAISE EXCEPTION 'a published release note does not change (withdraw it and publish a new one)'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;
CREATE TRIGGER releases_releasenote_guard
    BEFORE UPDATE OR DELETE ON releases_releasenote
    FOR EACH ROW EXECUTE FUNCTION releases_note_guard();

CREATE FUNCTION releases_change_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    note_id bigint := CASE WHEN TG_OP = 'INSERT' THEN NEW.note_id ELSE OLD.note_id END;
BEGIN
    IF EXISTS (SELECT 1 FROM releases_releasenote WHERE id = note_id AND published_at IS NOT NULL) THEN
        RAISE EXCEPTION 'a published release note''s changes are fixed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.note_id <> OLD.note_id
       AND EXISTS (SELECT 1 FROM releases_releasenote WHERE id = NEW.note_id AND published_at IS NOT NULL) THEN
        RAISE EXCEPTION 'a published release note''s changes are fixed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;
CREATE TRIGGER releases_change_guard
    BEFORE INSERT OR UPDATE OR DELETE ON releases_change
    FOR EACH ROW EXECUTE FUNCTION releases_change_guard();
"""
BACKWARD = """
DROP TRIGGER IF EXISTS releases_change_guard ON releases_change;
DROP FUNCTION IF EXISTS releases_change_guard();
DROP TRIGGER IF EXISTS releases_releasenote_guard ON releases_releasenote;
DROP FUNCTION IF EXISTS releases_note_guard();
"""


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReleaseNote",
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
                ("title", models.CharField(max_length=200)),
                (
                    "version",
                    models.CharField(
                        blank=True,
                        help_text="The release it ships in, e.g. 0.3.0; blank when no new code is needed",
                        max_length=20,
                        verbose_name="Platform release",
                    ),
                ),
                (
                    "summary",
                    models.TextField(help_text="What changes and why, in a few sentences"),
                ),
                ("effective_on", models.DateField(verbose_name="Takes effect on")),
                (
                    "short_notice_reason",
                    models.TextField(
                        blank=True,
                        help_text="Why it is published with less than the usual notice",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("withdrawn_at", models.DateTimeField(blank=True, null=True)),
                ("withdrawn_reason", models.TextField(blank=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "published_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "withdrawn_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-effective_on", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="Change",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            (
                                "specification",
                                "Specification (fields, templates, value lists)",
                            ),
                            ("rule", "Validation rules"),
                            ("limit", "A limit"),
                            ("system", "How CEDARS works"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "area",
                    models.CharField(max_length=20, verbose_name="Part of CEDARS"),
                ),
                ("text", models.TextField(verbose_name="What changes")),
                (
                    "action",
                    models.TextField(blank=True, verbose_name="What you need to do"),
                ),
                ("limit", models.CharField(blank=True, max_length=40)),
                (
                    "limit_value",
                    models.CharField(
                        blank=True,
                        help_text="As people will see it, e.g. 200 MB",
                        max_length=60,
                        verbose_name="New value",
                    ),
                ),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "note",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="changes",
                        to="releases.releasenote",
                    ),
                ),
            ],
            options={
                "ordering": ["note", "position", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="releasenote",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    models.Q(("version", ""), _negated=True),
                    ("withdrawn_at__isnull", True),
                ),
                fields=("version",),
                name="releases_one_note_per_version",
            ),
        ),
        migrations.RunSQL(FORWARD, BACKWARD),
    ]
