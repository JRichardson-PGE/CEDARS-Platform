"""Organization codes with their history (Jake, 2026-10-31: "AVA used to be EBCE ... keep the data traceable as the same party").

An organization keeps its identity through a rename; a former code still finds it; data keeps the code as filed and the dashboards,
record downloads, goals and legacy loads count it under the PA's current code; the same party loaded twice is merged."""

import importlib
import io
from datetime import date, timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone

from cedars.core import storage
from cedars.modules.data import dashboard, goal_view, records
from cedars.modules.data import query as dq
from cedars.modules.impact_profiles import access as profile_access
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation, OrganisationCode
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.legacy import tables as legacy_tables
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .test_data_query import results_file
from .test_snapshots import claims_deadline, confirm, filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db

FIRST_DAY = date(2024, 1, 1)


def _pa(admin, code, name=None):
    return access.create_organisation(admin, code=code, name=name or code, kind="pa")


@pytest.fixture
def ebce(cpuc_admin):
    return _pa(cpuc_admin, "EBCE", "East Bay Community Energy")


@pytest.fixture
def ava(cpuc_admin, ebce):
    """EBCE renamed to AVA from 2024, as CEDARS has it."""
    return access.rename_organisation(cpuc_admin, ebce, code="AVA", name="Ava Community Energy", effective=FIRST_DAY, reason="renamed")


# --- the history


def test_a_rename_keeps_the_organisation_and_its_old_code(cpuc_admin, ebce):
    pk = ebce.pk
    org = access.rename_organisation(cpuc_admin, ebce, code="ava", effective=FIRST_DAY, reason="CEDARS changelog 2025-02-27")
    assert org.pk == pk and org.code == "AVA" and org.name == "East Bay Community Energy"  # the name is kept unless given
    assert [(c.code, c.valid_from, c.valid_to, c.span) for c in access.code_history(org)] == [
        ("EBCE", None, FIRST_DAY, "until 2023-12-31"),
        ("AVA", FIRST_DAY, None, "from 2024-01-01"),
    ]
    assert access.by_code("EBCE") == org and access.by_code(" ebce ") == org and access.by_code("AVA") == org
    assert access.by_code("NOPE") is None and access.by_code("") is None
    assert access.code_on(org, date(2023, 6, 30)) == "EBCE" and access.code_on(org, date(2024, 1, 1)) == "AVA"
    assert access.code_map() == {"EBCE": "AVA"} and access.current_code("ebce") == "AVA" and access.current_code("PGE") == "PGE"
    assert access.party_codes(org) == ["AVA", "EBCE"]
    e = AuditEvent.objects.get(action="organisation.renamed", object_id=str(pk))
    assert e.before == {"code": "EBCE", "name": "East Bay Community Energy"}
    assert e.after == {"code": "AVA", "name": "East Bay Community Energy", "former_code": "EBCE", "effective": "2024-01-01"}
    assert e.reason == "CEDARS changelog 2025-02-27" and e.actor == cpuc_admin


def test_renames_are_refused_without_the_right_or_a_good_code(cpuc_admin, pge, pge_admin, ebce):
    rename = access.rename_organisation
    with pytest.raises(AccessDenied, match="system administrator"):
        rename(pge_admin, ebce, code="AVA", reason="x")
    with pytest.raises(AccessDenied, match="reason"):
        rename(cpuc_admin, ebce, code="AVA", reason=" ")
    with pytest.raises(AccessDenied, match="PGE is its current code"):
        rename(cpuc_admin, ebce, code="pge", reason="x")
    with pytest.raises(AccessDenied, match="1 to 20 letters"):
        rename(cpuc_admin, ebce, code="A V A", reason="x")
    with pytest.raises(AccessDenied, match="already its code"):
        rename(cpuc_admin, ebce, code="EBCE", reason="x")
    with pytest.raises(AccessDenied, match="future"):
        rename(cpuc_admin, ebce, code="AVA", reason="x", effective=timezone.localdate() + timedelta(days=1))
    rename(cpuc_admin, ebce, code="AVA", reason="x", effective=FIRST_DAY)
    with pytest.raises(AccessDenied, match="after 2024-01-01"):
        rename(cpuc_admin, ebce, code="AVA3", reason="x", effective=FIRST_DAY)
    # a former code stays the organisation's for good: no one else may take it, here or when adding an organisation
    with pytest.raises(AccessDenied, match="EBCE is a former code of theirs: AVA"):
        rename(cpuc_admin, pge, code="EBCE", reason="x")
    with pytest.raises(AccessDenied, match="former code"):
        _pa(cpuc_admin, "EBCE")
    # renaming back to its own former code is allowed
    rename(cpuc_admin, ebce, code="EBCE", reason="back", effective=date(2025, 1, 1))
    assert [c.code for c in access.code_history(ebce)] == ["EBCE", "AVA", "EBCE"]


def test_the_database_keeps_a_code_with_one_organisation_and_one_code_a_day(cpuc_admin, pge, ava):
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganisationCode.objects.create(organisation=pge, code="EBCE", valid_to=date(2020, 1, 1))
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganisationCode.objects.create(organisation=ava, code="OTHER", valid_from=date(2022, 1, 1), valid_to=date(2025, 1, 1))
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganisationCode.objects.create(organisation=pge, code="lower", valid_to=date(2020, 1, 1))


def test_a_former_code_recorded_on_the_organisation_that_has_the_new_one(cpuc_admin):
    ava = _pa(cpuc_admin, "AVA", "Ava Community Energy")
    access.add_former_code(cpuc_admin, ava, code="EBCE", until=FIRST_DAY, reason="CEDARS changelog")
    assert [(c.code, c.span) for c in access.code_history(ava)] == [("EBCE", "until 2023-12-31"), ("AVA", "from 2024-01-01")]
    assert access.by_code("EBCE") == ava
    assert AuditEvent.objects.filter(action="organisation.code_added", object_id=str(ava.pk)).count() == 1
    with pytest.raises(AccessDenied, match="overlap"):
        access.add_former_code(cpuc_admin, ava, code="OLD", until=date(2020, 1, 1), reason="x")
    with pytest.raises(AccessDenied, match="overlap"):  # EBCE was in use from the start
        access.add_former_code(cpuc_admin, ava, code="OLDER", until=date(2020, 1, 1), since=date(2018, 1, 1), reason="x")
    with pytest.raises(AccessDenied, match="must end by then"):
        access.add_former_code(cpuc_admin, ava, code="LATE", until=date(2025, 1, 1), reason="x")


def test_the_same_party_loaded_twice_is_merged(cpuc_admin, make_user):
    ebce, ava = _pa(cpuc_admin, "EBCE"), _pa(cpuc_admin, "AVA", "Ava Community Energy")
    member = make_user("someone@ebce.example", "Some One")
    access.ensure_membership(cpuc_admin, member, ebce)
    with pytest.raises(AccessDenied, match="still has members"):
        access.merge_organisation(cpuc_admin, ebce, into=ava, effective=FIRST_DAY, reason="same party")
    access.end_membership(None, ebce.memberships.get(), "moved to AVA")
    with pytest.raises(AccessDenied, match="itself"):
        access.merge_organisation(cpuc_admin, ava, into=ava, reason="x")
    access.merge_organisation(cpuc_admin, ebce, into=ava, effective=FIRST_DAY, reason="same party")
    ebce.refresh_from_db()
    assert ebce.successor == ava and ebce.merged_on == FIRST_DAY and not ebce.active and ebce.code == "EBCE"
    assert access.by_code("EBCE") == ava and access.by_code("EBCE", follow=False) == ebce
    assert access.party_members(ava) == [ava, ebce] and access.party_codes(ava) == ["AVA", "EBCE"]
    assert [(c.code, c.span) for c in access.code_history(ava)] == [("EBCE", "until 2023-12-31"), ("AVA", "from 2024-01-01")]
    assert access.code_map() == {"EBCE": "AVA"}
    assert AuditEvent.objects.filter(action="organisation.merged").count() == 2  # one on each side
    with pytest.raises(AccessDenied, match="already merged"):
        access.merge_organisation(cpuc_admin, ebce, into=ava, reason="x")
    with pytest.raises(AccessDenied, match="merged into AVA"):
        access.rename_organisation(cpuc_admin, ebce, code="X", reason="x")


# --- filed codes: legacy loads and importers find the one organisation


def test_a_known_rename_finds_one_organisation_whichever_code_comes_first(cpuc_admin):
    org = access.organisation_for_filed_code("EBCE")  # neither here: made under the new code, the old one recorded
    assert (org.code, org.name, org.pa_type) == ("AVA", "Ava Community Energy", "CCA")
    assert access.organisation_for_filed_code("AVA") == org and access.organisation_for_filed_code("ebce") == org
    assert [(c.code, c.span) for c in access.code_history(org)] == [("EBCE", "until 2023-12-31"), ("AVA", "from 2024-01-01")]
    assert access.organisation_for_filed_code("ZZZ").code == "ZZZ"  # any other code: a new PA under it


def test_a_known_rename_applied_to_the_old_or_the_new_organisation(cpuc_admin):
    old = _pa(cpuc_admin, "EBCE")  # only the old one here: a load under the new code renames it
    assert access.organisation_for_filed_code("AVA") == old
    old.refresh_from_db()
    assert old.code == "AVA" and old.name == "Ava Community Energy"  # it had only its code for a name
    assert access.organisation_for_filed_code("EBCE") == old  # and its old code now finds it


def test_legacy_files_under_the_old_code_load_under_the_renamed_pa(cpuc_admin, ava):
    assert legacy_tables.organisation("EBCE") == ava and legacy_tables.organisation("AVA") == ava


def test_the_migration_applies_known_renames_and_audits_each(cpuc_admin, make_user):
    migration = importlib.import_module("cedars.services.accounts.migrations.0004_organisation_codes")
    assert migration.KNOWN_RENAMES == access.KNOWN_RENAMES  # the copy matches the service's table as it stands
    # as before the migration: organisations with no code history
    old = Organisation.objects.create(code="EBCE", name="EBCE", kind="pa", pa_type="CCA")
    plain = Organisation.objects.create(code="SCP", name="Sonoma Clean Power", kind="pa", pa_type="CCA")
    migration.record_codes(apps, None)
    old.refresh_from_db()
    assert old.code == "AVA" and old.name == "Ava Community Energy"
    assert [(c.code, c.span) for c in access.code_history(old)] == [("EBCE", "until 2023-12-31"), ("AVA", "from 2024-01-01")]
    assert [c.code for c in access.code_history(plain)] == ["SCP"] and plain.codes.get().valid_from is None
    e = AuditEvent.objects.get(action="organisation.renamed", object_id=str(old.pk))
    assert e.actor_label == "system" and "CEDARS changelog" in e.reason
    migration.record_codes(apps, None)  # a second run changes nothing
    assert OrganisationCode.objects.filter(organisation=old).count() == 2


def test_the_migration_merges_both_or_leaves_them_when_the_old_one_has_members(cpuc_admin, make_user):
    migration = importlib.import_module("cedars.services.accounts.migrations.0004_organisation_codes")
    old = Organisation.objects.create(code="EBCE", name="EBCE", kind="pa", pa_type="CCA")
    new = Organisation.objects.create(code="AVA", name="Ava Community Energy", kind="pa", pa_type="CCA")
    access.ensure_membership(cpuc_admin, make_user("m@ebce.example", "M"), old)
    migration.record_codes(apps, None)
    old.refresh_from_db()
    assert old.successor is None and access.code_map() == {}  # left for a system administrator
    access.end_membership(None, old.memberships.get(), "moved")
    migration.record_codes(apps, None)
    old.refresh_from_db()
    assert old.successor == new and not old.active and access.code_map() == {"EBCE": "AVA"}
    assert [(c.code, c.span) for c in access.code_history(new)] == [("EBCE", "until 2023-12-31"), ("AVA", "from 2024-01-01")]


# --- the dashboards and record downloads count a former code under the PA


@pytest.fixture
def filed(filer, cpuc_admin, pge, ebce, django_capture_on_commit_callbacks):  # noqa: F811
    """The test records (TSB 100 for PGE's three, 50 and 30 for the other two) with one credited to EBCE and one to AVA, published
    while the platform still knew only EBCE."""
    table = pq.read_table(io.BytesIO(results_file()))
    table = table.set_column(table.schema.get_field_index("CreditedPA"), "CreditedPA", pa.array(["PGE", "EBCE", "AVA", "PGE", "PGE"]))
    buf = io.BytesIO()
    pq.write_table(table, buf)
    v = submissions.start(filer, claims_deadline(cpuc_admin, 1), pge)
    submissions.record_validation(filer, v, None, {"records": 5, "rejected": 0, "warnings": 0})
    key = "runs/codes/out/_results/measure_level_run.parquet"
    storage.store("results").put_bytes(key, buf.getvalue())
    submissions.record_calculation(None, v, run="codes", input_hash="h", details={"results": {"measure_level": key}})
    return confirm(filer, SubmissionVersion.objects.get(pk=v.pk), django_capture_on_commit_callbacks)


def test_the_dashboards_count_a_former_code_under_the_pa(filed, cpuc_admin, ebce):
    q = dq.Query("claims", ("tsb",), by=("pa",))
    by_pa = lambda query: {r["pa"]: r["tsb"] for r in dq.run(query).rows}  # noqa: E731
    assert by_pa(q) == {"PGE": 100, "EBCE": 50, "AVA": 30}  # before the rename: two PAs
    assert dq.pa_expression({}) == dq.DIMENSIONS["pa"].expression  # no renames: the PA as filed
    access.rename_organisation(cpuc_admin, ebce, code="AVA", effective=FIRST_DAY, reason="renamed")
    assert by_pa(q) == {"PGE": 100, "AVA": 80}  # counted at once, nothing republished
    assert by_pa(q.but(filters=(("pa", ("EBCE",)),))) == {"AVA": 80}  # an old link's code selects the PA
    assert by_pa(q.but(filters=(("pa", ("AVA",)),))) == {"AVA": 80}
    assert {r["pa_type"]: r["tsb"] for r in dq.run(q.but(by=("pa_type",))).rows} == {"IOU": 100, "CCA": 80}
    assert dq.selected_pas(q.but(filters=(("pa", ("ebce",)),)), {}) == {"AVA"}
    groups = dashboard.pa_groups({"pa": ("EBCE",)})
    ccas = next(g for g in groups if g["type"] == "CCA")
    assert ccas["pas"] == [{"code": "AVA", "checked": True, "former": ["EBCE"]}]


def test_record_downloads_keep_the_code_as_filed_beside_the_counted_pa(filed, cpuc_admin, ebce):
    access.rename_organisation(cpuc_admin, ebce, code="AVA", effective=FIRST_DAY, reason="renamed")
    rq = records.from_params({"dataset": ["claims"], "pa": ["EBCE"], "columns": ["none"], "col": ["CreditedPA", "CountedPA"]})
    page = records.page(rq)
    rows = sorted((r[page.columns.index("CreditedPA")], r[page.columns.index("CountedPA")]) for r in page.rows)
    assert rows == [("AVA", "AVA"), ("EBCE", "AVA")]
    assert "CountedPA" in records.KEY_COLUMNS


def test_a_merged_organisations_filings_count_under_its_successor(filed, cpuc_admin):
    ava = _pa(cpuc_admin, "AVA", "Ava Community Energy")
    assert {f["organisation"] for f in dq.files_of(dq.Query("claims", ("tsb",)))} == {"PGE"}
    ebce = Organisation.objects.get(code="EBCE")
    access.merge_organisation(cpuc_admin, ebce, into=ava, effective=FIRST_DAY, reason="same party")
    q = dq.Query("claims", ("tsb",), by=("pa",))
    assert {r["pa"]: r["tsb"] for r in dq.run(q).rows} == {"PGE": 100, "AVA": 80}
    assert all(g["type"] != "CCA" or [p["code"] for p in g["pas"]] == ["AVA"] for g in dashboard.pa_groups({}))


def test_a_goal_rule_written_for_a_former_code_counts_for_the_pa(cpuc_admin, ava):
    rule = lambda pa, t, share: SimpleNamespace(pa=pa, territory=t, share=share, applies_to="all")  # noqa: E731
    shares = goal_view.shares([rule("EBCE", "PGE", 1), rule("MCE", "PGE", 1)], "programs")
    assert shares["AVA"] == [("PGE", 1.0)] and "EBCE" not in shares and shares["MCE"] == [("PGE", 1.0)]
    # the PA's own rule wins over its former code's for the same territory
    shares = goal_view.shares([rule("EBCE", "PGE", 1), rule("AVA", "PGE", 0.5)], "programs")
    assert shares["AVA"] == [("PGE", 0.5)]


def test_load_shapes_led_under_the_old_code_belong_to_the_renamed_pa(cpuc_admin, ava):
    package = SimpleNamespace(pa_lead="EBCE", measure_version_id="SWXX001-01")
    profile_access.require_package_lead(None, ava, package)  # the system itself
    assert profile_access.sees_organisation(cpuc_admin, "EBCE")


# --- the pages


def test_the_organisation_page_shows_the_codes_and_takes_changes(signed_in, cpuc_admin, pge, pge_admin, ebce):
    c = signed_in(cpuc_admin)
    r = c.post(
        f"/admin/organisations/{ebce.code}/rename",
        {"code": "AVA", "name": "Ava Community Energy", "effective": "2024-01-01", "reason": "CEDARS changelog"},
    )
    assert r.status_code == 302 and r["Location"] == "/admin/organisations/AVA/"
    assert c.get("/admin/organisations/EBCE/")["Location"] == "/admin/organisations/AVA/"  # an old link still leads to it
    page = c.get("/admin/organisations/AVA/").content.decode()
    assert '<h2 id="codes-h">Codes</h2>' in page and "until 2023-12-31" in page and "from 2024-01-01" in page
    assert "Rename: give AVA a new code" in page and "Merge in an organization recorded under an old code" in page
    assert "formerly EBCE" in c.get("/admin/organisations/").content.decode()
    r = c.post("/admin/organisations/AVA/former-code", {"code": "EBCE2", "until": "2024-01-01", "reason": "x"})
    assert r.status_code == 302 and not OrganisationCode.objects.filter(code="EBCE2").exists()  # overlaps EBCE: refused
    twice = _pa(cpuc_admin, "AVA2")
    r = c.post("/admin/organisations/AVA/merge", {"source": twice.pk, "effective": "2025-01-01", "reason": "same party"})
    assert r.status_code == 302
    twice.refresh_from_db()
    assert twice.successor.code == "AVA"
    merged = c.get("/admin/organisations/AVA2/").content.decode()
    assert "is the same party as" in merged and "Rename: give" not in merged  # no further changes on a merged organization
    pa = signed_in(pge_admin)
    assert "Rename: give" not in pa.get(f"/admin/organisations/{pge.code}/").content.decode()
    assert pa.post(f"/admin/organisations/{pge.code}/rename", {"code": "X", "effective": "2024-01-01", "reason": "x"}).status_code == 404


def test_cedars_programs_list_puts_ebce_and_ava_programs_under_one_pa(cpuc_admin):
    from cedars.services.registry.importers import import_cedars_programs
    from cedars.services.registry.models import Program

    from .test_registry import DATA

    import_cedars_programs((DATA / "cedars_programs_20261003.csv").read_bytes(), actor=cpuc_admin)
    ava = Program.objects.get(prg_id="AVA01").administrator
    assert Program.objects.get(prg_id="EBCE01").administrator == ava and ava.code == "AVA"
    assert not Organisation.objects.filter(code="EBCE").exists() and access.by_code("EBCE") == ava
