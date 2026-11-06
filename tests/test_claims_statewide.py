"""Statewide claims filed first (Jake, 2026-11-06): a statewide program's lead PA files its claims in statewide confirmations due before
the PAs' own; their results are credited to the funding PAs, who see their shares before they file and whose totals count them."""

import io
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import Run
from cedars.modules.claims import service
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program
from cedars.services.submissions import service as submissions
from cedars.services.submissions import status as filing_status
from cedars.services.submissions.models import Deadline, SubmissionVersion

from .conftest import sign_in
from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claim, claims_zip, filer, programs, row, tables  # noqa: F401, F811 - fixtures
from .test_fund_shifts import FACTORS

pytestmark = pytest.mark.django_db


@pytest.fixture
def year(cpuc_admin):
    now = timezone.now()
    due = {kind: now + timedelta(days=30 * (i + 1)) for i, (kind, _) in enumerate(service.PERIODS)}
    return service.add_year(cpuc_admin, year=2028, acc_version="2026", due=due)


@pytest.fixture
def sw_x(cpuc_admin, pge):
    """SW_X, led by PG&E, funded 60 / 40 by PG&E and SCE."""
    v = registry.import_allocation_csv(cpuc_admin, FACTORS.encode(), label="2028 factors", purpose="filing")
    registry.confirm_allocation(cpuc_admin, v)
    p = registry.create_program(cpuc_admin, prg_id="SW_X", administrator=None, statewide=True, valid_from=date(2020, 1, 1), name="SW X")
    return registry.set_lead(cpuc_admin, p, pge, "PG&E leads SW_X")


def sw_deadline(kind="Q1 statewide claim") -> Deadline:
    return next(d for d in service.periods(2028, statewide=True) if d.kind == kind)


def own_deadline(kind="Q1 claim") -> Deadline:
    return next(d for d in service.periods(2028) if d.kind == kind)


def sw_upload(cid="PGE-2028-101", prg="SW_X") -> bytes:
    return claims_zip(tables(claim(cid, prg=prg), ProgramCost=[row("ProgramCost", PrgID=prg)], Site=[row("Site", SiteID="PGE-S1")]))


def finish_allocated(run: Run) -> Run:
    """The worker's end for an allocated statewide job: SW_X's results credited 60 / 40 to PG&E and SCE."""
    rows = [
        {"_pa": pa_, "_prg": "SW_X", "_prgname": "SW X", "_sector": "Res", "_segment": "RA", "_year": "2028", "_n": 1,
         "TotalSystemBenefit": 1000.0 * share, "FirstYearNetKWh": 500.0 * share, "TotalExpenditures": 200.0 * share,
         "RebatesandIncents": 50.0 * share}
        for pa_, share in (("PGE", 0.6), ("SCE", 0.4))
    ]  # fmt: skip
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf)
    calculations.mark_started(run, worker="test")
    out = run.storage_prefix + "out/"
    storage.store("results").put_bytes(out + "_results/group_sums2_measure_level_run.parquet", buf.getvalue())
    storage.store("results").put_bytes(out + "receipt.json", b"{}")
    return calculations.finish(run, exit_code=0, receipt={"packs": {"files": {}}, "totals": {}})


def confirmed_sw(filer, pge) -> SubmissionVersion:  # noqa: F811
    v = service.upload_claims(filer, sw_deadline(), pge, sw_upload(), "sw.zip")
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    run = Run.objects.get(pk=service.price(filer, v))
    assert run.settings == {"allocate_statewide": True, "acc_purpose": "claims"}
    finish_allocated(run)
    v.refresh_from_db()
    return service.confirm(filer, v)


def rule_findings(v, rule):
    return [f for f in service.findings(v) if f["rule_id"] == rule]


# --- the statewide deadlines


def test_a_claim_year_has_statewide_confirmations_due_first(year):
    sw, own = service.periods(2028, statewide=True), service.periods(2028)
    assert [d.kind for d in sw] == service.SW_KINDS and [d.kind for d in own] == service.KINDS
    for s, o in zip(sw, own, strict=True):
        assert s.period_id == o.period_id and s.due_at < o.due_at and service.counterpart(o) == s and service.counterpart(s) == o
    assert sw[0].due_at == own[0].due_at - timedelta(days=14)  # two weeks before a quarter
    annual, sw_annual = own[-1].due_at, sw[-1].due_at
    assert (sw_annual.year * 12 + sw_annual.month) == (annual.year * 12 + annual.month) - 1  # a month before the annual claim
    assert sw_annual.day == min(annual.day, sw_annual.day) and sw_annual.time() == annual.time()
    assert len(list(service.year_deadlines(year))) == 10


def test_a_statewide_due_date_must_come_before_the_pas_own(cpuc_admin):
    now = timezone.now()
    due = {kind: now + timedelta(days=30 * (i + 1)) for i, (kind, _) in enumerate(service.PERIODS)}
    with pytest.raises(AccessDenied, match="Q2 statewide claim must be due before the Q2 claim"):
        service.add_year(cpuc_admin, year=2028, acc_version="2026", due=due, sw_due={"Q2 statewide claim": due["Q2 claim"]})
    cy = service.add_year(cpuc_admin, year=2028, acc_version="2026", due=due, sw_due={"Q1 statewide claim": now + timedelta(days=3)})
    assert sw_deadline().due_at == now + timedelta(days=3) and sw_deadline("Q2 statewide claim").due_at == due["Q2 claim"] - timedelta(14)
    assert not service.missing_sw_deadlines(cy)


def test_a_year_set_up_before_gets_its_statewide_deadlines(year, cpuc_admin, pge_admin):
    Deadline.objects.filter(kind__in=service.SW_KINDS).delete()
    assert service.missing_sw_deadlines(year)
    with pytest.raises(AccessDenied, match="CPUC system administrator"):
        service.add_sw_deadlines(pge_admin, year)
    service.add_sw_deadlines(cpuc_admin, year)
    assert [d.kind for d in service.periods(2028, statewide=True)] == service.SW_KINDS
    assert AuditEvent.objects.filter(action="claim_year.statewide_deadlines_added").exists()
    with pytest.raises(AccessDenied, match="already have"):
        service.add_sw_deadlines(cpuc_admin, year)


def test_the_set_up_page_adds_missing_statewide_due_dates(client, year, cpuc_admin):
    Deadline.objects.filter(kind__in=service.SW_KINDS).delete()
    sign_in(client, cpuc_admin)
    url = reverse("claims:setup", args=[2028])
    assert b"Add the statewide due dates" in client.get(url).content
    r = client.post(url, {"add_statewide": "1"})
    assert r.status_code == 302 and not service.missing_sw_deadlines(year)


# --- the lead PA


def test_any_pa_records_a_missing_lead_and_only_the_lead_changes_it(cpuc_admin, pge_admin, pge, programs, make_user):  # noqa: F811
    sce = programs
    sce_admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, sce_admin, sce, "org.admin")
    p = registry.create_program(cpuc_admin, prg_id="SW_Y", administrator=None, statewide=True, valid_from=date(2020, 1, 1), name="Y")
    assert "no lead PA recorded" in registry.filing_problem(p, pge)
    assert registry.can_set_lead(pge_admin, p, pge) and registry.can_set_lead(sce_admin, p, sce)
    assert not registry.can_set_lead(pge_admin, p, None) and not registry.can_set_lead(pge_admin, p, sce)  # acting for its own PA
    with pytest.raises(AccessDenied, match="reason"):
        registry.set_lead(pge_admin, p, pge, " ", organisation=pge)
    with pytest.raises(AccessDenied, match="not a statewide program"):
        registry.set_lead(cpuc_admin, Program.objects.get(prg_id="PGE_Res"), pge, "r")
    registry.set_lead(sce_admin, p, sce, "SCE leads it", organisation=sce)  # no lead yet: any PA records one
    assert registry.filing_problem(p, sce) == "" and "led by SCE" in registry.filing_problem(p, pge)
    with pytest.raises(AccessDenied, match="Only SCE, SW_Y's lead PA, changes its lead"):
        registry.set_lead(pge_admin, p, pge, "ours now", organisation=pge)
    registry.set_lead(sce_admin, p, pge, "handed to PG&E", organisation=sce)  # the lead hands it on
    assert not registry.can_set_lead(sce_admin, p, sce) and registry.can_set_lead(pge_admin, p, pge)
    registry.set_lead(cpuc_admin, p, sce, "CPUC correction")  # a CPUC system administrator can still correct it
    events = AuditEvent.objects.filter(action="program.lead_set").order_by("pk")
    assert [(e.before, e.after) for e in events] == [
        ({"lead": None}, {"lead": "SCE"}), ({"lead": "SCE"}, {"lead": "PGE"}), ({"lead": "PGE"}, {"lead": "SCE"})
    ]  # fmt: skip


def test_the_program_page_records_the_lead(client, cpuc_admin, pge_admin, pge, programs):  # noqa: F811
    registry.create_program(cpuc_admin, prg_id="SW_Y", administrator=None, statewide=True, valid_from=date(2020, 1, 1), name="Y")
    sign_in(client, pge_admin)
    page = client.get(reverse("programs:program", args=["SW_Y"])).content.decode()
    assert "no lead PA recorded yet" in page and 'id="lead-form"' in page
    client.post(reverse("programs:lead", args=["SW_Y"]), {"lead-lead": pge.pk, "lead-reason": "PG&E leads it"})
    assert Program.objects.get(prg_id="SW_Y").lead == pge
    registry.set_lead(pge_admin, Program.objects.get(prg_id="SW_Y"), programs, "SCE leads it now", organisation=pge)
    page = client.get(reverse("programs:program", args=["SW_Y"])).content.decode()
    assert 'id="lead-form"' not in page and "Only SCE can change its lead" in page
    r = client.post(reverse("programs:lead", args=["SW_Y"]), {"lead-lead": pge.pk, "lead-reason": "back"})
    assert r.status_code == 404 and Program.objects.get(prg_id="SW_Y").lead == programs


# --- filing


def test_statewide_programs_go_only_in_the_statewide_filing_by_their_lead(filer, year, pge, programs, sw_x, pge_admin):  # noqa: F811
    own = service.upload_claims(filer, own_deadline(), pge, sw_upload(), "own.zip")
    assert own.status == SubmissionVersion.Status.DRAFT and "is a statewide program" in rule_findings(own, "CLM-SW")[0]["message"]
    mixed = claims_zip(tables(claim("PGE-2028-102", prg="PGE_Res"), ProgramCost=[row("ProgramCost", PrgID="PGE_Res")]))
    sw = service.upload_claims(filer, sw_deadline(), pge, mixed, "sw.zip")
    assert "PGE_Res is not a statewide program" in rule_findings(sw, "CLM-SW")[0]["message"]
    registry.set_lead(None, sw_x, programs, "SCE leads it now")
    sw = service.upload_claims(filer, sw_deadline(), pge, sw_upload(), "sw.zip")
    assert "led by SCE; only its lead PA files it" in rule_findings(sw, "CLM-ORG")[0]["message"]


def test_the_statewide_claims_are_compiled_on_their_own(filer, year, pge, programs, sw_x, release):  # noqa: F811
    confirmed_sw(filer, pge)
    assert service.base_for(pge, own_deadline()) is None  # PG&E's own Q1 is not compiled over its statewide Q1
    assert service.base_for(pge, sw_deadline("Q2 statewide claim")).submission.deadline == sw_deadline()
    assert service.earlier_unconfirmed(pge, own_deadline()) is None
    assert submissions.stream(sw_deadline()) == "statewide" and submissions.stream(own_deadline()) == ""


# --- what the funding PAs see


def test_each_funder_sees_its_share_and_counts_it(filer, year, pge, programs, sw_x, release):  # noqa: F811
    sce = programs
    assert service.statewide_share(sce, 2028) == []
    draft = service.upload_claims(filer, sw_deadline(), pge, sw_upload(), "sw.zip")
    finish_allocated(Run.objects.get(pk=service.price(filer, draft)))
    assert service.statewide_share(sce, 2028) == []  # a lead's draft is its own until it confirms
    v = confirmed_sw(filer, pge)
    share = service.statewide_share(sce, 2028)
    assert [(r["prg_id"], r["lead"].code, r["version"].pk) for r in share] == [("SW_X", "PGE", v.pk)]
    assert share[0]["values"]["TotalSystemBenefit"] == pytest.approx(400.0)
    totals = service.year_totals(sce, 2028)  # SCE filed nothing of its own: the share alone
    assert len(totals) == 1 and totals[0]["statewide_lead"] == "PGE" and totals[0]["pa"] == "SCE" and totals[0]["quarters"] == [1]
    assert totals[0]["tsb"] == pytest.approx(400.0) and float(totals[0]["expenditures"]) == pytest.approx(80.0)
    pge_totals = service.year_totals(pge, 2028)  # the lead counts its own 60%, never the whole program
    assert [(t["prg_id"], round(t["tsb"])) for t in pge_totals] == [("SW_X", 600)]
    status = service.statewide_status(2028, own_deadline())
    assert status[0]["deadline"] == sw_deadline() and status[0]["confirmed"] == ["PGE"]


def test_the_statewide_share_page_and_csv(client, filer, year, pge, programs, sw_x, release, make_user):  # noqa: F811
    confirmed_sw(filer, pge)
    sce_user = make_user("claims@sce.example.org")
    access.grant(None, sce_user, programs, "claims.read")
    sign_in(client, sce_user)
    page = client.get(reverse("claims:statewide", args=[2028]) + "?org=SCE").content.decode()
    assert "Credited to SCE" in page and "SW_X" in page and "$400" in page
    csv_text = client.get(reverse("claims:statewide_csv", args=[2028]) + "?org=SCE").content.decode()
    assert csv_text.splitlines()[1].startswith('SCE,2028,SW_X,SW X,PGE,"2028 Q1 statewide claim, version 1",1,')
    period = client.get(reverse("claims:period", args=[own_deadline().pk]) + "?org=SCE").content.decode()
    assert "Statewide claims credited to SCE" in period and "Confirmed by PGE" in period


def test_only_lead_pas_owe_the_statewide_filing(year, pge, programs, sw_x, cpuc_admin):  # noqa: F811
    assert submissions.files_for(sw_deadline(), pge) and not submissions.files_for(sw_deadline(), programs)
    assert submissions.files_for(own_deadline(), programs)
    board = filing_status.board(cpuc_admin)
    i = next(i for i, c in enumerate(board.columns) if c.deadline == sw_deadline())  # claims' next due: the statewide Q1
    cells = {r.organisation.code: r.cells[i].status for r in board.rows}
    assert cells["SCE"] == "Nothing to file" and cells["PGE"] == "Not started"


def test_leads_load_from_cedars_statewide_programs_download(tmp_path, cpuc_admin, pge, programs):  # noqa: F811
    from django.core.management import call_command

    for prg in ("SW_A", "SW_B"):
        registry.create_program(cpuc_admin, prg_id=prg, administrator=None, statewide=True, valid_from=date(2020, 1, 1), name=prg)
    f = tmp_path / "statewide.csv"
    f.write_text(
        "LeadPA,PrgID,ProgramName,LaunchedYear,ClosedYear\n"
        "Pacific Gas & Electric (PGE),SW_A,A,2026,\nSouthern California Edison (SCE),SW_B,B,2026,\nNobody (XYZ),SW_C,C,2026,\n",
        encoding="utf-8-sig",
    )
    out = io.StringIO()
    call_command("import_statewide_leads", str(f), "--reason", "CEDARS list", stdout=out)
    assert "would change 2 leads; skipped 1" in out.getvalue() and Program.objects.get(prg_id="SW_A").lead is None
    call_command("import_statewide_leads", str(f), "--reason", "CEDARS list", "--apply", stdout=out)
    assert Program.objects.get(prg_id="SW_A").lead == pge and Program.objects.get(prg_id="SW_B").lead == programs
