"""Testing: the acceptance test scripts run on the site itself. A test lead (a CPUC system administrator) loads the scripts as a
version (manage.py load_test_scripts; the scripts are data, never part of the code), opens a round of testing (a campaign) and assigns
each script to people's own accounts. A tester finds the assignment in My queue, steps through one test at a time and records Passed,
Failed, Blocked or Does not apply with what happened; each answer is saved as it is given, so they can leave and come back.
A failed or blocked result is also sent, from the tester, to the requests inbox as a finding (the Feedback service): one inbox
for what people report and what testing finds, with the same conversation and the same way on to a work item.

The scripts have a tester sign in to the site as shared test accounts, and some tests run signed out, so a run cannot depend on who is
signed in. Opening a run as yourself leaves a run link in that browser (a signed cookie); the run then stays open, and every result
stays under your own name, whichever account the site is signed in to. The same link, shown on the run's page, opens the run in
another browser or window.

A release plan's acceptance testing (the Work items service) opens a round here and reads back how its tests were answered
(`newest_version`, `find_tests`, `outcomes_by_test`); this service knows nothing of work items.

Testing is on where CEDARS_TESTING is set (the sandbox, staging and demo sites), never in production."""

import hashlib
import json
from collections import Counter

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from cedars.core import queue as my_queue
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.feedback import service as requests
from cedars.services.notifications import service as notifications

from .models import Assignment, Campaign, Draft, Finding, Result, Script, ScriptVersion, TestCase

SALT = "cedars.testing.run"
COOKIE = "cedars_testing"
COOKIE_LINKS = 12  # the most run links one browser keeps
SIGNED_OUT = ("", "none", "signed out")


class TestingRefused(ValueError):
    """The action cannot be taken; str(e) says why, as the page shows it."""


def enabled() -> bool:
    return bool(getattr(settings, "CEDARS_TESTING", False))


def can_manage(user) -> bool:
    return enabled() and access.is_system_admin(user)


def _require_lead(actor) -> None:
    if not enabled():
        raise AccessDenied("Testing is not switched on for this site.")
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can set up testing.")


# ---- the scripts


def _text(value, what: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TestingRefused(f"{what} must be text.")
    value = value.strip()
    if required and not value:
        raise TestingRefused(f"{what} is missing.")
    return value


def _texts(value, what: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TestingRefused(f"{what} must be a list of text.")
    return [v.strip() for v in value if v.strip()]


@transaction.atomic
def load_scripts(actor, data: dict, label: str) -> ScriptVersion:
    """Load the scripts file (the uat.json the script builder writes) as a new version. The whole file is checked first: nothing is
    kept of a file with a fault."""
    label = (label or "").strip()
    if not label:
        raise TestingRefused("Give the version a label.")
    if ScriptVersion.objects.filter(label__iexact=label).exists():
        raise TestingRefused(f"There is already a version labeled {label}. Versions are never replaced: choose another label.")
    if not isinstance(data, dict) or not isinstance(data.get("scripts"), list) or not data["scripts"]:
        raise TestingRefused('The file must be an object with a "scripts" list.')
    about = {k: v for k, v in data.items() if k not in ("scripts", "site")}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    version = ScriptVersion.objects.create(
        label=label, site=_text(data.get("site"), "site").rstrip("/"), about=about, sha256=digest, loaded_by=actor
    )
    seen: set[str] = set()
    count = 0
    for si, s in enumerate(data["scripts"]):
        if not isinstance(s, dict):
            raise TestingRefused(f"Script {si + 1} is not an object.")
        code = _text(s.get("code"), f"Script {si + 1}'s code", required=True)
        accounts = s.get("accounts") or []
        if not isinstance(accounts, list):
            raise TestingRefused(f"Script {code}'s accounts must be a list.")
        script = Script.objects.create(
            version=version,
            code=code,
            number=_text(s.get("num"), f"Script {code}'s number"),
            title=_text(s.get("title"), f"Script {code}'s title", required=True),
            who=_text(s.get("who"), f"Script {code}'s who"),
            scope=_text(s.get("scope"), f"Script {code}'s scope"),
            accounts=[[str(part) for part in a] if isinstance(a, (list, tuple)) else [str(a)] for a in accounts],
            before=_texts(s.get("before"), f"Script {code}'s before"),
            order_notes=_texts(s.get("order"), f"Script {code}'s order"),
            position=si,
        )
        position = 0
        for section in s.get("sections") or []:
            if not isinstance(section, dict):
                raise TestingRefused(f"A section of script {code} is not an object.")
            title = _text(section.get("title"), f"A section title in script {code}")
            for t in section.get("tests") or []:
                if not isinstance(t, dict):
                    raise TestingRefused(f"A test of script {code} is not an object.")
                test_id = _text(t.get("id"), f"A test ID in script {code}", required=True)
                if test_id in seen:
                    raise TestingRefused(f"Test {test_id} is in the file twice.")
                seen.add(test_id)
                steps = _texts(t.get("steps"), f"Test {test_id}'s steps")
                if not steps:
                    raise TestingRefused(f"Test {test_id} has no steps.")
                TestCase.objects.create(
                    script=script,
                    test_id=test_id,
                    section=title,
                    title=_text(t.get("title"), f"Test {test_id}'s title", required=True),
                    priority=_text(t.get("pri"), f"Test {test_id}'s priority"),
                    account=_text(t.get("acct"), f"Test {test_id}'s account"),
                    precondition=_text(t.get("pre"), f"Test {test_id}'s precondition"),
                    steps=steps,
                    expected=_text(t.get("exp"), f"Test {test_id}'s expected result", required=True),
                    position=position,
                )
                position += 1
                count += 1
        if not position:
            raise TestingRefused(f"Script {code} has no tests.")
    audit.record(
        "testing.scripts_loaded",
        obj=version,
        after={"label": label, "scripts": len(data["scripts"]), "tests": count, "sha256": digest},
        actor=actor,
        actor_label="" if actor else "command line",
    )
    return version


# ---- rounds of testing and assignments


@transaction.atomic
def create_campaign(actor, *, name: str, version: ScriptVersion, starts_on, ends_on=None, note: str = "") -> Campaign:
    _require_lead(actor)
    name = (name or "").strip()
    if not name:
        raise TestingRefused("Give the round of testing a name.")
    if Campaign.objects.filter(name__iexact=name).exists():
        raise TestingRefused(f"There is already a round of testing named {name}.")
    if ends_on and ends_on < starts_on:
        raise TestingRefused("The last day cannot be before the first day.")
    c = Campaign.objects.create(
        name=name, version=version, starts_on=starts_on, ends_on=ends_on, note=(note or "").strip(), created_by=actor
    )
    audit.record(
        "testing.campaign_opened",
        obj=c,
        after={"name": c.name, "scripts": version.label, "starts_on": str(starts_on), "ends_on": str(ends_on or "")},
        actor=actor,
    )
    return c


@transaction.atomic
def close_campaign(actor, campaign: Campaign, reason: str = "") -> Campaign:
    _require_lead(actor)
    c = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if not c.is_open:
        raise TestingRefused("That round of testing is already closed.")
    c.closed_at, c.closed_by = timezone.now(), actor
    c.save(update_fields=["closed_at", "closed_by"])
    audit.record("testing.campaign_closed", obj=c, before={"open": True}, after={"open": False}, reason=reason.strip(), actor=actor)
    return c


@transaction.atomic
def reopen_campaign(actor, campaign: Campaign, reason: str) -> Campaign:
    _require_lead(actor)
    if not (reason or "").strip():
        raise TestingRefused("Give a reason for reopening it.")
    c = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if c.is_open:
        raise TestingRefused("That round of testing is open.")
    c.closed_at, c.closed_by = None, None
    c.save(update_fields=["closed_at", "closed_by"])
    audit.record("testing.campaign_reopened", obj=c, before={"open": False}, after={"open": True}, reason=reason.strip(), actor=actor)
    return c


@transaction.atomic
def assign(actor, campaign: Campaign, script: Script, tester) -> Assignment:
    _require_lead(actor)
    if not campaign.is_open:
        raise TestingRefused("That round of testing is closed.")
    if script.version_id != campaign.version_id:
        raise TestingRefused("That script is not in this round's version of the scripts.")
    if not tester.is_active:
        raise TestingRefused(f"{tester.email}'s account is deactivated.")
    if Assignment.objects.filter(campaign=campaign, script=script, tester=tester, withdrawn_at__isnull=True).exists():
        raise TestingRefused(f"{tester.name or tester.email} already has that script in this round.")
    a = Assignment.objects.create(campaign=campaign, script=script, tester=tester, assigned_by=actor)
    audit.record(
        "testing.assigned",
        obj=a,
        after={"campaign": campaign.name, "script": script.code, "tester": tester.email},
        actor=actor,
    )
    tests = script.tests.count()
    transaction.on_commit(
        lambda: notifications.notify(
            tester,
            f"Testing assigned to you: {script}",
            f"{campaign.name}: script {script} ({tests} tests) is assigned to you. Open it from My queue; your answers are saved as "
            "you give them, so you can stop and come back.",
            f"/testing/runs/{a.pk}/",
            key=f"testing:{a.pk}",
        )
    )
    return a


@transaction.atomic
def withdraw(actor, assignment: Assignment, reason: str) -> Assignment:
    _require_lead(actor)
    if not (reason or "").strip():
        raise TestingRefused("Give a reason for withdrawing it.")
    a = Assignment.objects.select_for_update().get(pk=assignment.pk)
    if a.withdrawn_at:
        raise TestingRefused("That assignment is already withdrawn.")
    a.withdrawn_at, a.withdrawn_by, a.withdrawn_reason = timezone.now(), actor, reason.strip()
    a.save(update_fields=["withdrawn_at", "withdrawn_by", "withdrawn_reason"])
    audit.record(
        "testing.assignment_withdrawn", obj=a, before={"withdrawn": False}, after={"withdrawn": True}, reason=reason.strip(), actor=actor
    )
    return a


# ---- run links


def run_token(assignment: Assignment) -> str:
    # no time stamp in it: the same link every time it is shown, until the tester asks for a new one
    return signing.Signer(salt=SALT).sign_object([assignment.pk, assignment.link_number])


def assignment_of(token: str) -> Assignment | None:
    """The assignment a run link opens, or None when the link is not ours, was replaced, or its assignment was withdrawn."""
    try:
        pk, number = signing.Signer(salt=SALT).unsign_object(token)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    a = Assignment.objects.select_related("campaign", "script", "tester").filter(pk=pk).first()
    if a is None or a.link_number != number or a.withdrawn_at:
        return None
    return a


def cookie_tokens(raw: str) -> list[str]:
    return [t for t in (raw or "").split("|") if t][:COOKIE_LINKS]


def holds_link(raw: str, assignment: Assignment) -> bool:
    """Whether a browser's cookie holds this assignment's current run link."""
    return any(a is not None and a.pk == assignment.pk for a in map(assignment_of, cookie_tokens(raw)))


def with_link(raw: str, assignment: Assignment) -> str:
    """The cookie's value with this assignment's link in front (older links of the same assignment dropped)."""
    keep = []
    for t in cookie_tokens(raw):
        other = assignment_of(t)
        if other is not None and other.pk != assignment.pk:
            keep.append(t)
    return "|".join([run_token(assignment), *keep][:COOKIE_LINKS])


@transaction.atomic
def new_link(tester, assignment: Assignment) -> Assignment:
    """The tester replaces their run link: the old one stops working everywhere."""
    if assignment.tester_id != tester.pk:
        raise AccessDenied("Only the tester can replace their run link.")
    a = Assignment.objects.select_for_update().get(pk=assignment.pk)
    a.link_number += 1
    a.save(update_fields=["link_number"])
    audit.record("testing.run_link_replaced", obj=a, after={"link_number": a.link_number}, actor=tester)
    return a


# ---- results


@transaction.atomic
def record(assignment: Assignment, test: TestCase, *, outcome: str, comment: str = "", step_notes=None) -> Result:
    """Save the tester's answer to one test. The result is the tester's whoever is signed in to the site: `assignment` came from their
    own sign-in or their run link."""
    a = _open_for_results(assignment, test)
    if outcome not in Result.Outcome.values:
        raise TestingRefused("Choose a result.")
    comment = (comment or "").strip()
    limit = settings.CEDARS_COMMENT_MAX_CHARS
    if len(comment) > limit:
        raise TestingRefused(f"What happened can be at most {limit:,} characters; yours has {len(comment):,}.")
    if outcome in (Result.Outcome.FAILED, Result.Outcome.BLOCKED) and not comment:
        raise TestingRefused("Say what happened, so the problem can be found again.")
    notes = _notes(test, step_notes)
    result = Result.objects.select_for_update().filter(assignment=a, test=test).first()
    before = {"outcome": result.outcome} if result else None
    if result is None:
        result = Result(assignment=a, test=test)
    changed = (result.outcome, result.comment, result.step_notes) != (outcome, comment, notes)
    result.outcome, result.comment, result.step_notes = outcome, comment, notes
    if changed or result.pk is None:
        result.save()
    if before is None or before["outcome"] != outcome:
        audit.record(
            "testing.result_recorded",
            obj=result,
            before=before,
            after={"test": test.test_id, "outcome": outcome, "campaign": a.campaign.name},
            actor=a.tester,
        )
    Draft.objects.filter(assignment=a, test=test).delete()
    _keep_finding(a, test, result, changed)
    return result


# ---- findings: a failed or blocked result is a request in the one inbox


def finding_text(result: Result) -> str:
    """What the request says: the test, the round, the result, what the tester wrote and what was expected."""
    a, test = result.assignment, result.test
    lines = [
        f"Test {test.test_id}: {test.title}",
        f"Round of testing: {a.campaign.name}. Script: {test.script}." + (f" Priority: {test.priority}." if test.priority else ""),
        f"Result: {result.get_outcome_display()}",
        "",
        "What happened:",
        result.comment,
    ]
    notes = step_notes_text(result)
    if notes:
        lines += ["", "Notes on the steps:", notes]
    lines += ["", "Expected:", test.expected]
    text = "\n".join(lines)
    limit = settings.CEDARS_FEEDBACK_MAX_CHARS
    return text if len(text) <= limit else text[: limit - 30].rstrip() + "\n(cut short: see the result)"


def finding_of(result: Result):
    """The request a result's finding is, or None (never sent, or its retention period ended)."""
    finding = Finding.objects.filter(result=result).select_related("feedback").first()
    return finding.feedback if finding else None


def _keep_finding(a: Assignment, test: TestCase, result: Result, changed: bool) -> None:
    """Keep the result's finding in step with it: a failed or blocked result has an open request from the tester; saving it again
    with other words adds them to the request; a result that now passes (or does not apply) says so and closes the request."""
    fb = finding_of(result)
    is_open = fb is not None and fb.is_open
    if result.needs_attention:
        if not is_open:
            fb = requests.submit(
                page=f"/testing/runs/{a.pk}/{test.test_id}/",
                message=finding_text(result),
                user=a.tester,
                kind=requests.Feedback.Kind.FINDING,
                limited=False,
            )
            Finding.objects.update_or_create(result=result, defaults={"feedback": fb})
            audit.record("testing.finding_sent", obj=result, after={"test": test.test_id, "request": fb.pk}, actor=a.tester)
        elif changed:
            again = "The tester saved this result again.\n\n" + finding_text(result)
            requests.reply(a.tester, fb, again[: settings.CEDARS_FEEDBACK_MAX_CHARS])
    elif is_open:
        requests.reply(a.tester, fb, f"The tester saved this test again, and the result is now: {result.get_outcome_display()}.")
        requests.settle(a.tester, fb)


def _open_for_results(assignment: Assignment, test: TestCase) -> Assignment:
    if not enabled():
        raise TestingRefused("Testing is not switched on for this site.")
    a = Assignment.objects.select_related("campaign", "tester").get(pk=assignment.pk)
    if a.withdrawn_at:
        raise TestingRefused("This script is no longer assigned to you.")
    if not a.campaign.is_open:
        raise TestingRefused(f"{a.campaign.name} is closed: its results can no longer be changed.")
    if test.script_id != a.script_id:
        raise TestingRefused("That test is not in this script.")
    return a


def _notes(test: TestCase, step_notes) -> list[str]:
    notes = [(n or "").strip()[:500] for n in (step_notes or [])][: len(test.steps)]
    notes += [""] * (len(test.steps) - len(notes))
    return notes if any(notes) else []


# ---- drafts: what a tester has typed and not yet saved as a result


@transaction.atomic
def save_draft(assignment: Assignment, test: TestCase, *, outcome: str = "", comment: str = "", step_notes=None) -> Draft | None:
    """Keep what the tester has typed so far. Nothing is checked beyond its size, nothing is counted and no audit event is written:
    it becomes a result only when the tester saves it. A draft that says what the saved result already says (or nothing) is dropped."""
    a = _open_for_results(assignment, test)
    outcome = outcome if outcome in Result.Outcome.values else ""
    comment = (comment or "").strip()[: settings.CEDARS_COMMENT_MAX_CHARS]
    notes = _notes(test, step_notes)
    result = Result.objects.filter(assignment=a, test=test).first()
    saved = (result.outcome, result.comment, result.step_notes) if result else ("", "", [])
    if (outcome, comment, notes) == saved:
        Draft.objects.filter(assignment=a, test=test).delete()
        return None
    draft, _ = Draft.objects.update_or_create(
        assignment=a, test=test, defaults={"outcome": outcome, "comment": comment, "step_notes": notes}
    )
    return draft


def draft_of(assignment: Assignment, test: TestCase) -> Draft | None:
    return Draft.objects.filter(assignment=assignment, test=test).first()


def discard_draft(assignment: Assignment, test: TestCase) -> None:
    Draft.objects.filter(assignment=assignment, test=test).delete()


def drafted(assignment: Assignment) -> set[int]:
    """The tests (their keys) of an assignment with something typed and not saved."""
    return set(Draft.objects.filter(assignment=assignment).values_list("test_id", flat=True))


OUTCOME_ORDER = ("passed", "failed", "blocked", "na")


def progress(assignment: Assignment) -> dict:
    """Counts for one assignment: its tests, how many have a result, and how many of each outcome."""
    total = TestCase.objects.filter(script_id=assignment.script_id).count()
    counts = Counter(Result.objects.filter(assignment=assignment).values_list("outcome", flat=True))
    done = sum(counts.values())
    return {
        "total": total,
        "done": done,
        "left": max(total - done, 0),
        "percent": round(100 * done / total) if total else 0,
        **{key: counts.get(key, 0) for key in OUTCOME_ORDER},
    }


def next_test(assignment: Assignment, after: TestCase | None = None) -> TestCase | None:
    """The next test without a result (after a given test first, then from the top), or None when every test has one."""
    done = Result.objects.filter(assignment=assignment).values_list("test_id", flat=True)
    left = TestCase.objects.filter(script_id=assignment.script_id).exclude(pk__in=list(done)).order_by("position", "pk")
    if after is not None:
        later = left.filter(position__gt=after.position).first()
        if later:
            return later
    return left.first()


def _open_first():
    return F("campaign__closed_at").asc(nulls_first=True)


def mine(user):
    """A person's assignments that are not withdrawn, the open rounds first."""
    return (
        Assignment.objects.filter(tester=user, withdrawn_at__isnull=True)
        .select_related("campaign", "script", "script__version")
        .order_by(_open_first(), "-campaign__starts_on", "script__position", "pk")
    )


def waiting_on(user) -> list:
    """My queue: the user's assignments in open rounds that still have tests without a result."""
    if not enabled():
        return []
    items = []
    for a in mine(user).filter(campaign__closed_at__isnull=True):
        p = progress(a)
        if not p["left"]:
            continue
        items.append(
            my_queue.Item(
                what=f"{a.campaign.name}: script {a.script}",
                url=f"/testing/runs/{a.pk}/",
                since=a.assigned_at,
                due=a.campaign.ends_on,
                note=f"{p['done']} of {p['total']} tests done.",
            )
        )
    return items


def is_signed_out_test(test: TestCase) -> bool:
    return test.account.strip().lower() in SIGNED_OUT


# ---- a round's results, for the test lead

OUTCOME_WORDS = dict(Result.Outcome.choices)
SHOW = {
    "attention": ("Failed and blocked", ("failed", "blocked")),
    "failed": ("Failed", ("failed",)),
    "blocked": ("Blocked", ("blocked",)),
    "na": ("Does not apply", ("na",)),
    "passed": ("Passed", ("passed",)),
}


def summary(campaign: Campaign) -> dict:
    """A round's results by script, over its assignments that are not withdrawn: testers, tests to do (every tester's), how many
    have a result and how many of each outcome; then the same over the whole round."""
    counts: dict[int, Counter] = {}
    for script, outcome in Result.objects.filter(assignment__campaign=campaign, assignment__withdrawn_at__isnull=True).values_list(
        "assignment__script_id", "outcome"
    ):
        counts.setdefault(script, Counter())[outcome] += 1
    testers = Counter(campaign.assignments.filter(withdrawn_at__isnull=True).values_list("script_id", flat=True))
    rows = []
    for script in campaign.version.scripts.all():
        c = counts.get(script.pk, Counter())
        total = script.tests.count() * testers.get(script.pk, 0)
        done = sum(c.values())
        rows.append(
            {
                "script": script,
                "testers": testers.get(script.pk, 0),
                "total": total,
                "done": done,
                "left": max(total - done, 0),
                **{key: c.get(key, 0) for key in OUTCOME_ORDER},
            }
        )
    keys = ("testers", "total", "done", "left", *OUTCOME_ORDER)
    whole = {key: sum(r[key] for r in rows) for key in keys}
    whole["testers"] = campaign.assignments.filter(withdrawn_at__isnull=True).values("tester").distinct().count()
    whole["percent"] = round(100 * whole["done"] / whole["total"]) if whole["total"] else 0
    return {"rows": rows, "whole": whole}


def results(campaign: Campaign, outcomes=None, script: Script | None = None):
    """The results of a round's assignments that are not withdrawn, in the scripts' order, the testers' within a test."""
    qs = Result.objects.filter(assignment__campaign=campaign, assignment__withdrawn_at__isnull=True).select_related(
        "test", "test__script", "assignment", "assignment__tester", "finding__feedback"
    )
    if outcomes:
        qs = qs.filter(outcome__in=outcomes)
    if script is not None:
        qs = qs.filter(test__script=script)
    return qs.order_by("test__script__position", "test__position", "assignment__tester__email", "pk")


def newest_version() -> ScriptVersion | None:
    """The test scripts as last loaded; None where none are."""
    return ScriptVersion.objects.first()


def find_tests(version: ScriptVersion | None, test_ids) -> dict[str, TestCase]:
    """The tests of a version of the scripts with these IDs, by ID in capitals (IDs are matched without regard to case)."""
    wanted = {(t or "").strip().upper() for t in test_ids} - {""}
    if version is None or not wanted:
        return {}
    found = TestCase.objects.filter(script__version=version).select_related("script")
    return {t.test_id.upper(): t for t in found if t.test_id.upper() in wanted}


def outcomes_by_test(campaign: Campaign) -> dict[str, Counter]:
    """How a round's testers answered each test: the count of each outcome by test ID in capitals."""
    counts: dict[str, Counter] = {}
    for test_id, outcome in results(campaign).values_list("test__test_id", "outcome"):
        counts.setdefault(test_id.upper(), Counter())[outcome] += 1
    return counts


def step_notes_text(result: Result) -> str:
    """A result's notes on steps as one text: "Step 2: ...", a line each."""
    return "\n".join(f"Step {i + 1}: {note}" for i, note in enumerate(result.step_notes or []) if note)


def results_workbook(actor, campaign: Campaign) -> bytes:
    """A round's results as a workbook: the round, the counts by script, and a row for every test of every tester (those without a
    result too; withdrawn assignments are marked). The download is audited."""
    _require_lead(actor)
    from cedars.core import workbooks

    def name(user) -> str:
        return (user.name or user.email) if user else ""

    book = workbooks.Book(f"Testing results: {campaign.name}", subject="CEDARS testing results")
    s = summary(campaign)
    book.sheet(
        "Round",
        ["Item", "Value"],
        [
            ("Round of testing", campaign.name),
            ("Status", "Open" if campaign.is_open else "Closed"),
            ("First day", campaign.starts_on.isoformat()),
            ("Last day", campaign.ends_on.isoformat() if campaign.ends_on else ""),
            ("Scripts version", campaign.version.label),
            ("Site the scripts were written for", campaign.version.site),
            ("What is being tested", campaign.note),
            ("Testers", s["whole"]["testers"]),
            ("Tests to do (every tester's)", s["whole"]["total"]),
            ("With a result", s["whole"]["done"]),
            ("Downloaded", timezone.localtime().strftime("%Y-%m-%d %H:%M")),
        ],
        widths={0: 34, 1: 60},
    )
    book.sheet(
        "By script",
        ["Script", "Title", "Testers", "Tests to do", "With a result", "Passed", "Failed", "Blocked", "Does not apply", "No result yet"],
        [
            (
                r["script"].number,
                r["script"].title,
                r["testers"],
                r["total"],
                r["done"],
                r["passed"],
                r["failed"],
                r["blocked"],
                r["na"],
                r["left"],
            )
            for r in s["rows"]
        ],
        widths={1: 40},
    )
    answers = {(r.assignment_id, r.test_id): r for r in Result.objects.filter(assignment__campaign=campaign).select_related("test")}
    rows = []
    for a in campaign.assignments.select_related("script", "tester").order_by("script__position", "tester__email", "pk"):
        for t in a.script.tests.all():
            r = answers.get((a.pk, t.pk))
            rows.append(
                (
                    a.script.number,
                    a.script.title,
                    t.test_id,
                    t.title,
                    t.section,
                    t.priority,
                    t.account,
                    name(a.tester),
                    a.tester.email,
                    "Withdrawn" if a.withdrawn_at else "Assigned",
                    OUTCOME_WORDS[r.outcome] if r else "No result yet",
                    r.comment if r else "",
                    step_notes_text(r) if r else "",
                    timezone.localtime(r.recorded_at).strftime("%Y-%m-%d %H:%M") if r else "",
                )
            )
    book.sheet(
        "Results",
        [
            "Script",
            "Script title",
            "Test",
            "Test title",
            "Section",
            "Priority",
            "Account used",
            "Tester",
            "Tester's email",
            "Assignment",
            "Result",
            "What happened",
            "Notes on steps",
            "Saved",
        ],  # fmt: skip
        rows,
        widths={1: 24, 3: 44, 4: 24, 6: 20, 7: 22, 8: 28, 11: 60, 12: 50, 13: 18},
    )
    audit.record("testing.results_downloaded", obj=campaign, after={"campaign": campaign.name, "rows": len(rows)}, actor=actor)
    return book.save()
