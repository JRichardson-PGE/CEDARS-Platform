import csv
import io
from datetime import date
from pathlib import Path

import pytest
from django.db import IntegrityError, transaction

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service
from cedars.services.registry.importers import import_cedars_programs, parse_sectors
from cedars.services.registry.models import AllocationVersion, Program, ProgramVersion, ReportingPeriod

pytestmark = pytest.mark.django_db
DATA = Path(__file__).parent / "data"


@pytest.fixture
def program(pge, pge_admin):
    return service.create_program(
        pge_admin,
        prg_id="PGE_Res_HVAC",
        administrator=pge,
        valid_from=date(2024, 1, 1),
        name="Residential HVAC",
        segment="resource_acquisition",
        sectors={"Residential": 1.0},
    )


# --- effective-dated versions (BR-13.3, BR-12.7)


def test_a_change_starts_a_new_version_and_keeps_the_old(program, pge_admin):
    service.change_program(
        pge_admin, program, effective=date(2026, 7, 1), reason="resolution D.26-05-001", implementer="Third-Party Solicited"
    )
    old, new = program.versions.order_by("valid_from")
    assert (old.valid_from, old.valid_to, old.implementer) == (date(2024, 1, 1), date(2026, 6, 30), "")
    assert (new.valid_from, new.valid_to, new.implementer, new.name) == (
        date(2026, 7, 1),
        None,
        "Third-Party Solicited",
        "Residential HVAC",
    )
    assert service.version_on(program, date(2026, 6, 30)) == old
    assert service.version_on(program, date(2026, 7, 1)) == new
    assert service.version_for_year(program, 2025) == old
    assert service.version_for_year(program, 2026) == new  # the version in force at the year's end
    hist = service.history(program)
    assert hist[1][1] == [("implementer", "", "Third-Party Solicited")]
    assert AuditEvent.objects.filter(action="program.changed", object_id=str(program.pk)).get().reason == "resolution D.26-05-001"


def test_future_dated_changes_are_allowed_and_history_is_not(program, pge_admin):
    service.change_program(pge_admin, program, effective=date(2030, 1, 1), reason="entered ahead of its effective date", status="closed")
    assert service.current_version(program).status == ""
    assert service.version_for_year(program, 2030).status == "closed"
    with pytest.raises(AccessDenied, match="must start after"):
        service.change_program(pge_admin, program, effective=date(2025, 1, 1), reason="rewrite history", name="X")


def test_versions_cannot_overlap_in_the_database(program):
    with pytest.raises(IntegrityError), transaction.atomic():
        ProgramVersion.objects.create(program=program, valid_from=date(2025, 1, 1), name="overlap")


def test_only_the_programs_administrator_changes_it(program, cpuc_admin, make_user):
    with pytest.raises(AccessDenied, match="PGE's administrators"):
        service.change_program(make_user("x@sce.example.org"), program, effective=date(2027, 1, 1), reason="r", name="X")
    service.change_program(cpuc_admin, program, effective=date(2027, 1, 1), reason="CPUC correction", name="Residential HVAC Program")
    with pytest.raises(AccessDenied, match="reason"):
        service.change_program(cpuc_admin, program, effective=date(2028, 1, 1), reason="", name="Y")


def test_catalog_hides_portfolio_costs_and_keeps_closed_programs(program, pge, pge_admin):
    service.create_program(
        pge_admin, prg_id="PGE_Portfolio_EMV", administrator=pge, valid_from=date(2024, 1, 1), name="EM&V", portfolio_cost=True
    )
    closed = service.create_program(
        pge_admin, prg_id="PGE_Old", administrator=pge, valid_from=date(2015, 1, 1), name="Old", status="closed"
    )
    ids = [p.prg_id for p, _ in service.catalog()]
    assert "PGE_Portfolio_EMV" not in ids and closed.prg_id in ids


# --- cycles and periods


def test_cycles_and_periods():
    cycle = service.ensure_cycle(2028)
    assert str(cycle) == "2028-2031" and cycle.covers(2031) and not cycle.covers(2032)
    periods = service.ensure_periods(2028)
    assert len(periods) == 17
    q4 = ReportingPeriod.objects.get(kind="quarter", year=2028, number=4)
    feb = ReportingPeriod.objects.get(kind="month", year=2028, number=2)
    assert (q4.starts, q4.ends, str(q4)) == (date(2028, 10, 1), date(2028, 12, 31), "2028Q4")
    assert feb.ends == date(2028, 2, 29)


# --- importing CEDARS' Programs list


def test_import_cedars_programs(cpuc_admin):
    counts = import_cedars_programs((DATA / "cedars_programs_20261003.csv").read_bytes())
    assert counts["added"] == 1416 and counts["skipped"] == 0
    assert Program.objects.filter(statewide=True, administrator__isnull=True).count() == 33
    p = Program.objects.get(prg_id="AVA01")
    v = p.versions.get()
    assert (p.administrator.code, v.valid_from, v.sectors) == ("AVA", date(2023, 1, 1), {"Commercial": 0.9, "Industrial": 0.1})
    assert access.cpuc() is not None and p.administrator.kind == "pa"
    assert import_cedars_programs((DATA / "cedars_programs_20261003.csv").read_bytes())["added"] == 0  # idempotent


def test_parse_sectors():
    assert parse_sectors("") == {}
    assert parse_sectors("Residential: 1.000000") == {"Residential": 1.0}


# --- statewide allocation versions


def _factors(edit=None) -> bytes:
    text = (DATA / "statewide_factors_20260918.csv").read_text(encoding="utf-8-sig")
    if edit is None:
        return text.encode()
    rows = list(csv.DictReader(io.StringIO(text)))
    edit(rows)
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().encode()


def test_allocation_version_import_check_confirm(cpuc_admin):
    v = service.import_allocation_csv(
        cpuc_admin,
        _factors(),
        label="2028 TUAL",
        purpose="filing",
        filing_cycle=service.ensure_cycle(2028),
        source_name="statewide_factors_20260918.csv",
    )
    assert v.status == "draft" and v.entries.count() == 304
    blocking = [i for i in service.check_allocation(v) if i.blocking]
    assert blocking == []
    assert service.most_recent_allocation(2028) is None  # drafts are never used by a run
    service.confirm_allocation(cpuc_admin, v)
    years = sorted(set(v.entries.values_list("year", flat=True)))
    assert service.most_recent_allocation(years[-1]) == v
    with pytest.raises(AccessDenied, match="already confirmed"):
        service.confirm_allocation(cpuc_admin, v)
    with pytest.raises(AccessDenied, match="kept for good"):
        service.discard_draft(cpuc_admin, v)


def test_allocation_round_trips_to_recets_layout(cpuc_admin):
    original = _factors()
    v = service.import_allocation_csv(cpuc_admin, original, label="round trip", purpose="claims")
    back = {(r["Year"], r["ProgramID"]): r for r in csv.DictReader(io.StringIO(service.allocation_csv(v)))}
    src = list(csv.DictReader(io.StringIO(original.decode())))
    assert len(back) == len(src)
    for a in src:
        b = back[(a["Year"], a["ProgramID"].strip())]
        for k in a:
            if k in ("Summary",):
                continue
            if k in ("Year", "ProgramID", "ProgramName"):
                assert a[k].strip() == b[k], k
            else:
                assert float(a[k] or 0) == pytest.approx(float(b[k])), (a["ProgramID"], k)


def test_a_newer_confirmed_version_takes_over_and_the_old_stays(cpuc_admin):
    first = service.confirm_allocation(cpuc_admin, service.import_allocation_csv(cpuc_admin, _factors(), label="filing", purpose="filing"))
    second = service.confirm_allocation(
        cpuc_admin, service.import_allocation_csv(cpuc_admin, _factors(), label="claims revision", purpose="claims")
    )
    year = first.entries.first().year
    assert service.most_recent_allocation(year) == second
    assert AllocationVersion.objects.get(pk=first.pk).status == "confirmed"


def test_factors_that_do_not_add_up_block_confirmation(cpuc_admin):
    def break_first(rows):
        rows[0]["PGEElectricFactor"] = "0.9000"

    v = service.import_allocation_csv(cpuc_admin, _factors(break_first), label="broken", purpose="filing")
    issues = service.check_allocation(v)
    assert any(i.blocking and "electric factors add up to" in i.message for i in issues)
    with pytest.raises(AccessDenied, match="problems to fix first"):
        service.confirm_allocation(cpuc_admin, v)
    service.discard_draft(cpuc_admin, v, "re-upload")
    assert not AllocationVersion.objects.filter(label="broken").exists()


def test_managing_allocation_needs_its_own_permission_in_a_managing_organisation(cpuc_admin, pge, pge_admin, make_user):
    from cedars.services.accounts import permissions as perms

    assert pge.manages_allocation  # a funding IOU manages them from the start
    with pytest.raises(AccessDenied, match="permission to manage them"):  # an organization administrator is not enough
        service.import_allocation_csv(pge_admin, _factors(), label="x", purpose="filing", organisation=pge)
    analyst = make_user("analyst@pge.example.org", "Ann Analyst")
    access.grant(pge_admin, analyst, pge, perms.ALLOCATION, reason="manages the SW factors")
    with pytest.raises(AccessDenied):  # the permission counts only while acting for the organization that granted it
        service.import_allocation_csv(analyst, _factors(), label="x", purpose="filing")
    v = service.import_allocation_csv(analyst, _factors(), label="2028 TUAL", purpose="filing", organisation=pge)
    assert v.created_organisation == pge
    service.confirm_allocation(analyst, v, organisation=pge)
    v.refresh_from_db()
    assert (v.status, v.confirmed_by, v.confirmed_organisation) == ("confirmed", analyst, pge)
    events = list(service.allocation_history(v))
    assert [e.action for e in events] == ["allocation_version.imported", "allocation_version.confirmed"]
    assert all(e.organisation == pge and e.actor == analyst for e in events)


def test_allocation_permission_only_in_a_managing_organisation(cpuc_admin, make_user):
    from cedars.services.accounts import permissions as perms

    bayren = access.create_organisation(cpuc_admin, code="BAYREN", name="Bay Area Regional Energy Network", kind="pa", pa_type="REN")
    assert not bayren.manages_allocation
    admin = make_user("admin@bayren.example.org", "Bo Admin")
    access.grant(cpuc_admin, admin, bayren, perms.ORG_ADMIN, reason="first admin")
    member = make_user("m@bayren.example.org")
    ok, why = access.can_grant(admin, member, bayren, perms.ALLOCATION)
    assert not ok and "does not manage statewide allocations" in why
    access.change_organisation(cpuc_admin, bayren, reason="agreed by the funding PAs", manages_allocation=True)
    assert access.can_grant(admin, member, bayren, perms.ALLOCATION)[0]


def test_confirming_tells_the_other_managing_organisations(cpuc_admin, pge, pge_admin, make_user):
    from cedars.services.accounts import permissions as perms
    from cedars.services.notifications.models import Notification

    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    sce_admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, sce_admin, sce, perms.ORG_ADMIN, reason="first admin")
    sce_manager = make_user("sw@sce.example.org")
    access.grant(sce_admin, sce_manager, sce, perms.ALLOCATION, reason="SW factors")
    pge_manager = make_user("sw@pge.example.org")
    access.grant(pge_admin, pge_manager, pge, perms.ALLOCATION, reason="SW factors")
    v = service.import_allocation_csv(pge_manager, _factors(), label="claims 2026", purpose="claims", organisation=pge)
    service.confirm_allocation(pge_manager, v, organisation=pge)
    told = Notification.objects.filter(user=sce_manager, link=f"/allocations/{v.pk}/")
    assert told.count() == 1 and "PGE confirmed claims 2026" in told.get().body
    assert not Notification.objects.filter(user=pge_manager, link=f"/allocations/{v.pk}/").exists()


def test_allocation_pages_for_pas(signed_in, cpuc_admin, pge, pge_admin, make_user, client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from cedars.services.accounts import permissions as perms
    from tests.conftest import page_only

    v = service.import_allocation_csv(cpuc_admin, _factors(), label="CPUC draft", purpose="filing")
    c = signed_in(pge_admin)  # reads every version, changes none
    page = page_only(c.get("/allocations/").content.decode())
    assert "CPUC draft" in page and "Load a factor file" not in page and "Statewide allocation factors</a>" in page
    assert "Confirm this version" not in page_only(c.get(f"/allocations/{v.pk}/").content.decode())
    assert c.post(f"/allocations/{v.pk}/", {"action": "confirm"}).status_code == 404
    assert c.get(f"/allocations/{v.pk}/factors.csv").content.decode().startswith("Year,ProgramID")
    assert c.get("/admin/allocations/")["Location"] == "/allocations/"

    manager = make_user("sw@pge.example.org")
    access.grant(pge_admin, manager, pge, perms.ALLOCATION, reason="SW factors")
    client.logout()
    c = signed_in(manager)
    r = c.post("/allocations/", {"file": SimpleUploadedFile("f.csv", _factors()), "label": "PGE 2028", "purpose": "filing", "cycle": 2028})
    mine = AllocationVersion.objects.get(label="PGE 2028")
    assert r["Location"] == f"/allocations/{mine.pk}/" and mine.created_organisation == pge
    c.post(f"/allocations/{mine.pk}/", {"action": "confirm"})
    mine.refresh_from_db()
    assert mine.status == "confirmed" and mine.confirmed_organisation == pge
    page = c.get(f"/allocations/{mine.pk}/").content.decode()
    assert "for PGE" in page and "Confirmed" in page


def test_allocation_pages(signed_in, cpuc_admin):
    from django.core.files.uploadedfile import SimpleUploadedFile

    c = signed_in(cpuc_admin)
    r = c.post("/allocations/", {"file": SimpleUploadedFile("f.csv", _factors()), "label": "2028 TUAL", "purpose": "filing", "cycle": 2028})
    v = AllocationVersion.objects.get(label="2028 TUAL")
    assert r["Location"] == f"/allocations/{v.pk}/"
    page = c.get(r["Location"]).content.decode()
    assert "Confirm this version" in page and "304 program-years" in page
    c.post(f"/allocations/{v.pk}/", {"action": "confirm"})
    v.refresh_from_db()
    assert v.status == "confirmed"
    assert c.get(f"/allocations/{v.pk}/factors.csv").content.decode().startswith("Year,ProgramID")
