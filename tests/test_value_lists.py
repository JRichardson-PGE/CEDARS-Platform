"""Value lists an administrator edits (BR-12.6): versions with effective dates, retired codes kept, never deleted, every change audited,
and the version in force is what the rules and the input editor check against."""

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import value_lists as vls
from cedars.services.dictionary.models import Code, ValueListVersion

pytestmark = pytest.mark.django_db
recet = pytest.importorskip("recet", reason="needs the pinned ReCET engine (engine/README.md)")

TODAY = timezone.localdate()


@pytest.fixture(autouse=True)
def fresh_engine():
    """Each test starts with the engine's shipped lists, whatever an earlier test handed it."""
    vls.reset_engine()
    from recet import editor

    editor.set_value_list_changes({})
    yield
    vls.reset_engine()
    editor.set_value_list_changes({})


@pytest.fixture
def sector(cpuc_admin):
    return vls.get_list("sector")


def _published(cpuc_admin, vl, *, effective=TODAY, edit=None, reason="Test"):
    d = vls.start_draft(cpuc_admin, vl)
    if edit:
        edit(d)
    return vls.publish(cpuc_admin, d, effective, reason)


def test_the_engines_lists_are_offered(sector):
    names = vls.engine_lists()
    assert "sector" in names and "ac_climate_zone" in names and "prgid" not in names
    assert vls.get_list("sector") == sector
    with pytest.raises(vls.ValueListError):
        vls.get_list("no_such_list")


def test_first_draft_copies_the_shipped_list(cpuc_admin, sector):
    d = vls.start_draft(cpuc_admin, sector)
    shipped = vls.shipped("sector")
    assert d.number == 1 and d.status == "draft" and d.codes.count() == len(shipped)
    assert "ReCET" in d.based_on
    assert vls.in_force(sector) is None
    with pytest.raises(vls.ValueListError, match="already has a draft"):
        vls.start_draft(cpuc_admin, sector)
    assert AuditEvent.objects.filter(action="value_list.draft_started").exists()


def test_shipped_duplicates_are_kept_once():
    codes = [r["code"].lower() for r in vls.shipped("ntg")]
    assert len(codes) == len(set(codes))


def test_add_retire_reinstate_and_publish(cpuc_admin, sector):
    d = vls.start_draft(cpuc_admin, sector)
    vls.add_code(cpuc_admin, d, "Tribal", description="Tribal communities", start_year=2028, reason="New sector")
    with pytest.raises(vls.ValueListError, match="already in the list"):
        vls.add_code(cpuc_admin, d, "tribal")
    ag = d.codes.get(code="Agricultural")
    vls.change_code(cpuc_admin, ag, end_year=2029, reason="Merged into Industrial")
    ag.refresh_from_db()
    assert ag.end_year == 2029 and not ag.active_in(2030) and ag.active_in(2029)
    with pytest.raises(vls.ValueListError, match="never deleted"):
        vls.drop_code(cpuc_admin, ag)
    with pytest.raises(vls.ValueListError, match="start year stays"):
        vls.change_code(cpuc_admin, ag, start_year=2020)
    with pytest.raises(vls.ValueListError, match="before the start year"):
        vls.change_code(cpuc_admin, d.codes.get(code="Tribal"), end_year=2027)
    with pytest.raises(vls.ValueListError, match="reason"):
        vls.publish(cpuc_admin, d, TODAY, "")
    v1 = vls.publish(cpuc_admin, d, TODAY, "2028 sectors")
    assert v1.status == "published" and vls.in_force(sector) == v1
    changes = vls.changes_in(v1)
    assert [r["code"] for r in changes["added"]] == ["Tribal"] and [r["code"] for r in changes["retired"]] == ["Agricultural"]
    published = AuditEvent.objects.get(action="value_list.published")
    assert published.after["added"] == 1 and published.after["retired"] == 1 and published.reason == "2028 sectors"
    assert {"value_list.code_added", "value_list.code_retired"} <= set(AuditEvent.objects.values_list("action", flat=True))

    # the next version copies version 1; reinstating clears the end year
    d2 = vls.start_draft(cpuc_admin, sector)
    assert d2.number == 2 and d2.based_on == "version 1" and d2.codes.count() == v1.codes.count()
    vls.change_code(cpuc_admin, d2.codes.get(code="Agricultural"), end_year=None, reason="Kept after all")
    assert [r["code"] for r in vls.changes_in(d2)["reinstated"]] == ["Agricultural"]
    with pytest.raises(vls.ValueListError, match="takes effect after"):
        vls.publish(cpuc_admin, d2, TODAY, "Same day")
    v2 = vls.publish(cpuc_admin, d2, TODAY + timedelta(days=30), "Kept")
    assert vls.in_force(sector) == v1  # not yet in effect
    assert vls.in_force(sector, TODAY + timedelta(days=30)) == v2


def test_a_draft_may_drop_its_own_codes_only(cpuc_admin, sector):
    d = vls.start_draft(cpuc_admin, sector)
    c = vls.add_code(cpuc_admin, d, "Oops")
    vls.drop_code(cpuc_admin, c, reason="Typo")
    assert not d.codes.filter(code="Oops").exists()
    assert AuditEvent.objects.filter(action="value_list.code_dropped").exists()


def test_published_versions_and_codes_never_change(cpuc_admin, sector):
    v1 = _published(cpuc_admin, sector)
    code = v1.codes.first()
    with pytest.raises(vls.ValueListError, match="never changes"):
        vls.change_code(cpuc_admin, code, description="x")
    with pytest.raises(vls.ValueListError, match="never changes"):
        vls.add_code(cpuc_admin, v1, "New")
    for statement in (
        lambda: Code.objects.filter(pk=code.pk).update(description="changed"),
        lambda: Code.objects.filter(pk=code.pk).delete(),
        lambda: Code.objects.create(version=v1, code="Sneaked in"),
        lambda: ValueListVersion.objects.filter(pk=v1.pk).update(reason="changed"),
        lambda: ValueListVersion.objects.filter(pk=v1.pk).delete(),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            statement()


def test_only_system_admins_change_lists(pge_admin, sector):
    with pytest.raises(AccessDenied):
        vls.start_draft(pge_admin, sector)


def test_upload_adds_and_changes_but_never_deletes(cpuc_admin, sector):
    d = vls.start_draft(cpuc_admin, sector)
    before = d.codes.count()
    out = vls.upload_codes(
        cpuc_admin, d, b"code,description,end_year\nTribal,Tribal communities,\nAgricultural,Agricultural,2030\n", reason="Bulk"
    )
    assert out == {"added": 1, "changed": 1, "unchanged": 0}
    assert d.codes.count() == before + 1 and d.codes.get(code="Agricultural").end_year == 2030
    with pytest.raises(vls.ValueListError, match="Line 2"):
        vls.upload_codes(cpuc_admin, d, b"code|end_year\nX|soon\n")
    with pytest.raises(vls.ValueListError, match="code column"):
        vls.upload_codes(cpuc_admin, d, b"name\nX\n")
    ev = AuditEvent.objects.get(action="value_list.codes_uploaded")
    assert ev.after["added"] == ["Tribal"] and ev.after["changed"][0]["after"]["end_year"] == 2030


def test_discarding_a_draft(cpuc_admin, sector):
    d = vls.start_draft(cpuc_admin, sector)
    vls.discard_draft(cpuc_admin, d, "Not needed")
    assert not ValueListVersion.objects.exists() and AuditEvent.objects.filter(action="value_list.draft_discarded").exists()


def test_the_engine_checks_against_the_version_in_force(cpuc_admin, sector):
    from recet import editor

    assert "Agricultural" in editor.active_codes("sector", 2030)

    def edit(d):
        vls.add_code(cpuc_admin, d, "Tribal", start_year=2028)
        vls.change_code(cpuc_admin, d.codes.get(code="Agricultural"), end_year=2029)

    _published(cpuc_admin, sector, edit=edit)
    assert vls.apply_to_engine() == {"sector": 1}
    assert "Tribal" in editor.active_codes("sector", 2028) and "Tribal" not in editor.active_codes("sector", 2027)
    assert "Agricultural" not in editor.active_codes("sector", 2030) and "Agricultural" in editor.active_codes("sector", 2029)
    # a version dated later is not handed over until it takes effect
    assert vls.apply_to_engine(date(2000, 1, 1)) == {}
    assert "Tribal" not in editor.active_codes("sector", 2028)


def test_a_code_the_engine_ships_but_the_version_lacks_is_not_accepted(cpuc_admin, sector):
    from recet import editor

    def edit(d):
        Code.objects.filter(version=d, code="Agricultural").delete()  # as if a newer engine shipped it

    _published(cpuc_admin, sector, edit=edit)
    vls.apply_to_engine()
    assert "Agricultural" not in editor.active_codes("sector", 2025)


def test_the_rules_use_the_version_in_force(cpuc_admin, sector):
    from cedars.services.rules import service as rules

    rs = rules.import_from_engine(cpuc_admin, "claims")
    sector_rules = rs.rules.filter(kind="spec", definition__check="value_list", definition__list__iexact="sector")
    assert sector_rules.exists()
    from recet import templates

    row = {"CEInputID": "M1", "Sector": "Tribal", "ClaimYearQuarter": "2028Q1"}
    cols = templates.MEASURE_COLUMNS
    df = rules.read_table(("|".join(cols) + "\n" + "|".join(row.get(c, "") for c in cols) + "\n").encode())

    def sector_findings():
        found, _ = rules.check(rs, "Measure", df)
        return [f for f in found if f.rule_id in set(sector_rules.values_list("rule_id", flat=True))]

    assert sector_findings()
    _published(cpuc_admin, sector, edit=lambda d: vls.add_code(cpuc_admin, d, "Tribal", start_year=2028))
    assert not sector_findings()


def test_value_list_pages(client, signed_in, cpuc_admin, pge_admin, sector):
    assert "sector" in client.get("/dictionary/lists/").content.decode()
    page = client.get("/dictionary/lists/sector/").content.decode()
    assert "As shipped with the engine" in page and 'id="edit-h"' not in page
    csv_text = client.get("/dictionary/lists/sector/?format=csv&q=agri").content.decode()
    assert csv_text.startswith("code,description,start_year,end_year\n") and "\nAgricultural,Agricultural,2019,\n" in csv_text
    assert client.get("/dictionary/lists/sector/draft/").status_code == 302  # sign in first
    assert signed_in(pge_admin).get("/dictionary/lists/sector/draft/").status_code == 403

    c = signed_in(cpuc_admin)
    assert 'id="edit-h"' in c.get("/dictionary/lists/sector/").content.decode()
    assert c.post("/dictionary/lists/sector/new-version").status_code == 302
    page = c.get("/dictionary/lists/sector/draft/").content.decode()
    assert "Draft of sector, version 1" in page and "0 added" in page
    c.post(
        "/dictionary/lists/sector/draft/add", {"code": "Tribal", "description": "Tribal communities", "start_year": "2028", "reason": "New"}
    )
    draft = vls.draft_of(sector)
    ag = draft.codes.get(code="Agricultural")
    assert "start year stays" in c.get(f"/dictionary/lists/sector/draft/codes/{ag.pk}/").content.decode()
    r = c.post(f"/dictionary/lists/sector/draft/codes/{ag.pk}/", {"description": "Agricultural", "end_year": "2029", "reason": "Merged"})
    assert r.status_code == 302
    upload = SimpleUploadedFile("codes.csv", b"code,end_year\nIndustrial,2030\n")
    c.post("/dictionary/lists/sector/draft/upload", {"file": upload, "reason": "Bulk"})
    page = c.get("/dictionary/lists/sector/draft/").content.decode()
    assert "1 added, 2 retired" in page and "Added: Tribal" in page and "Retired: Agricultural, accepted up to 2029" in page
    tribal = draft.codes.get(code="Tribal")
    c.post(f"/dictionary/lists/sector/draft/codes/{tribal.pk}/drop", {"reason": "Not yet"})
    assert not draft.codes.filter(code="Tribal").exists()
    c.post("/dictionary/lists/sector/draft/publish", {"effective_from": TODAY.isoformat(), "reason": "2030 sectors"})
    page = client.get("/dictionary/lists/sector/").content.decode()
    assert "In force: version 1" in page and "2030 sectors" in page and "2 retired" in page
    assert "Version 1, from" in client.get("/dictionary/lists/").content.decode()
    assert client.get("/dictionary/lists/sector/?year=2031&state=active").content.decode().count("Agricultural") == 0


def test_the_edit_inputs_dropdowns_use_the_version_in_force(cpuc_admin, sector):
    from cedars.modules.calculations import drafts

    _published(cpuc_admin, sector, edit=lambda d: vls.add_code(cpuc_admin, d, "Tribal", start_year=2028))
    src = drafts.choices()
    from recet import editor

    assert src is not None and "Tribal" in editor.active_codes("sector", 2028)
