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
def statewide(cpuc_admin, programs, pge):  # noqa: F811
    v = registry.import_allocation_csv(cpuc_admin, FACTORS.encode(), label="2028 factors", purpose="filing")
    registry.confirm_allocation(cpuc_admin, v)
    p = registry.create_program(cpuc_admin, prg_id="SW_X", administrator=None, statewide=True, valid_from=date(2020, 1, 1), name="SW X")
    registry.set_lead(cpuc_admin, p, pge, "PG&E files it")  # only a statewide program's lead PA files it
    return "SW_X"


@pytest.fixture
def sce_confirmer(make_user, cpuc_admin, programs):  # noqa: F811
    sce = programs
    admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, admin, sce, "org.admin")
    u = make_user("budgets@sce.example.org")
    for p in ("budgets.read", "budgets.confirm", "budgets.submit"):
        access.grant(admin, u, sce, p)
    return u


@pytest.fixture
def sce_standing(sce_confirmer, filing, programs, release):  # noqa: F811
    """SCE's approved budget: SCE_Ind 2028 $1,000 (no shift may take a program below zero, so a funder needs a budget to place from)."""
    sce = programs
    v = service.upload(
        sce_confirmer, filing, sce, job(["SCE_Ind|2028|2028Q1|SCE|1000|0|0|0|0|0|0|0|0|0|0"], ["S1|SCE_Ind|2028Q1|1|0|0|0|0"]), "sce.zip"
    )
    finish_run(Run.objects.get(pk=service.price(sce_confirmer, v)))
    v.refresh_from_db()
    return service.confirm(sce_confirmer, v)


def test_an_unbalanced_shift_is_rejected_and_stays_a_draft(filer, pge, cycle, programs):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="Move marketing", reason="HVAC demand")
    shifts.set_lines(filer, s, L(("PGE_Res_HVAC", 2028, "1,000"), ("PGE_Com_Ltg", 2028, "-$400")))
    with pytest.raises(
        shifts.ShiftInvalid, match=r"PGE's part does not balance: \$1,000.00 into its programs and \$400.00 out, \$600.00 net"
    ):
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
    assert "not one of PGE's programs" in e.value.problems[0] and "more than one line" in e.value.problems[-1]
    assert [(ln.prg_id, ln.amount) for ln in s.lines.all()] == [("PGE_Com_Ltg", -5), ("PGE_Res_HVAC", 5)]


def test_making_a_shift_needs_budgets_submit(pge, pge_admin, cycle, make_user, programs):  # noqa: F811
    reader = make_user("reader@pge.example.org")
    access.grant(pge_admin, reader, pge, "budgets.read")
    with pytest.raises(AccessDenied, match="Budgets: submit permission"):
        shifts.create(reader, pge, cycle, title="t", reason="r")


def test_a_balanced_shift_changes_the_operating_budget_not_the_approved_one(filer, pge, cycle, standing):  # noqa: F811
    approved = service.table(standing).total.budget
    s = shifts.create(filer, pge, cycle, title="Into HVAC", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Res_HVAC", 2029, 250), ("PGE_Com_Ltg", 2028, -150), ("PGE_Res_HVAC", 2028, -100)))
    assert shifts.problems(s) == []
    s = shifts.submit(filer, s)
    assert s.status == FundShift.Status.EFFECTIVE and s.cofunders == [] and s.shares == {} and s.effective_at is not None
    ob = shifts.operating_budget(pge, cycle)
    rows = {(r.prg_id, r.year): r for r in ob.rows}
    assert rows[("PGE_Res_HVAC", 2029)].approved == Decimal(1900 + 200) and rows[("PGE_Res_HVAC", 2029)].shifted == 250
    assert rows[("PGE_Com_Ltg", 2028)].operating == Decimal(300 + 100 - 150)
    assert ob.total().approved == approved and ob.total().operating == approved and ob.total([2029]).shifted == 250
    assert service.table(standing).total.budget == approved  # The approved budget is unchanged
    assert ob.standing == standing and ob.shifts == [s]
    with pytest.raises(AccessDenied, match="new shift that moves the money back"):
        shifts.withdraw(filer, s, "oops")


def test_no_shift_takes_a_programs_operating_budget_below_zero(filer, pge, cycle, standing):  # noqa: F811
    # Jake, 2026-11-02: never below zero. PGE_Com_Ltg 2028 holds $400 (300 program costs + 100 rebates).
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Com_Ltg", 2028, -1000), ("PGE_Res_HVAC", 2028, 1000)))
    assert shifts.problems(s) == [
        "PGE PGE_Com_Ltg 2028: moves $1,000.00 out of an operating budget of $400.00, which would leave it at -$600.00. "
        "No fund shift may take a program's operating budget below zero."
    ]
    with pytest.raises(shifts.ShiftInvalid, match="below zero"):
        shifts.submit(filer, s)
    assert s.status == FundShift.Status.DRAFT
    shifts.set_lines(filer, s, L(("PGE_Com_Ltg", 2028, -400), ("PGE_Res_HVAC", 2028, 400)))
    assert shifts.problems(s) == []  # down to exactly zero is allowed
    shifts.submit(filer, s)
    t = shifts.create(filer, pge, cycle, title="again", reason="r")
    shifts.set_lines(filer, t, L(("PGE_Com_Ltg", 2028, -1), ("PGE_Res_HVAC", 2028, 1)))
    assert "operating budget of $0.00" in shifts.problems(t)[0]  # the first shift is in effect
    # a program-year with no approved budget at all (every shift counts against zero without a standing filing)
    shifts.set_lines(filer, t, L(("PGE_Com_Ltg", 2030, -1), ("PGE_Res_HVAC", 2030, 1)))
    assert "PGE PGE_Com_Ltg 2030: moves $1.00 out of an operating budget of $0.00" in shifts.problems(t)[0]


def test_a_later_filing_carries_the_earlier_shifts(client, filer, pge, cycle, standing, cpuc_admin, release):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("PGE_Com_Ltg", 2028, -100), ("PGE_Res_HVAC", 2028, 100)))
    shifts.submit(filer, s)
    mcal = service.add_filing(cpuc_admin, first_year=2028, kind="MCAL", due_at=timezone.now() + timedelta(days=60), acc_version="2026")
    v = service.upload(filer, mcal, pge, job(), "mcal.zip")
    page = sign_in(client, filer).get(reverse("budgets:filing", args=[mcal.pk])).content.decode()
    assert "1 fund shift in effect for PGE in this cycle." in page and "Put their amounts into this filing" in page
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    v = service.confirm(filer, v)
    ob = shifts.operating_budget(pge, cycle)
    assert ob.standing == v and ob.shifts == [] and [x.pk for x in ob.earlier] == [s.pk]
    assert ob.total().shifted == 0


def test_a_statewide_shift_changes_every_funders_portion(
    filer,  # noqa: F811
    pge,
    cycle,
    standing,
    statewide,
    sce_confirmer,
    sce_standing,
    programs,  # noqa: F811
    make_user,  # noqa: F811
):
    sce = programs
    s = shifts.create(filer, pge, cycle, title="Into SW X", reason="r")
    shifts.set_lines(filer, s, L((statewide, 2028, 500), ("PGE_Com_Ltg", 2028, -500)))
    # PGE funds 60% of SW X: its portion goes up by $300, so its own lines must move $300 out, not $500
    with pytest.raises(shifts.ShiftInvalid, match=r"\$500.00 out, with its \$300.00 portion of the statewide lines, -\$200.00 net"):
        shifts.submit(filer, s)
    shifts.set_lines(filer, s, L((statewide, 2028, 500), ("PGE_Com_Ltg", 2028, -300)))
    s = shifts.submit(filer, s)
    assert s.status == FundShift.Status.WAITING and s.cofunders == ["SCE"]
    assert s.shares == {"PGE": {"SW_X 2028": "300.00"}, "SCE": {"SW_X 2028": "200.00"}}
    assert s.factors == {"PGE": {"SW_X 2028": "0.6000"}, "SCE": {"SW_X 2028": "0.4000"}}
    note = Notification.objects.get(user=sce_confirmer)
    assert "waiting for SCE" in note.title and "by $200.00 net" in note.body and note.link == reverse("budgets:shift", args=[s.pk])
    assert shifts.operating_budget(pge, cycle).total().shifted == 0  # not in effect yet
    assert shifts.can_read(sce_confirmer, s) and list(shifts.readable(sce_confirmer)) == [s]
    assert [x.pk for x in shifts.waiting_for(sce)] == [s.pk] and shifts.waiting_for(pge) == []
    with pytest.raises(AccessDenied):
        shifts.acknowledge(filer, s, sce, L(("SCE_Ind", 2028, -200)))  # not a member of SCE
    with pytest.raises(AccessDenied):
        shifts.acknowledge(filer, s, pge, L(("PGE_Com_Ltg", 2028, -200)))  # PGE is the shift's PA
    with pytest.raises(shifts.ShiftInvalid, match=r"must add up to -\$200.00 \(they add up to -\$150.00\)"):
        shifts.acknowledge(sce_confirmer, s, sce, L(("SCE_Ind", 2028, -150)))
    with pytest.raises(shifts.ShiftInvalid) as e:
        shifts.acknowledge(sce_confirmer, s, sce, L(("PGE_Res_HVAC", 2028, -100), (statewide, 2028, -100)))
    assert "not one of SCE's programs" in e.value.problems[0] and "statewide program" in e.value.problems[1]
    assert s.lines.filter(pa="SCE").count() == 0 and not s.acknowledgements.exists()
    s = shifts.acknowledge(sce_confirmer, s, sce, L(("SCE_Ind", 2028, -200)), note="from industrial")
    assert s.status == FundShift.Status.EFFECTIVE and shifts.waiting_for(sce) == []
    assert Notification.objects.filter(user=filer, title="Fund shift in effect").exists()
    assert [p.net for p in shifts.parts(s)] == [0, 0]
    pge_rows = {(r.prg_id, r.year): r.shifted for r in shifts.operating_budget(pge, cycle).rows}
    sce_rows = {(r.prg_id, r.year): r.shifted for r in shifts.operating_budget(sce, cycle).rows}
    pge_rows = {k: v for k, v in pge_rows.items() if v}
    assert pge_rows == {("SW_X", 2028): 300, ("PGE_Com_Ltg", 2028): -300}  # its portion on the statewide row, not the $500
    assert sce_rows == {("SW_X", 2028): 200, ("SCE_Ind", 2028): -200}
    sw = next(r for r in shifts.operating_budget(sce, cycle).rows if r.prg_id == "SW_X")
    assert sw.statewide and sw.statewide_total == 0 and sw.factor == Decimal("0.4000")  # nobody filed SW_X: approved 0
    assert shifts.operating_budget(sce, cycle).shifts == [s]
    stranger = make_user("x@example.com")
    assert not shifts.can_read(stranger, s)


def test_a_statewide_line_needs_allocation_factors(filer, pge, cycle, programs, cpuc_admin):  # noqa: F811
    registry.create_program(cpuc_admin, prg_id="SW_Y", administrator=None, statewide=True, valid_from=date(2020, 1, 1), name="SW Y")
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("SW_Y", 2028, 10), ("PGE_Com_Ltg", 2028, -10)))
    assert "SW_Y 2028 has no allocation factors in any confirmed allocation version" in shifts.problems(s)[0]


def test_a_draft_is_private_and_a_waiting_shift_can_be_withdrawn(filer, pge, cycle, standing, statewide, sce_confirmer):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L((statewide, 2028, 10), ("PGE_Com_Ltg", 2028, -6)))
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


def test_the_pages(client, filer, pge, cycle, standing, statewide, sce_confirmer, sce_standing, programs):  # noqa: F811
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
        reverse("budgets:shift_lines", args=[s.pk]), {"prg_id": ["SW_X", "PGE_Com_Ltg"], "year": ["2028"] * 2, "amount": ["20", "-12"]}
    )
    client.post(reverse("budgets:shift_submit", args=[s.pk]))
    s.refresh_from_db()
    assert s.status == "waiting"
    assert client.get(reverse("budgets:shift_lines", args=[s.pk])).status_code == 404
    client.logout()
    sign_in(client, sce_confirmer)
    assert b"Waiting for SCE to acknowledge" in client.get(reverse("budgets:shifts")).content
    assert b"Place SCE's portion and acknowledge" in client.get(reverse("budgets:shift", args=[s.pk])).content
    page = client.get(reverse("budgets:shift_acknowledge", args=[s.pk]))
    assert b"portion changes by $8.00 net" in page.content and b'<option value="SCE_Ind">' in page.content
    assert b'<td class="num">0.4000</td>' in page.content and b"$20.00" in page.content  # the factor and the whole change
    assert b'<option value="SW_X">' not in page.content
    bad = client.post(reverse("budgets:shift_acknowledge", args=[s.pk]), {"prg_id": ["SCE_Ind"], "year": ["2028"], "amount": ["-5"]})
    assert bad.status_code == 200 and b"must add up to -$8.00" in bad.content
    client.post(
        reverse("budgets:shift_acknowledge", args=[s.pk]), {"prg_id": ["SCE_Ind"], "year": ["2028"], "amount": ["-8"], "note": "ok"}
    )
    s.refresh_from_db()
    assert s.status == "effective"
    assert client.get(reverse("budgets:operating", args=[2028]) + "?org=PGE").status_code == 404  # SCE reads its own, not PGE's
    client.logout()
    sign_in(client, filer)
    page = client.get(reverse("budgets:operating", args=[2028]) + "?year=2028")
    assert page.status_code == 200 and b"Into HVAC" in page.content and b"Shifts before a later filing are not added" in page.content
    filing_page = client.get(reverse("budgets:filing", args=[standing.submission.deadline.pk]))
    assert b"fund shift in effect for PGE" not in filing_page.content  # the filing is confirmed: nothing more to put in it
    csv = client.get(reverse("budgets:operating_csv", args=[2028])).content.decode().splitlines()
    assert csv[0].startswith("PA,PrgID") and "PGE,SW_X,SW X,2028,0,12.00,12.00,0,0.6000" in csv
    assert csv[-1].startswith("PGE,Total")


def test_holders_lists_who_holds_a_permission(pge, pge_admin, filer, make_user):  # noqa: F811
    assert list(access.holders(pge, "budgets.submit")) == [filer]
    assert pge_admin in access.org_admins(pge)


def test_cycles_offered_are_those_with_a_filing(filing):  # noqa: F811
    FilingCycle.objects.create(first_year=2032, last_year=2035)
    assert [c.first_year for c in shifts.cycles()] == [2028]


SW_PAIR = FACTORS + "2028,SW_Y,Statewide Y,1,0,0,0,0,0.3,0,0.7,0,0.3,0,0.7,0\n2028,SW_Z,Statewide Z,1,0,0,0,0,0.6,0,0.4,0,0.6,0,0.4,0\n"


@pytest.fixture
def statewide_pair(cpuc_admin, programs, pge):  # noqa: F811
    """SW_X and SW_Z split PGE 60 / SCE 40; SW_Y splits PGE 30 / SCE 70."""
    v = registry.import_allocation_csv(cpuc_admin, SW_PAIR.encode(), label="2028 factors", purpose="filing")
    registry.confirm_allocation(cpuc_admin, v)
    for prg in ("SW_X", "SW_Y", "SW_Z"):
        p = registry.create_program(cpuc_admin, prg_id=prg, administrator=None, statewide=True, valid_from=date(2020, 1, 1), name=prg)
        registry.set_lead(cpuc_admin, p, pge, "PG&E files it")


def test_moving_between_statewide_programs_leaves_each_funder_its_net_gap(
    filer,  # noqa: F811
    pge,
    cycle,
    standing,
    statewide_pair,
    sce_confirmer,
    sce_standing,
    programs,  # noqa: F811
    cpuc_admin,  # noqa: F811
):
    """Jake, 2026-11-02: SW-to-SW shifts with different allocation rates; the gaps are covered by local portfolio shifts."""
    sce = programs
    s = shifts.create(filer, pge, cycle, title="X to Y", reason="r")
    shifts.set_lines(filer, s, L(("SW_X", 2028, -1000), ("SW_Y", 2028, 1000)))
    # PGE: -600 + 300 = -300 net (money comes back to PGE's portfolio); SCE: -400 + 700 = +300 (SCE must find $300)
    split = shifts.compute_shares(s)
    assert split.shares == {"PGE": {"SW_X 2028": -600, "SW_Y 2028": 300}, "SCE": {"SW_X 2028": -400, "SW_Y 2028": 700}}
    assert split.amounts == {"SW_X 2028": -1000, "SW_Y 2028": 1000} and split.problems == []
    [mine, theirs] = shifts.parts(s)
    assert (mine.pa, mine.portion, theirs.pa, theirs.portion) == ("PGE", -300, "SCE", 300)
    assert [(p.item, p.amount, p.factor, p.portion) for p in theirs.portions] == [
        ("SW_X 2028", -1000, Decimal("0.4000"), -400),
        ("SW_Y 2028", 1000, Decimal("0.7000"), 700),
    ]
    # nobody has filed SW_X, so PGE's portion of it cannot go down by $600
    assert shifts.problems(s) == [
        "PGE's part does not balance: $0.00 into its programs and $0.00 out, with its -$300.00 portion of the statewide lines, "
        "-$300.00 net. Its lines must add up to zero.",
        "PGE SW_X 2028: moves $600.00 out of an operating budget of $0.00, which would leave it at -$600.00. No fund shift may take a "
        "program's operating budget below zero.",
        "SCE SW_X 2028: moves $400.00 out of an operating budget of $0.00, which would leave it at -$400.00. No fund shift may take a "
        "program's operating budget below zero.",
    ]
    # PGE leads SW_X and files its whole $2,000 budget: each funder's operating row is its portion of it
    file_statewide(cpuc_admin, filer, pge, "SW_X|2028|2028Q1|PGE|2000|0|0|0|0|0|0|0|0|0|0")
    pge_rows = {(r.prg_id, r.year): r for r in shifts.operating_budget(pge, cycle).rows}
    sce_rows = {(r.prg_id, r.year): r for r in shifts.operating_budget(sce, cycle).rows}
    assert (pge_rows[("SW_X", 2028)].approved, pge_rows[("SW_X", 2028)].statewide_total, pge_rows[("SW_X", 2028)].factor) == (
        1200,
        2000,
        Decimal("0.6"),
    )
    assert (sce_rows[("SW_X", 2028)].approved, sce_rows[("SW_X", 2028)].factor) == (800, Decimal("0.4"))
    # its portion, not the $2,000 it filed (2028: the MCAL holds 2028 only, so 2029 keeps the earlier filing's budget)
    assert shifts.operating_budget(pge, cycle).total(years=[2028]).approved == 1200 + 400 + 2000
    shifts.set_lines(filer, s, L(("SW_X", 2028, -1000), ("SW_Y", 2028, 1000), ("PGE_Com_Ltg", 2028, 300)))
    assert shifts.problems(s) == []
    s = shifts.submit(filer, s)
    assert s.status == FundShift.Status.WAITING and s.cofunders == ["SCE"] and s.informed == []
    assert "by $300.00 net" in Notification.objects.get(user=sce_confirmer).body
    with pytest.raises(shifts.ShiftInvalid, match=r"must add up to -\$300.00"):
        shifts.acknowledge(sce_confirmer, s, sce, L(("SCE_Ind", 2028, -200)))
    s = shifts.acknowledge(sce_confirmer, s, sce, L(("SCE_Ind", 2028, -300)))
    assert s.status == FundShift.Status.EFFECTIVE
    pge_rows = {(r.prg_id, r.year): r.operating for r in shifts.operating_budget(pge, cycle).rows if r.year == 2028}
    sce_rows = {(r.prg_id, r.year): r.operating for r in shifts.operating_budget(sce, cycle).rows}
    assert pge_rows == {("SW_X", 2028): 600, ("SW_Y", 2028): 300, ("PGE_Com_Ltg", 2028): 700, ("PGE_Res_HVAC", 2028): 2000}
    assert sce_rows == {("SW_X", 2028): 400, ("SW_Y", 2028): 700, ("SCE_Ind", 2028): 700}


def file_statewide(cpuc_admin, filer, pge, *sw_rows):  # noqa: F811
    """PGE's MCAL filing with the statewide programs it leads, PGE_Com_Ltg 2028 at $400 and PGE_Res_HVAC 2028 at $2,000, confirmed: the
    new standing budget."""
    mcal = service.add_filing(cpuc_admin, first_year=2028, kind="MCAL", due_at=timezone.now() + timedelta(days=60), acc_version="2026")
    costs = [*sw_rows, "PGE_Com_Ltg|2028|2028Q1|PGE|300|0|0|0|0|0|0|0|0|0|0", "PGE_Res_HVAC|2028|2028Q1|PGE|2000|0|0|0|0|0|0|0|0|0|0"]
    v = service.upload(filer, mcal, pge, job(costs, ["M3|PGE_Com_Ltg|2028Q3|4|25|0|0|0"]), "mcal.zip")
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    return service.confirm(filer, v)


def test_a_funder_whose_portions_cancel_out_is_informed_not_asked(
    filer,  # noqa: F811
    pge,
    cycle,
    standing,
    statewide_pair,
    sce_confirmer,
    sce_standing,
    programs,  # noqa: F811
    cpuc_admin,  # noqa: F811
):
    """SW_X and SW_Z have the same rates: moving money between them changes nobody's total."""
    file_statewide(cpuc_admin, filer, pge, "SW_X|2028|2028Q1|PGE|2000|0|0|0|0|0|0|0|0|0|0")
    s = shifts.create(filer, pge, cycle, title="X to Z", reason="r")
    shifts.set_lines(filer, s, L(("SW_X", 2028, -100), ("SW_Z", 2028, 100)))
    assert shifts.problems(s) == [] and [p.net for p in shifts.parts(s)] == [0, 0]
    s = shifts.submit(filer, s)
    assert s.status == FundShift.Status.EFFECTIVE and s.cofunders == [] and s.informed == ["SCE"] and s.funders == ["PGE", "SCE"]
    note = Notification.objects.get(user=sce_confirmer)
    assert "no change to SCE's total" in note.title and "nothing to place or acknowledge" in note.body
    assert shifts.waiting_for(programs) == [] and shifts.can_read(sce_confirmer, s)
    assert {(r.prg_id, r.year): r.operating for r in shifts.operating_budget(programs, cycle).rows} == {
        ("SW_X", 2028): 760,
        ("SW_Z", 2028): 40,
        ("SCE_Ind", 2028): 1000,
    }


def test_rounding_cents_go_to_the_shifts_pa(filer, pge, cycle, standing, statewide_pair, sce_confirmer, sce_standing, cpuc_admin):  # noqa: F811
    file_statewide(cpuc_admin, filer, pge, "SW_Y|2028|2028Q1|PGE|2000|0|0|0|0|0|0|0|0|0|0")
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("SW_Y", 2028, "-0.05"), ("PGE_Com_Ltg", 2028, "0.02")))
    split = shifts.compute_shares(s)
    # 0.3 x -0.05 = -0.015 -> -0.02 (half-even: -0.02); 0.7 x -0.05 = -0.035 -> -0.04; one cent over, given back to PGE
    assert split.shares == {"PGE": {"SW_Y 2028": Decimal("-0.01")}, "SCE": {"SW_Y 2028": Decimal("-0.04")}}
    assert split.amounts == {"SW_Y 2028": Decimal("-0.05")}
    shifts.set_lines(filer, s, L(("SW_Y", 2028, "-0.05"), ("PGE_Com_Ltg", 2028, "0.01")))
    assert shifts.problems(s) == []


def test_a_co_funder_cannot_place_its_net_below_zero_either(
    filer,  # noqa: F811
    pge,
    cycle,
    standing,
    statewide_pair,
    sce_confirmer,
    sce_standing,
    programs,  # noqa: F811
    cpuc_admin,  # noqa: F811
):
    sce = programs
    file_statewide(cpuc_admin, filer, pge, "SW_X|2028|2028Q1|PGE|2000|0|0|0|0|0|0|0|0|0|0")
    s = shifts.create(filer, pge, cycle, title="t", reason="r")
    shifts.set_lines(filer, s, L(("SW_X", 2028, 3000), ("PGE_Com_Ltg", 2028, -400), ("PGE_Res_HVAC", 2028, -1400)))
    s = shifts.submit(filer, s)  # SCE's portion rises by $1,200, more than SCE_Ind holds
    assert s.status == FundShift.Status.WAITING
    with pytest.raises(shifts.ShiftInvalid) as e:
        shifts.acknowledge(sce_confirmer, s, sce, L(("SCE_Ind", 2028, -1200)))
    assert e.value.problems == [
        "SCE SCE_Ind 2028: moves $1,200.00 out of an operating budget of $1,000.00, which would leave it at -$200.00. No fund shift may "
        "take a program's operating budget below zero."
    ]
    # the waiting shift's own lines count as spent for PGE's next shift
    t = shifts.create(filer, pge, cycle, title="u", reason="r")
    shifts.set_lines(filer, t, L(("PGE_Com_Ltg", 2028, -1), ("PGE_Res_HVAC", 2028, 1)))
    assert "PGE PGE_Com_Ltg 2028: moves $1.00 out of an operating budget of $0.00" in shifts.problems(t)[0]
