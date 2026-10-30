from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.modules.budgets import service, shifts
from cedars.modules.budgets.models import FundShift
from cedars.modules.calculations.models import Run
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification
from cedars.services.registry import service as registry
from cedars.services.registry.models import FilingCycle

from .conftest import sign_in
from .test_budgets import filer, filing, finish_run, job, programs, release  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db
FACTORS = (
    "Year,ProgramID,ProgramName,PercentElectric,PGEGasFactor,SDGEGasFactor,SCEGasFactor,SCGGasFactor,PGEElectricFactor,"
    "SDGEElectricFactor,SCEElectricFactor,SCGElectricFactor,PGEWeightedFactor,SDGEWeightedFactor,SCEWeightedFactor,SCGWeightedFactor\n"
    "2028,SW_X,Statewide X,1,0,0,0,0,0.6,0,0.4,0,0.6,0,0.4,0\n"
)


def L(*rows):  # noqa: N802
    return [shifts.LineIn(p, str(y), str(a)) for p, y, a in rows]


@pytest.fixture
def cycle(filing):  # noqa: F811
    return filing.cycle


@pytest.fixture
def standing(filer, filing, pge, programs, release):  # noqa: F811
    v = service.upload(filer, filing, pge, job(), "tual.zip")
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    return service.confirm(filer, v)


@pytest.fixture
def statewide(cpuc_admin, programs):  # noqa: F811
    v = registry.import_allocation_csv(cpuc_admin, FACTORS.encode(), label="2028 factors", purpose="filing")
    registry.confirm_allocation(cpuc_admin, v)
    registry.create_program(cpuc_admin, prg_id="SW_X", administrator=None, statewide=True, valid_from=date(2020, 1, 1), name="SW X")
    return "SW_X"


@pytest.fixture
def sce_confirmer(make_user, cpuc_admin, programs):  # noqa: F811
    sce = programs
    admin = make_user("admin@sce.com")
    access.grant(cpuc_admin, admin, sce, "org.admin")
    u = make_user("budgets@sce.com")
    for p in ("budgets.read", "budgets.confirm"):
        access.grant(admin, u, sce, p)
    return u


def test_an_unbalanced_shift_is_rejected_and_stays_a_draft(filer, pge, cycle, programs):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="Move marketing", reason="HVAC demand")
    shifts.set_lines(filer, s, L(("PGE_Res_HVAC", 2028, "1,000"), ("PGE_Com_Ltg", 2028, "-$400")))
    with pytest.raises(shifts.ShiftInvalid, match=r"does not balance: \$1,000.00 in and \$400.00 out, \$600.00 net"):
        shifts.submit(filer, s)
    s.refresh_from_db()
    assert s.status == FundShift.Status.DRAFT and AuditEvent.objects.filter(action="fund_shift.rejected").exists()
    shifts.set_lines(filer, s, L(("PGE_Res_HVAC", 2028, "400")))
    with pytest.raises(shifts.ShiftInvalid, match="two lines or more"):
        shifts.submit(filer, s)


def test_lines_are_checked_and_nothing_is_saved_with_a_problem(filer, pge, cycle, programs):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Res_HVAC", 2028, 5), ("PGE_Com_Ltg", 2028, -5)))
    bad = L(
        ("SCE_Ind", 2028, 10),
        ("NOPE", 2028, 10),
        ("PGE_Res_HVAC", 2035, 10),
        ("PGE_Com_Ltg", 2029, 0),
        ("PGE_Com_Ltg", 2030, "ten"),
        ("PGE_Res_HVAC", 2029, 1),
        ("PGE_Res_HVAC", 2029, 2),
    )
    with pytest.raises(shifts.ShiftInvalid) as e:
        shifts.set_lines(filer, s, bad + [shifts.LineIn("", "", "")])
    assert [p.split(":")[0] for p in e.value.problems] == ["Line 1", "Line 2", "Line 3", "Line 4", "Line 5", "Line 7"]
    assert "another program administrator's" in e.value.problems[0] and "more than one line" in e.value.problems[-1]
    assert [(ln.prg_id, ln.amount) for ln in s.lines.all()] == [("PGE_Com_Ltg", -5), ("PGE_Res_HVAC", 5)]


def test_making_a_shift_needs_budgets_submit(pge, pge_admin, cycle, make_user, programs):  # noqa: F811
    reader = make_user("reader@pge.com")
    access.grant(pge_admin, reader, pge, "budgets.read")
    with pytest.raises(AccessDenied, match="Budgets: Submit"):
        shifts.create(reader, pge, cycle, title="t", reason="r")


def test_a_balanced_shift_changes_the_operating_budget_not_the_approved_one(filer, pge, cycle, standing):  # noqa: F811
    approved = service.table(standing).total.budget
    s = shifts.create(filer, pge, cycle, title="Into HVAC", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Res_HVAC", 2029, 250), ("PGE_Com_Ltg", 2028, -150), ("PGE_Res_HVAC", 2028, -100)))
    assert shifts.warnings(s) == []
    s = shifts.submit(filer, s)
    assert s.status == FundShift.Status.EFFECTIVE and s.cofunders == {} and s.effective_at is not None
    ob = shifts.operating_budget(pge, cycle)
    rows = {(r.prg_id, r.year): r for r in ob.rows}
    assert rows[("PGE_Res_HVAC", 2029)].approved == Decimal(1900 + 200) and rows[("PGE_Res_HVAC", 2029)].shifted == 250
    assert rows[("PGE_Com_Ltg", 2028)].operating == Decimal(300 + 100 - 150)
    assert ob.total().approved == approved and ob.total().operating == approved and ob.total([2029]).shifted == 250
    assert service.table(standing).total.budget == approved  # BR-14.4: the approved budget is unchanged
    assert ob.standing == standing and ob.shifts == [s]
    with pytest.raises(AccessDenied, match="new shift that moves the money back"):
        shifts.withdraw(filer, s, "oops")


def test_moving_out_more_than_the_operating_budget_warns(filer, pge, cycle, standing):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Com_Ltg", 2028, -1000), ("PGE_Res_HVAC", 2028, 1000)))
    assert shifts.warnings(s) == ["PGE_Com_Ltg 2028: moves $1,000.00 out of an operating budget of $400.00."]


def test_a_later_filing_carries_the_earlier_shifts(filer, pge, cycle, standing, cpuc_admin, release):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Com_Ltg", 2028, -100), ("PGE_Res_HVAC", 2028, 100)))
    shifts.submit(filer, s)
    mcal = service.add_filing(cpuc_admin, first_year=2028, kind="MCAL", due_at=timezone.now() + timedelta(days=60), acc_version="2026")
    v = service.upload(filer, mcal, pge, job(), "mcal.zip")
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    v = service.confirm(filer, v)
    ob = shifts.operating_budget(pge, cycle)
    assert ob.standing == v and ob.shifts == [] and [x.pk for x in ob.earlier] == [s.pk]
    assert ob.total().shifted == 0


def test_a_statewide_shift_waits_for_every_co_funder(filer, pge, cycle, statewide, sce_confirmer, programs, make_user):  # noqa: F811
    sce = programs
    s = shifts.create(filer, pge, cycle, title="Into SW X", reason="r")
    shifts.set_lines(filer, s, L((statewide, 2028, 500), ("PGE_Com_Ltg", 2028, -500)))
    s = shifts.submit(filer, s)
    assert s.status == FundShift.Status.WAITING and s.cofunders == {"SCE": ["SW_X 2028"]}
    note = Notification.objects.get(user=sce_confirmer)
    assert "waiting for SCE" in note.title and note.link == reverse("budgets:shift", args=[s.pk])
    assert shifts.operating_budget(pge, cycle).total().shifted == 0  # not in effect yet
    assert shifts.can_read(sce_confirmer, s) and list(shifts.readable(sce_confirmer)) == [s]
    assert [x.pk for x in shifts.waiting_for(sce)] == [s.pk]
    with pytest.raises(AccessDenied):
        shifts.acknowledge(filer, s, sce)  # not a member of SCE
    with pytest.raises(AccessDenied):
        shifts.acknowledge(filer, s, pge)  # PGE is not a co-funder
    s = shifts.acknowledge(sce_confirmer, s, sce, note="fine")
    assert s.status == FundShift.Status.EFFECTIVE and shifts.waiting_for(sce) == []
    assert Notification.objects.filter(user=filer, title="Fund shift in effect").exists()
    assert shifts.operating_budget(pge, cycle).total([2028]).shifted == 0 and len(shifts.operating_budget(pge, cycle).shifts) == 1
    stranger = make_user("x@example.com")
    assert not shifts.can_read(stranger, s)


def test_a_draft_is_private_and_a_waiting_shift_can_be_withdrawn(filer, pge, cycle, statewide, sce_confirmer):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L((statewide, 2028, 1), ("PGE_Com_Ltg", 2028, -1)))
    assert not shifts.can_read(sce_confirmer, s) and list(shifts.readable(sce_confirmer)) == []
    s = shifts.submit(filer, s)
    with pytest.raises(shifts.ShiftInvalid, match="why"):
        shifts.withdraw(filer, s, " ")
    s = shifts.withdraw(filer, s, "agreed elsewhere")
    assert s.status == FundShift.Status.WITHDRAWN and s.withdrawn_reason == "agreed elsewhere"
    assert [e.action for e in AuditEvent.objects.filter(object_type="budgets.fundshift").order_by("id")] == [
        "fund_shift.created",
        "fund_shift.lines_set",
        "fund_shift.submitted",
        "fund_shift.withdrawn",
    ]


def test_the_pages(client, filer, pge, cycle, standing, statewide, sce_confirmer, programs):  # noqa: F811
    sign_in(client, filer)
    assert b"New fund shift" in client.get(reverse("budgets:shifts")).content
    r = client.post(reverse("budgets:shift_new"), {"cycle": cycle.pk, "title": "Into HVAC", "reason": "demand"})
    s = FundShift.objects.get()
    assert r.status_code == 302 and r.url == reverse("budgets:shift_lines", args=[s.pk])
    page = client.get(r.url)
    assert page.status_code == 200 and page.content.count(b'name="prg_id"') == 4 and b'<option value="SW_X">' in page.content
    bad = client.post(r.url, {"prg_id": ["PGE_Res_HVAC", "NOPE"], "year": ["2028", "2028"], "amount": ["10", "-10"]})
    assert bad.status_code == 200 and b"NOPE is not in the program registry" in bad.content and not s.lines.exists()
    more = client.post(r.url, {"prg_id": ["PGE_Res_HVAC", ""], "year": ["2028", ""], "amount": ["10", ""], "more": "1"})
    assert more.status_code == 302 and client.get(more.url).content.count(b'name="prg_id"') == 1 + 4 + 4
    client.post(r.url, {"prg_id": ["PGE_Res_HVAC", "PGE_Com_Ltg"], "year": ["2028", "2028"], "amount": ["10", "-20"]})
    page = client.get(reverse("budgets:shift", args=[s.pk]))
    assert b"does not balance" in page.content and b"-$20.00" in page.content
    client.post(reverse("budgets:shift_submit", args=[s.pk]))
    s.refresh_from_db()
    assert s.status == "draft"
    client.post(
        reverse("budgets:shift_lines", args=[s.pk]), {"prg_id": ["SW_X", "PGE_Com_Ltg"], "year": ["2028"] * 2, "amount": ["20", "-20"]}
    )
    client.post(reverse("budgets:shift_submit", args=[s.pk]))
    s.refresh_from_db()
    assert s.status == "waiting"
    assert client.get(reverse("budgets:shift_lines", args=[s.pk])).status_code == 404
    client.logout()
    sign_in(client, sce_confirmer)
    assert b"Waiting for SCE to acknowledge" in client.get(reverse("budgets:shifts")).content
    assert b"Acknowledge for SCE" in client.get(reverse("budgets:shift", args=[s.pk])).content
    client.post(reverse("budgets:shift_acknowledge", args=[s.pk]), {"note": "ok"})
    s.refresh_from_db()
    assert s.status == "effective"
    assert client.get(reverse("budgets:operating", args=[2028]) + "?org=PGE").status_code == 404  # SCE reads its own, not PGE's
    client.logout()
    sign_in(client, filer)
    page = client.get(reverse("budgets:operating", args=[2028]) + "?year=2028")
    assert page.status_code == 200 and b"Into HVAC" in page.content
    csv = client.get(reverse("budgets:operating_csv", args=[2028])).content.decode().splitlines()
    assert csv[0].startswith("PA,PrgID") and "PGE,SW_X,SW X,2028,0,20.00,20.00" in csv
    assert csv[-1].startswith("PGE,Total")


def test_holders_lists_who_holds_a_permission(pge, pge_admin, filer, make_user):  # noqa: F811
    assert list(access.holders(pge, "budgets.submit")) == [filer]
    assert pge_admin in access.org_admins(pge)


def test_cycles_offered_are_those_with_a_filing(filing):  # noqa: F811
    FilingCycle.objects.create(first_year=2032, last_year=2035)
    assert [c.first_year for c in shifts.cycles()] == [2028]
