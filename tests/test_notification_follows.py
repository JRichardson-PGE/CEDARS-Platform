"""Following PAs and programs, refile notices with what changed, change letters for Budgets and Monthly reports, and deadline
reminders at the lead times each user chooses (BR-09.2, BR-09.3)."""

from datetime import date, timedelta

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone

from cedars.modules.budgets import service as budgets
from cedars.modules.budgets import shifts
from cedars.modules.budgets.models import ChangeLetter as BudgetLetter
from cedars.modules.calculations.models import Run
from cedars.modules.documents import service as documents
from cedars.modules.documents import subscriptions as doc_subscriptions
from cedars.modules.monthly import service as monthly
from cedars.modules.monthly.models import ChangeLetter as MonthlyLetter
from cedars.services.accounts import service as access
from cedars.services.notifications import service
from cedars.services.notifications.models import PROGRAM_EVENTS, DeadlineReminder, Notification, Preference
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline

from .conftest import sign_in
from .test_budgets import COSTS, finish_run, job, release  # noqa: F401 (release is a fixture)
from .test_documents import details, upload
from .test_monthly import report

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("release")]


@pytest.fixture
def sce(cpuc_admin):
    return access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")


@pytest.fixture
def programs(pge_admin, pge, cpuc_admin, sce):
    registry.create_program(pge_admin, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    registry.create_program(pge_admin, prg_id="PGE_Com_Ltg", administrator=pge, valid_from=date(2024, 1, 1), name="Commercial lighting")
    registry.create_program(cpuc_admin, prg_id="SCE_Ind", administrator=sce, valid_from=date(2024, 1, 1), name="Industrial")
    return {p.prg_id: p for p in Program.objects.all()}


@pytest.fixture
def filer(make_user, pge, pge_admin):
    u = make_user("filer@pge.example.org", "Fi Ler")
    for p in ("budgets.submit", "budgets.confirm", "monthly.submit", "monthly.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


@pytest.fixture
def filing(cpuc_admin):
    return budgets.add_filing(
        cpuc_admin, first_year=2028, kind="True-up advice letter (TUAL)", due_at=timezone.now() + timedelta(days=30), acc_version="2026"
    )


def published_budget(filer, filing, pge, data=None, scale=1.0, on_commit=None):
    v = budgets.upload(filer, filing, pge, data or job(), "tual.zip")
    finish_run(Run.objects.get(pk=budgets.price(filer, v)), scale)
    v.refresh_from_db()
    with on_commit(execute=True):
        return budgets.confirm(filer, v)


def corrected_budget(filer, filing, pge, cpuc_admin, v, data, on_commit, reason="PGE_Com_Ltg's 2028 marketing was left out"):
    submissions.move_deadline(cpuc_admin, filing, timezone.now() - timedelta(minutes=1), "closed")
    submissions.set_policy(cpuc_admin, "budgets", reopen_after_due="open")
    with on_commit(execute=True):
        submissions.request_reopen(filer, v.submission, reason)
        v2 = budgets.upload(filer, filing, pge, data, "correction.zip")
    finish_run(Run.objects.get(pk=budgets.price(filer, v2)), 1.0)
    v2.refresh_from_db()
    with on_commit(execute=True):
        return budgets.confirm(filer, v2)


CORRECTED = job(
    costs=[
        "PGE_Res_HVAC|2028|2028Q1|PGE|1000|500|200|0|0|0|0|100|0|5000|0",
        "PGE_Res_HVAC|2029|2029Q1|PGE|1100|500|200|0|0|0|0|100|0|0|0",
        "PGE_Com_Ltg|2028|2028Q1|PGE|300|0|900|0|0|0|0|0|0|0|0",
    ]
)


def titles(user) -> list[str]:
    return list(Notification.objects.filter(user=user).order_by("id").values_list("title", flat=True))


# --- following PAs


def test_the_pa_choice_narrows_what_subscribers_hear(filer, filing, pge, sce, programs, make_user, django_capture_on_commit_callbacks):
    everyone, pge_only, sce_only, nobody = (make_user(f"{n}@example.org") for n in ("everyone", "pge", "sce", "nobody"))
    for u in (everyone, pge_only, sce_only, nobody):
        service.subscribe(u, "submission.published", module="budgets")
        service.subscribe(u, "pack.published")
    service.set_follows(pge_only, pa_scope="chosen", pas=[pge])
    service.set_follows(sce_only, pa_scope="chosen", pas=[sce])
    service.set_follows(nobody, pa_scope="none")
    published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    got = set(Notification.objects.values_list("user__email", flat=True))
    assert got == {everyone.email, pge_only.email}
    n = Notification.objects.get(user=pge_only)
    assert n.title == "Budget filing published: PGE 2028-2031 True-up advice letter (TUAL)"
    assert n.body == "2 programs, budget $4,900 for 2028 to 2029." and n.link.startswith("/budgets/versions/")


def test_a_subscription_to_monthly_reports_does_not_bring_budget_filings(
    filer, filing, pge, programs, make_user, django_capture_on_commit_callbacks
):
    u = make_user("monthly-only@example.org")
    service.subscribe(u, "submission.published", module="monthly")
    published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    assert not Notification.objects.filter(user=u).exists()


# --- following programs, and refiles


def test_a_programs_follower_hears_of_filings_and_their_corrections(
    filer, filing, pge, cpuc_admin, programs, make_user, django_capture_on_commit_callbacks
):
    implementer = make_user("implementer@example.org", "Imp Lementer")  # no PA membership, no subscription
    service.set_follows(implementer, pa_scope="none")
    service.follow_program(implementer, programs["PGE_Com_Ltg"])
    other = make_user("industrial@example.org")
    service.follow_program(other, programs["SCE_Ind"])
    watcher = make_user("watcher@example.org")  # follows PG&E's filings
    service.subscribe(watcher, "submission.published", "in_app", module="budgets")
    service.set_follows(watcher, pa_scope="chosen", pas=[pge])

    v = published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    assert titles(implementer) == ["Budget filing published: PGE 2028-2031 True-up advice letter (TUAL)"]
    assert not Notification.objects.filter(user=other).exists()
    assert Notification.objects.get(user=implementer).email  # the programs' messages come by email unless the user chose otherwise

    v2 = corrected_budget(filer, filing, pge, cpuc_admin, v, CORRECTED, django_capture_on_commit_callbacks)
    letter = BudgetLetter.objects.get(version=v2)
    assert letter.previous == v and [p["prg_id"] for p in letter.changes["programs"]] == ["PGE_Com_Ltg"]
    assert letter.changes["programs"][0]["before"]["budget"] == 400 and letter.changes["programs"][0]["after"]["budget"] == 1300
    for u in (implementer, watcher):
        n = Notification.objects.filter(user=u).order_by("-id").first()
        assert n.title == "Corrected budget filing: PGE 2028-2031 True-up advice letter (TUAL), version 2"
        assert n.link == f"/budgets/changes/{letter.pk}/"
        assert "Reason: PGE_Com_Ltg's 2028 marketing was left out" in n.body
        assert "1 program changed." in n.body and "PGE_Com_Ltg: budget $400 to $1,300" in n.body
        assert "Total budget: $4,900 to $5,800." in n.body
    assert not Notification.objects.filter(user=other).exists()
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        BudgetLetter.objects.filter(pk=letter.pk).update(reason="changed")


def test_a_correction_that_changes_none_of_a_followed_program_tells_only_the_pas_subscribers(
    filer, filing, pge, cpuc_admin, programs, make_user, django_capture_on_commit_callbacks
):
    hvac = make_user("hvac@example.org")
    service.set_follows(hvac, pa_scope="none")
    service.follow_program(hvac, programs["PGE_Res_HVAC"])
    v = published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    corrected_budget(filer, filing, pge, cpuc_admin, v, CORRECTED, django_capture_on_commit_callbacks)
    assert len(titles(hvac)) == 1  # the first publication only: HVAC is unchanged by the correction


def test_a_follower_chooses_what_to_hear_about_the_programs(filer, filing, pge, programs, make_user, django_capture_on_commit_callbacks):
    u = make_user("picky@example.org")
    service.follow_program(u, programs["PGE_Com_Ltg"])
    service.set_follows(u, pa_scope="none", program_events=[k for k in PROGRAM_EVENTS if k != "submission.published:budgets"])
    published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    assert not Notification.objects.filter(user=u).exists()
    with pytest.raises(ValueError):
        service.set_follows(u, program_events=["pack.published"])


def test_a_fund_shift_in_effect_reaches_its_programs_followers(filer, filing, pge, programs, make_user, django_capture_on_commit_callbacks):
    u = make_user("ltg@example.org")
    service.set_follows(u, pa_scope="none")
    service.follow_program(u, programs["PGE_Com_Ltg"])
    published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    s = shifts.create(filer, pge, filing.cycle, title="Into lighting", reason="demand is up")
    lines = [("PGE_Com_Ltg", 2028, 150), ("PGE_Res_HVAC", 2028, -150)]
    shifts.set_lines(filer, s, [shifts.LineIn(p, str(y), str(a)) for p, y, a in lines])
    with django_capture_on_commit_callbacks(execute=True):
        shifts.submit(filer, s)  # no co-funder: in effect at once
    n = Notification.objects.filter(user=u).order_by("-id").first()
    assert n.title == "Fund shift in effect: PGE, Into lighting" and n.link == f"/budgets/shifts/{s.pk}/"
    assert n.body.splitlines() == ["Reason: demand is up", "PGE PGE_Com_Ltg 2028: +$150.00", "PGE PGE_Res_HVAC 2028: -$150.00"]


def test_a_programs_change_and_a_document_about_it_reach_its_followers(
    pge, pge_admin, programs, make_user, django_capture_on_commit_callbacks
):
    u = make_user("ltg@example.org")
    service.set_follows(u, pa_scope="none")
    service.follow_program(u, programs["PGE_Com_Ltg"])
    with django_capture_on_commit_callbacks(execute=True):
        registry.change_program(
            pge_admin, programs["PGE_Com_Ltg"], effective=date(2027, 1, 1), reason="new implementer", implementer="Lumen Co"
        )
    n = Notification.objects.get(user=u)
    assert n.title == "PGE_Com_Ltg has changed" and n.link == "/programs/PGE_Com_Ltg/"
    assert "Changed: implementer." in n.body and "In effect from 2027-01-01." in n.body and "Reason: new implementer" in n.body

    writer = make_user("writer@pge.example.org")
    for p in ("documents.submit", "documents.confirm"):
        access.grant(pge_admin, writer, pge, p)
    doc_subscriptions.subscribe(u, collection="pa")  # also matches: still one message
    with django_capture_on_commit_callbacks(execute=True):
        doc = documents.create_document(
            writer, pge, "pa", details("pa", "Cap and Target Report", programs=[programs["PGE_Com_Ltg"]]), [upload()]
        )
        documents.publish_version(writer, doc.versions.get())
    published = Notification.objects.filter(user=u, title=f"Published: {doc.title}")
    assert published.count() == 1 and published.get().link == doc.get_absolute_url()


def test_the_program_page_follows_and_stops_following(client, pge_admin, programs, make_user):
    u = make_user("someone@example.org")
    sign_in(client, u)
    page = client.get("/programs/PGE_Com_Ltg/").content.decode()
    assert "Follow this program" in page
    r = client.post("/notifications/follow/program/", {"prg_id": "PGE_Com_Ltg", "next": "/programs/PGE_Com_Ltg/"})
    assert r.status_code == 302 and r["Location"] == "/programs/PGE_Com_Ltg/"
    assert service.follows(u).programs == {"PGE_Com_Ltg"}
    page = client.get("/programs/PGE_Com_Ltg/").content.decode()
    assert "Stop following" in page and "You follow this program" in page
    client.post("/notifications/follow/program/", {"prg_id": "PGE_Com_Ltg", "action": "unfollow", "next": "https://elsewhere.example/"})
    assert service.follows(u).programs == set()
    client.logout()
    assert "Sign in to follow this program" in client.get("/programs/PGE_Com_Ltg/").content.decode()


# --- monthly reports


def test_a_monthly_report_correction_has_a_change_letter_and_a_notice(
    filer, pge, cpuc_admin, programs, make_user, client, django_capture_on_commit_callbacks
):
    monthly.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=20)
    Deadline.objects.filter(module="monthly").update(opens_at=None, due_at=timezone.now() + timedelta(days=30))
    january = monthly.months(2028).get(period__number=1)
    u = make_user("ltg@example.org")
    service.set_follows(u, pa_scope="none")
    service.follow_program(u, programs["PGE_Com_Ltg"])

    v = monthly.upload(filer, january, pge, report(measures=[]), "january.zip")
    assert monthly.price(filer, v) == "none"
    v.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        v = monthly.confirm(filer, v)
    assert titles(u) == ["Monthly report published: PGE January 2028 report"]

    submissions.move_deadline(cpuc_admin, january, timezone.now() - timedelta(minutes=1), "closed")
    submissions.set_policy(cpuc_admin, "monthly", reopen_after_due="open")
    with django_capture_on_commit_callbacks(execute=True):
        submissions.request_reopen(filer, v.submission, "a late lighting invoice")
        lighting = "PGE_Com_Ltg|2028|2028Q1|PGE|450|0|0|0|0|0|0|0|0|50|0"
        v2 = monthly.upload(filer, january, pge, report(measures=[], costs=[lighting, *_hvac()]), "correction.zip")
    assert monthly.price(filer, v2) == "none"
    v2.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        v2 = monthly.confirm(filer, v2)
    letter = MonthlyLetter.objects.get(version=v2)
    (p,) = letter.changes["programs"]
    assert p["prg_id"] == "PGE_Com_Ltg" and p["before"]["YTDExpenditures"] == 200 and p["after"]["YTDExpenditures"] == 450
    assert p["before"]["RebatesAndIncentives"] == 0 and "YTDCommittedFunds" in p["before"]  # paid and committed, apart
    n = Notification.objects.filter(user=u).order_by("-id").first()
    assert n.title == "Corrected monthly report: PGE January 2028 report, version 2" and n.link == f"/monthly/changes/{letter.pk}/"
    assert "PGE_Com_Ltg: spend, year to date $200 to $450" in n.body

    page = client.get(f"/monthly/changes/{letter.pk}/").content.decode()  # public
    assert "a late lighting invoice" in page and "+$250" in page and monthly.PAID in page and monthly.COMMITTED in page
    assert f'href="/monthly/changes/{letter.pk}/"' in client.get("/monthly/changes/").content.decode()


def _hvac():
    return ["PGE_Res_HVAC|2028|2028Q1|PGE|600|0|0|0|0|0|0|0|0|0|0"]


def test_the_budgets_change_log_is_public(filer, filing, pge, cpuc_admin, programs, client, django_capture_on_commit_callbacks):
    v = published_budget(filer, filing, pge, on_commit=django_capture_on_commit_callbacks)
    v2 = corrected_budget(filer, filing, pge, cpuc_admin, v, CORRECTED, django_capture_on_commit_callbacks)
    letter = BudgetLetter.objects.get(version=v2)
    log = client.get("/budgets/changes/").content.decode()
    assert "PGE 2028-2031 True-up advice letter (TUAL), version 2" in log
    page = client.get(f"/budgets/changes/{letter.pk}/").content.decode()
    assert "PGE_Com_Ltg" in page and "+$900" in page and "PGE_Res_HVAC" not in page
    assert client.get("/budgets/changes/?org=SCE").status_code == 200


# --- deadline reminders


def test_reminders_come_at_each_users_own_lead_time(cpuc_admin, pge_admin, make_user):
    quarter = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    early = make_user("early@example.org")
    service.set_reminders(early, [(30, "budgets", "in_app"), (2, "", "email")])
    due = timezone.make_aware(timezone.datetime(2028, 3, 21, 17, 0))
    claims = submissions.add_deadline(cpuc_admin, module="claims", kind="Q1 claim", period=quarter, due_at=due)
    budget = submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", period=quarter, due_at=due)
    assert service.send_deadline_reminders(date(2028, 2, 1)) == 0  # 49 days out
    assert service.send_deadline_reminders(date(2028, 3, 1)) == 1  # 20 days: the budgets reminder (claims only at 2 days)
    n = Notification.objects.get(user=early)
    assert n.title == "Due in 20 days: Budgets TUAL 2028Q1, 2028-03-21" and not n.email
    assert service.send_deadline_reminders(date(2028, 3, 19)) == 2  # 2 days: both deadlines
    assert service.send_deadline_reminders(date(2028, 3, 20)) == 0
    submissions.move_deadline(cpuc_admin, claims, due + timedelta(days=7), "extended")
    assert service.send_deadline_reminders(date(2028, 3, 27)) == 1  # a moved deadline is reminded of again
    assert Notification.objects.filter(user=early).order_by("-id").first().title.startswith("Due tomorrow")
    assert budget.pk and DeadlineReminder.objects.filter(user=early).count() == 2
    with pytest.raises(ValueError):
        service.set_reminders(early, [(400, "", "email")])


def test_the_old_fixed_reminders_become_reminders_of_7_and_1_days(pge_admin):
    import importlib

    from django.apps import apps

    from cedars.services.notifications.models import Subscription

    m = importlib.import_module("cedars.services.notifications.migrations.0002_follows_and_reminders")
    Subscription.objects.create(user=pge_admin, event="deadline.reminder", channel="in_app")
    Subscription.objects.create(user=pge_admin, event="submission.published", channel="email")
    m.forward(apps, None)
    assert sorted(DeadlineReminder.objects.filter(user=pge_admin).values_list("days_before", "channel")) == [(1, "in_app"), (7, "in_app")]
    assert sorted(Subscription.objects.filter(user=pge_admin).values_list("event", "module")) == [
        ("submission.published", "budgets"),
        ("submission.published", "claims"),
        ("submission.published", "monthly"),
    ]


# --- the settings page


def test_the_settings_page_saves_pas_programs_and_reminders(client, pge, sce, pge_admin, programs):
    sign_in(client, pge_admin)
    page = client.get("/notifications/settings/").content.decode()
    for text in ("PAs you follow", "Programs you follow", "Deadline reminders", "Southern California Edison (SCE)"):
        assert text in page
    r = client.post(
        "/notifications/settings/",
        {
            "follows": "1",
            "reminders": "1",
            "reminder_rows": "3",
            "pa_scope": "chosen",
            "pa": [str(sce.pk)],
            "add_programs": "pge_com_ltg, NOPE_1\nSCE_Ind",
            "program_event": ["submission.published:claims", "program.changed"],
            "program_channel": "in_app",
            "r0_days": "14",
            "r0_module": "claims",
            "r0_channel": "email",
            "r1_days": "",
            "r2_days": "0",
            "r2_module": "",
            "r2_channel": "in_app",
            "submission.published:claims": "email",
        },
        follow=True,
    )
    text = r.content.decode()
    assert "no program has the PrgID NOPE_1" in text
    pref = Preference.objects.get(user=pge_admin)
    assert pref.pa_scope == "chosen" and list(pref.pas.all()) == [sce]
    assert set(pref.programs.values_list("prg_id", flat=True)) == {"PGE_Com_Ltg", "SCE_Ind"}
    assert pref.program_events == ["submission.published:claims", "program.changed"] and pref.program_channel == "in_app"
    assert sorted(DeadlineReminder.objects.filter(user=pge_admin).values_list("days_before", "module", "channel")) == [
        (0, "", "in_app"),
        (14, "claims", "email"),
    ]
    # unticking a program stops following it; a bad number of days leaves the reminders as they were
    client.post(
        "/notifications/settings/",
        {"follows": "1", "reminders": "1", "reminder_rows": "1", "pa_scope": "all", "program": ["SCE_Ind"], "r0_days": "abc"},
    )
    assert service.follows(pge_admin).programs == {"SCE_Ind"} and service.follows(pge_admin).scope == "all"
    assert DeadlineReminder.objects.filter(user=pge_admin).count() == 2
