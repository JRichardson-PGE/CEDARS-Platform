"""PA types (Jake, 2026-10-31): every program administrator is an IOU, a REN or a CCA; the dashboards and record downloads select PAs by
type as well as one by one, and can view results by PA type."""

import importlib
import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.apps import apps
from django.http import QueryDict

from cedars.core import storage
from cedars.modules.data import budget_view, dashboard
from cedars.modules.data import query as dq
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .test_data_query import results_file
from .test_snapshots import claims_deadline, confirm, filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db

# The test records (test_data_query.RECORDS) credited to an IOU, a REN and a CCA: TSB 100, 50 and 30 (the last two rows, C&S and
# Pension and Benefits, are left out by default).
CREDITED = ["PGE", "BAY", "MCE", "PGE", "PGE"]


@pytest.fixture
def mixed(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    access.create_organisation(cpuc_admin, code="BAY", name="Bay Area Regional Energy Network", kind="pa")
    access.create_organisation(cpuc_admin, code="MCE", name="MCE", kind="pa")
    access.create_organisation(cpuc_admin, code="NEWPA", name="A new PA", kind="pa")  # a code CEDARS has not used: no type yet
    table = pq.read_table(io.BytesIO(results_file()))
    table = table.set_column(table.schema.get_field_index("CreditedPA"), "CreditedPA", pa.array(CREDITED))
    buf = io.BytesIO()
    pq.write_table(table, buf)
    v = submissions.start(filer, claims_deadline(cpuc_admin, 1), pge)
    submissions.record_validation(filer, v, None, {"records": 5, "rejected": 0, "warnings": 0})
    key = "runs/mixed/out/_results/measure_level_run.parquet"
    storage.store("results").put_bytes(key, buf.getvalue())
    submissions.record_calculation(None, v, run="mixed", input_hash="h", details={"results": {"measure_level": key}})
    return confirm(filer, SubmissionVersion.objects.get(pk=v.pk), django_capture_on_commit_callbacks)


def _qd(**params):
    q = QueryDict(mutable=True)
    for k, v in params.items():
        q.setlist(k, v if isinstance(v, list) else [v])
    return q


def tsb_by(q: dq.Query, by: str) -> dict:
    return {r[by]: r["tsb"] for r in dq.run(q.but(by=(by,))).rows}


# --- the PA type on each organization


def test_cedars_codes_are_typed_when_added(cpuc_admin):
    assert access.known_pa_type("pge") == "IOU" and access.known_pa_type("BAY") == "REN" and access.known_pa_type("MCE") == "CCA"
    assert access.known_pa_type("NOPE") == ""
    assert access.create_organisation(cpuc_admin, code="scr", name="SoCalREN", kind="pa").pa_type == "REN"
    assert access.create_organisation(cpuc_admin, code="NEWPA", name="New", kind="pa").pa_type == ""
    assert access.create_organisation(cpuc_admin, code="SJCE", name="San Jose", kind="pa", pa_type="REN").pa_type == "REN"  # as given
    assert access.create_organisation(cpuc_admin, code="EVAL", name="An evaluator", kind="other").pa_type == ""
    assert set(access.KNOWN_PA_TYPES.values()) == set(Organisation.PAType.values)


def test_the_migration_types_existing_pas_and_audits_each(cpuc_admin):
    blank = [Organisation.objects.create(code=c, name=c, kind="pa") for c in ("TCR", "PCE", "SDGE", "NEWPA")]
    typed = Organisation.objects.create(code="RRN", name="RRN", kind="pa", pa_type="CCA")  # set by hand: left alone
    migration = importlib.import_module("cedars.services.accounts.migrations.0003_pa_types")
    assert migration.KNOWN_PA_TYPES == access.KNOWN_PA_TYPES  # the copy matches the service's table as it stands
    migration.type_pas(apps, None)
    assert {o.code: o.pa_type for o in Organisation.objects.filter(pk__in=[o.pk for o in [*blank, typed]])} == {
        "TCR": "REN",
        "PCE": "CCA",
        "SDGE": "IOU",
        "NEWPA": "",
        "RRN": "CCA",
    }
    events = AuditEvent.objects.filter(action="organisation.changed")
    assert sorted(e.after["pa_type"] for e in events) == ["CCA", "IOU", "REN"]
    assert all(e.before["pa_type"] == "" and "migration accounts/0003_pa_types" in e.reason for e in events)


def test_a_system_admin_changes_the_pa_type_with_a_reason(cpuc_admin, pge, pge_admin):
    new = access.create_organisation(cpuc_admin, code="NEWPA", name="New", kind="pa")
    with pytest.raises(access.AccessDenied, match="reason"):
        access.change_organisation(cpuc_admin, new, pa_type="REN", reason=" ")
    with pytest.raises(access.AccessDenied, match="system administrator"):
        access.change_organisation(pge_admin, new, pa_type="REN", reason="new REN")
    with pytest.raises(access.AccessDenied, match="needs a PA type"):
        access.change_organisation(cpuc_admin, new, pa_type="", reason="oops")
    with pytest.raises(access.AccessDenied, match="Only a program administrator"):
        access.change_organisation(cpuc_admin, access.cpuc(), pa_type="IOU", reason="oops")
    access.change_organisation(cpuc_admin, new, pa_type="REN", name="New Regional Energy Network", reason="D.26-01-001 approved it")
    new.refresh_from_db()
    assert (new.pa_type, new.name) == ("REN", "New Regional Energy Network")
    [e] = AuditEvent.objects.filter(action="organisation.changed", object_id=str(new.pk))
    assert (
        e.before == {"name": "New", "pa_type": "", "manages_allocation": False}
        and e.after["pa_type"] == "REN"
        and e.reason == "D.26-01-001 approved it"
    )
    access.change_organisation(cpuc_admin, new, pa_type="REN", reason="no change")
    assert AuditEvent.objects.filter(action="organisation.changed", object_id=str(new.pk)).count() == 1  # nothing changed, nothing recorded


def test_the_organisation_pages(signed_in, cpuc_admin, pge, pge_admin):
    access.create_organisation(cpuc_admin, code="NEWPA", name="New", kind="pa")
    c = signed_in(cpuc_admin)
    listing = c.get("/admin/organizations/").content.decode()
    assert '<th scope="col">PA type</th>' in listing and "Not set" in listing
    page = c.get("/admin/organizations/NEWPA/").content.decode()
    assert "PA type" in page and "Not set" in page and "Change the name, PA type or statewide allocations" in page
    r = c.post("/admin/organizations/NEWPA/details", {"name": "New", "pa_type": "CCA", "reason": "a CCA"})
    assert r.status_code == 302 and Organisation.objects.get(code="NEWPA").pa_type == "CCA"
    c.post("/admin/organizations/NEWPA/details", {"name": "New", "pa_type": "", "reason": "blank"})
    assert Organisation.objects.get(code="NEWPA").pa_type == "CCA"  # refused: a PA needs a type
    # adding a PA asks for its type; another kind of organization has none
    r = c.post("/admin/organizations/new/", {"code": "XYZ", "name": "XYZ", "kind": "pa", "pa_type": ""})
    assert r.status_code == 200 and "Choose the program administrator" in r.content.decode()
    r = c.post("/admin/organizations/new/", {"code": "EVAL", "name": "Evaluator", "kind": "other", "pa_type": "IOU"})
    assert r.status_code == 200 and "Only a program administrator has a PA type" in r.content.decode()
    pa = signed_in(pge_admin)  # the same client, now signed in as PG&E's administrator: no details form, no changes
    assert "Change the name, PA type or statewide allocations" not in pa.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert pa.post("/admin/organizations/NEWPA/details", {"name": "x", "pa_type": "IOU", "reason": "x"}).status_code == 404


# --- grouped selection on the dashboards and record downloads


def test_query_by_and_filtered_on_pa_type(mixed):
    q = dq.Query("claims", ("tsb",))
    assert tsb_by(q, "pa_type") == {"IOU": 100, "REN": 50, "CCA": 30}
    assert dq.run(q.but(filters=(("pa_type", ("REN",)),))).totals["tsb"] == 50
    assert dq.run(q.but(filters=(("pa_type", ("ren", "CCA")),))).totals["tsb"] == 80  # case as typed
    # a group and a single PA add up: all RENs and MCE, not RENs that are also MCE
    assert dq.run(q.but(filters=(("pa_type", ("REN",)), ("pa", ("MCE",))))).totals["tsb"] == 80
    # other filters still narrow the selection
    assert dq.run(q.but(filters=(("pa_type", ("REN", "IOU")), ("sector", ("Residential",))))).totals["tsb"] == 150
    assert dq.run(q.but(filters=(("pa_type", ("CCA",)), ("sector", ("Residential",))))).rows == []
    assert dq.from_params(_qd(pa_type="REN", pa="MCE")).filter_map == {"pa_type": ("REN",), "pa": ("MCE",)}


def test_a_pa_typed_later_reads_as_typed(mixed, cpuc_admin):
    q = dq.Query("claims", ("tsb",), by=("pa_type",))
    bay = Organisation.objects.get(code="BAY")
    Organisation.objects.filter(pk=bay.pk).update(pa_type="")  # as if never typed
    assert {r["pa_type"]: r["tsb"] for r in dq.run(q).rows} == {"IOU": 100, "CCA": 30, None: 50}
    access.change_organisation(cpuc_admin, Organisation.objects.get(pk=bay.pk), pa_type="REN", reason="typed")
    assert {r["pa_type"]: r["tsb"] for r in dq.run(q).rows} == {"IOU": 100, "REN": 50, "CCA": 30}
    assert dq.pa_type_expression({}) == "CAST(NULL AS VARCHAR)"
    assert dq.pa_type_expression({"o'k": "REN"}).count("'O''K'") == 1  # codes are quoted as SQL literals


def test_dashboard_views_by_pa_type_and_drills_to_its_pas(mixed):
    overview = dashboard.PAGES["overview"]
    v = dashboard.parse(overview, _qd(by="pa_type", series="claims"))
    c = dashboard.compare(v)
    assert [(r.key, r.values["claims"]) for r in c.rows] == [("IOU", 100), ("REN", 50), ("CCA", 30)]
    assert c.rows[1].drill == "?series=claims&by=pa&pa_type=REN"
    v = dashboard.parse(overview, _qd(by="pa", series="claims", pa_type="REN", pa="MCE"))
    assert {r.key: r.values["claims"] for r in dashboard.compare(v).rows} == {"BAY": 50, "MCE": 30}
    chips = dashboard.applied_filters(v)
    assert [(ch["label"], ch["value"]) for ch in chips] == [("PA type", "REN"), ("PA", "MCE")]
    assert chips[0]["remove"] == "?series=claims&by=pa&pa=MCE"


def test_pa_filter_choices_are_grouped_by_type(mixed):
    groups = dashboard.pa_groups({"pa_type": ("REN",), "pa": ("MCE",)})
    assert [(g["label"], g["type"], g["all"], [(p["code"], p["checked"]) for p in g["pas"]]) for g in groups] == [
        ("IOUs", "IOU", False, [("PGE", False)]),
        ("RENs", "REN", True, [("BAY", False)]),
        ("CCAs", "CCA", False, [("MCE", True)]),
        ("No PA type", "", False, [("NEWPA", False)]),
    ]


def test_the_pages_offer_whole_groups(mixed, client):
    page = client.get("/data/overview/?pa_type=REN&pa=MCE&series=claims").content.decode()
    assert '<input type="checkbox" name="pa_type" value="REN" checked> All RENs' in page
    assert '<input type="checkbox" name="pa_type" value="IOU"> All IOUs' in page
    assert '<input type="checkbox" name="pa" value="MCE" checked> MCE' in page
    assert "No PA type" in page and "by PA</h2>" in page and '<option value="pa_type">PA type</option>' in page
    assert "PA type: <strong>REN</strong>" in page
    records = client.get("/data/records/?pa_type=CCA&metric=tsb").content.decode()
    assert '<input type="checkbox" name="pa_type" value="CCA" checked> All CCAs' in records
    assert 'type="hidden" name="pa_type"' not in records  # in the form itself, not carried as a hidden field


def test_spending_reads_every_pa_of_the_chosen_types(mixed):
    pick = lambda **p: [o.code for o in budget_view.organisations(dashboard.parse(dashboard.PAGES["budget"], _qd(**p)))]  # noqa: E731
    assert pick() == ["BAY", "MCE", "NEWPA", "PGE"]
    assert pick(pa_type="REN") == ["BAY"]
    assert pick(pa_type=["REN", "IOU"], pa="mce") == ["BAY", "MCE", "PGE"]
    assert pick(pa="PGE") == ["PGE"]
