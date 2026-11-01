"""The program page's records (BR-07.6): budget filing lines by version, fund shifts, claims by confirmation, monthly reports and
documents, read through registry.program_records, each linked to its record and shown signed out only where the record is published."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from cedars.modules.budgets.models import BudgetLine, BudgetResult, FundShift, FundShiftLine
from cedars.modules.claims.models import ClaimProgramLine, ClaimResult
from cedars.modules.documents.models import AttributeOption, Document
from cedars.modules.monthly.models import ReportLine
from cedars.services.accounts import service as access
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, Submission, SubmissionVersion

pytestmark = pytest.mark.django_db
PUBLISHED, DRAFT = SubmissionVersion.Status.PUBLISHED, SubmissionVersion.Status.DRAFT
WHEN = datetime(2028, 6, 1, tzinfo=UTC)


def _version(submission, number, status, run=""):
    return SubmissionVersion.objects.create(
        submission=submission,
        number=number,
        status=status,
        calculation={"run": run} if run else {},
        published_at=WHEN if status == PUBLISHED else None,
    )


@pytest.fixture
def scene(pge, pge_admin, cpuc_admin):
    hvac = registry.create_program(
        pge_admin, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC", start_year=2028
    )
    registry.create_program(pge_admin, prg_id="PGE_Com_Ltg", administrator=pge, valid_from=date(2024, 1, 1), name="Commercial lighting")
    cycle = registry.ensure_cycle(2028)
    periods = {(p.kind, p.number): p for p in registry.ensure_periods(2028)}

    tual = Deadline.objects.create(module="budgets", kind="True-up advice letter (TUAL)", cycle=cycle, due_at=WHEN)
    sub = Submission.objects.create(deadline=tual, organisation=pge)
    b1, b2 = _version(sub, 1, PUBLISHED, run="r1"), _version(sub, 2, DRAFT)
    BudgetLine.objects.create(version=b1, prg_id="PGE_Res_HVAC", year=2028, program_costs={"EMV": "100"}, rebates_and_incentives=50)
    BudgetLine.objects.create(version=b1, prg_id="PGE_Res_HVAC", year=2029, program_costs={"EMV": "200"}, rebates_and_incentives=0)
    BudgetLine.objects.create(version=b1, prg_id="PGE_Com_Ltg", year=2028, program_costs={"EMV": "888"})
    BudgetLine.objects.create(version=b2, prg_id="PGE_Res_HVAC", year=2028, program_costs={"EMV": "777"})
    BudgetResult.objects.create(
        version=b1,
        run="r1",
        pa="PGE",
        prg_id="PGE_Res_HVAC",
        year=2028,
        values={"TotalSystemBenefit": 300.0, "TRCBen": 3.0, "TRCCost": 2.0},
    )
    BudgetResult.objects.create(version=b1, run="old", pa="PGE", prg_id="PGE_Res_HVAC", year=2028, values={"TotalSystemBenefit": 1e9})

    def shift(title, status, amount):
        s = FundShift.objects.create(
            organisation=pge, cycle=cycle, title=title, reason="r", status=status, created_by=pge_admin, effective_at=WHEN
        )
        FundShiftLine.objects.create(shift=s, pa="PGE", prg_id="PGE_Res_HVAC", year=2029, amount=Decimal(amount))
        FundShiftLine.objects.create(shift=s, pa="PGE", prg_id="PGE_Com_Ltg", year=2029, amount=-Decimal(amount))

    shift("Into residential HVAC", FundShift.Status.EFFECTIVE, "125")
    shift("Still a draft shift", FundShift.Status.DRAFT, "5")

    q1 = Deadline.objects.create(module="claims", kind="Q1 claim", period=periods[("quarter", 1)], due_at=WHEN)
    csub = Submission.objects.create(deadline=q1, organisation=pge)
    c1, c2 = _version(csub, 1, PUBLISHED, run="c1"), _version(csub, 2, PUBLISHED, run="c2")
    c3 = _version(csub, 3, DRAFT)
    for v, n in ((c1, 1), (c2, 3), (c3, 9)):
        ClaimProgramLine.objects.create(version=v, prg_id="PGE_Res_HVAC", pa="PGE", program_costs={"EMV": "10"}, claims=n)
    ClaimResult.objects.create(
        version=c2, run="c2", pa="PGE", prg_id="PGE_Res_HVAC", values={"FirstYearNetKWh": 12345.0, "TotalSystemBenefit": 678.0}
    )

    jan = Deadline.objects.create(module="monthly", kind="Monthly report", period=periods[("month", 1)], due_at=WHEN)
    msub = Submission.objects.create(deadline=jan, organisation=pge)
    m1 = _version(msub, 1, PUBLISHED)
    ReportLine.objects.create(
        version=m1, table="Spend", prg_id="PGE_Res_HVAC", values={"YTDExpenditures": "4321.40", "RebatesAndIncentives": "1234"}
    )
    ReportLine.objects.create(version=m1, table="Commitments", prg_id="PGE_Res_HVAC", values={"YTDCommittedFunds": "999"})

    kind = AttributeOption.objects.filter(attribute_list__collections__contains=["program"]).first() or AttributeOption.objects.first()
    for title, published in (("HVAC implementation plan", WHEN), ("HVAC plan, unpublished draft", None)):
        doc = Document.objects.create(
            collection="program", document_type=kind, title=title, owner=pge, published_at=published, first_year=2028, last_year=2031
        )
        doc.programs.add(hvac)
    return {"budget": b1, "draft": b2, "claims": c2}


def _page(client, query=""):
    r = client.get(f"/programs/PGE_Res_HVAC/{query}")
    assert r.status_code == 200
    return r.content.decode()


def test_signed_out_sees_only_published_records(client, scene):
    html = _page(client)
    assert f'href="/budgets/versions/{scene["budget"].pk}/"' in html
    assert "$150" in html and "$200" in html and "$350" in html  # 2028 (costs + incentives), 2029, total
    assert "$300" in html and "1.50" in html  # TSB and TRC from the version's own run, not an older one
    assert "$888" not in html and "$777" not in html  # another program's line; the draft version
    assert "Fund shifts are shown to the PAs involved" in html and "Into residential HVAC" not in html
    assert f'href="/claims/versions/{scene["claims"].pk}/"' in html  # the standing version of Q1, not the one it replaced
    assert "12,345" in html and "$678" in html and "$10" in html
    assert "/claims/versions/" in html and html.count("/claims/versions/") == 1
    assert "$4,321" in html and "$1,234" in html and "$999" in html  # spend, paid incentives (part of spend), committed
    assert "Paid incentives, year to date (part of spend)" in html and "Committed incentives, year to date (to be paid later)" in html
    assert "HVAC implementation plan" in html and "unpublished draft" not in html
    assert "sign in" in html


def test_the_pa_sees_drafts_and_shifts(signed_in, make_user, pge, pge_admin, scene):
    reader = make_user("reader@example.org")
    for code in ("budgets.read", "claims.read", "monthly.read"):
        access.grant(pge_admin, reader, pge, code, reason="test")
    html = _page(signed_in(reader))
    assert f'href="/budgets/versions/{scene["draft"].pk}/"' in html and "$777" in html
    assert "Into residential HVAC" in html and "Still a draft shift" in html and "$125" in html
    assert html.count("/claims/versions/") == 2  # the standing version and the draft being worked on


def test_another_signed_in_user_sees_published_only(signed_in, make_user, scene):
    html = _page(signed_in(make_user("someone@example.org")))
    assert "$777" not in html and "Into residential HVAC" not in html
    assert "No fund shift you can see" in html
    assert html.count("/claims/versions/") == 1


def test_cpuc_staff_see_unpublished_documents(signed_in, cpuc_admin, scene):
    assert "unpublished draft" in _page(signed_in(cpuc_admin))


def test_one_program_year(client, scene):
    html = _page(client, "?year=2029")
    assert f'href="/budgets/versions/{scene["budget"].pk}/?years=2029"' in html  # the year carries over to the filing's page
    assert "$200" in html and "$150" not in html
    assert "Not priced" in html  # no results for 2029
    assert "/claims/versions/" not in html and "No published claims include this program in 2029" in html
    assert "HVAC implementation plan" in html  # covers 2028 to 2031
    assert "Showing program year 2029 only" in html
    assert "/claims/versions/" in _page(client, "?year=2028")
    assert "Showing program year" not in _page(client, "?year=nope")


def test_records_are_read_through_the_registry(client, scene):
    from django.contrib.auth.models import AnonymousUser

    hvac = Program.objects.get(prg_id="PGE_Res_HVAC")
    records = registry.program_records(hvac, AnonymousUser())
    assert set(records) == set(registry.PROGRAM_SECTIONS)
    assert [r["version"] for r in records["budgets"]] == [1]
    assert records["fund_shifts"] == []
    with pytest.raises(ValueError):
        registry.register_program_records("nope", lambda *a: [])


def test_standing_numbers(scene):
    v = scene["claims"]
    assert submissions.standing_numbers([v.submission_id]) == {v.submission_id: (2, 3)}
