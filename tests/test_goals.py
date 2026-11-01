import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as accounts
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import goals
from cedars.services.registry.models import Goal, GoalSet

HEADER = "Utility,Kind,FirstYear,LastYear,ProgramTSB,ProgramGWh,ProgramMW,ProgramMMTherms,CSGWh,CSMW,CSMMTherms\n"
# Made-up values in the decision tables' layout (not a real decision's)
GOALS = (
    HEADER
    + '"PG&E",cycle,2028,2031,"$1,000.5",500,90,10,300,50,5\n'
    + "PGE,annual,2028,2028,250,120,22,-,80,12,1\n"
    + "PGE,annual,2029,2029,260,125,23,-,82,12,1\n"
    + "SCE,annual,2028,2028,200,150,25,,90,15,\n"
)
RULES = "PA,Territory,Share,AppliesTo,Source\nMCE,PGE,1,programs,counting example\nBAYREN,PGE,1,all,\n"


@pytest.fixture
def second_admin(cpuc_admin, make_user):
    u = make_user("second@cpuc.example.org", "Sam Second")
    accounts.grant(cpuc_admin, u, accounts.cpuc(), perms.SYSTEM_ADMIN, reason="second administrator")
    return u


def _load(actor, content=GOALS, decision="D.00-00-001", rules=RULES, adopted=date(2026, 1, 1)):
    return goals.import_goals(
        actor, content.encode(), decision=decision, adopted=adopted, rules=rules.encode() if rules else None, source_name="goals.csv"
    )


def test_import_reads_the_decision_tables(cpuc_admin):
    s = _load(cpuc_admin)
    assert s.status == GoalSet.Status.DRAFT
    cycle = s.goals.get(kind="cycle")
    assert (cycle.utility, cycle.first_year, cycle.last_year, cycle.program_tsb) == ("PGE", 2028, 2031, Decimal("1000.5"))
    sce = s.goals.get(utility="SCE")
    assert sce.program_mmtherms is None and sce.cs_mmtherms is None  # blank = not set by the decision
    assert s.goals.get(utility="PGE", first_year=2028, kind="annual").program_mmtherms is None  # "-" too
    assert {(r.pa, r.applies_to) for r in s.counting_rules.all()} == {("MCE", "programs"), ("BAYREN", "all")}
    assert goals.check(s) == []
    assert AuditEvent.objects.filter(action="goal_set.imported").count() == 1


def test_import_refuses_a_bad_file(cpuc_admin):
    with pytest.raises(ValueError, match="missing columns"):
        _load(cpuc_admin, "Utility,Kind\nPGE,cycle\n")
    with pytest.raises(ValueError, match="not a number"):
        _load(cpuc_admin, HEADER + "PGE,annual,2028,2028,lots,1,1,1,1,1,1\n")
    _load(cpuc_admin)
    with pytest.raises(AccessDenied, match="loaded already"):
        _load(cpuc_admin)


def test_check_finds_problems(cpuc_admin):
    s = _load(
        cpuc_admin,
        HEADER
        + "XYZ,annual,2028,2028,1,1,1,1,1,1,1\n"
        + "PGE,annual,2028,2029,1,1,1,1,1,1,1\n"
        + "PGE,cycle,2028,2030,1,1,1,1,1,1,1\n"
        + "SCE,annual,2028,2028,-1,,,,,,\n"
        + "SDGE,annual,2028,2028,,,,,,,\n",
        rules="PA,Territory,Share\nMCE,PGE,1.5\n",
    )
    issues = {(i.where.split()[0], i.message, i.blocking) for i in goals.check(s)}
    assert ("XYZ", "XYZ is not an IOU territory (PGE, SCE, SDGE, SCG)", True) in issues
    assert ("PGE", "an annual target covers one year", True) in issues
    assert ("PGE", "a cycle goal covers four years", False) in issues
    assert ("SCE", "a negative value", True) in issues
    assert ("SDGE", "no values", True) in issues
    assert ("rule", "share 1.5 is outside 0 to 1", True) in issues


def test_a_second_administrator_confirms(cpuc_admin, second_admin, pge_admin):
    s = _load(cpuc_admin)
    assert goals.goals("annual", 2028) == {}  # a draft is never used
    with pytest.raises(AccessDenied, match="different CPUC system administrator"):
        goals.confirm(cpuc_admin, s)
    with pytest.raises(AccessDenied):
        goals.confirm(pge_admin, s)
    goals.confirm(second_admin, s)
    s.refresh_from_db()
    assert (s.status, s.confirmed_by) == (GoalSet.Status.CONFIRMED, second_admin)
    assert set(goals.goals("annual", 2028)) == {"PGE", "SCE"}
    assert goals.goals("cycle", 2030)["PGE"].program_tsb == Decimal("1000.5")
    with pytest.raises(AccessDenied, match="kept for the record"):
        goals.discard(cpuc_admin, s)


def test_problems_block_confirmation(cpuc_admin, second_admin):
    s = _load(cpuc_admin, HEADER + "XYZ,annual,2028,2028,1,1,1,1,1,1,1\n")
    with pytest.raises(AccessDenied, match="1 problems"):
        goals.confirm(second_admin, s)
    goals.discard(cpuc_admin, s, "wrong file")
    assert not GoalSet.objects.exists()
    _load(cpuc_admin)  # the decision can be loaded again once its draft is gone


def test_the_newest_set_replaces_earlier_goals_year_by_year(cpuc_admin, second_admin):
    old = goals.confirm(second_admin, _load(cpuc_admin))
    new = goals.confirm(
        second_admin,
        _load(
            cpuc_admin,
            HEADER + "PGE,annual,2029,2029,999,1,1,1,1,1,1\nPGE,annual,2030,2030,5,1,1,1,1,1,1\n",
            decision="D.00-00-002",
            rules="",
        ),
    )
    assert goals.goals("annual", 2028)["PGE"].goal_set == old  # the new decision does not set 2028
    assert goals.goals("annual", 2029)["PGE"].program_tsb == 999
    assert "SCE" not in goals.goals("annual", 2029)
    assert goals.in_force_years(old) == {"cycle": [2028, 2029, 2030, 2031], "annual": [2028]}
    assert goals.in_force_years(new) == {"cycle": [], "annual": [2029, 2030]}
    assert {r.pa for r in goals.counting_rules()} == {"MCE", "BAYREN"}  # the new set lists none, so the older rules stand


def test_goals_csv_round_trips(cpuc_admin):
    s = _load(cpuc_admin)
    rows = list(csv.DictReader(io.StringIO(goals.goals_csv(s))))
    assert len(rows) == 4 and rows[0]["Utility"] == "PGE"
    again = goals.import_goals(cpuc_admin, goals.goals_csv(s).encode(), decision="copy", adopted=date(2026, 1, 1))
    key = lambda g: (g.utility, g.kind, g.first_year)  # noqa: E731
    fields = [f for _, f in goals.METRICS]
    assert sorted((key(g), [getattr(g, f) for f in fields]) for g in again.goals.all()) == sorted(
        (key(g), [getattr(g, f) for f in fields]) for g in s.goals.all()
    )


def test_only_cpuc_admins_manage_goals(pge_admin):
    with pytest.raises(AccessDenied):
        _load(pge_admin)


def test_goal_pages(cpuc_admin, second_admin, pge_admin, signed_in):
    client = signed_in(cpuc_admin)
    response = client.post(
        "/admin/goals/",
        {
            "file": _upload("goals.csv", GOALS),
            "rules": _upload("rules.csv", RULES),
            "decision": "D.00-00-003",
            "adopted": "2026-01-01",
            "url": "https://example.org/decision.pdf",
            "notes": "",
        },
    )
    s = GoalSet.objects.get(decision="D.00-00-003")
    assert response.status_code == 302 and response["Location"] == f"/admin/goals/{s.pk}/"
    page = client.get(f"/admin/goals/{s.pk}/").content.decode()
    assert "D.00-00-003" in page and "1000.5" in page
    assert client.post(f"/admin/goals/{s.pk}/", {"action": "confirm"}).status_code == 302
    assert GoalSet.objects.get(pk=s.pk).status == GoalSet.Status.DRAFT  # refused: the loader cannot confirm
    signed_in(second_admin)
    client.post(f"/admin/goals/{s.pk}/", {"action": "confirm"})
    assert GoalSet.objects.get(pk=s.pk).status == GoalSet.Status.CONFIRMED
    listing = client.get("/admin/goals/").content.decode()
    assert "D.00-00-003" in listing
    download = client.get(f"/admin/goals/{s.pk}/goals.csv")
    assert download["Content-Type"].startswith("text/csv") and b"PGE,cycle,2028,2031" in download.content
    signed_in(pge_admin)
    assert client.get("/admin/goals/").status_code == 404  # as the other administration pages answer
    assert Goal.objects.count() == 4


def _upload(name, text):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")
