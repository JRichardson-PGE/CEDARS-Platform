"""Which specification version a filing set uses: chosen when it is set up (the current one offered, an older one in effect allowed,
checked against the set's ACC), and moved to a later published version with a reason, after its impact test. A move re-pins the set's
filings, checks open versions again (their calculation void) and leaves confirmed versions recorded against the version they were checked
with."""

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications import service as notifications
from cedars.services.specifications import editor, moving
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification
from cedars.services.submissions.models import Deadline, SubmissionVersion

from .conftest import sign_in
from .test_monthly import filer, month, programs, report  # noqa: F401 - fixtures
from .test_monthly import service as monthly

pytestmark = pytest.mark.django_db


def current():
    return specifications.current(Specification.objects.get(code="monthly"))


def later_version(name="Monthly reports, August update"):
    """A later published version of the monthly specification, in effect now."""
    doc = specifications.stored_starting("monthly")
    doc["version"]["name"] = name
    return specifications.load(doc)


def set_up(admin, version=None, acc="2026"):
    s = monthly.set_up_year(admin, year=2028, acc_version=acc, due_day=20, closing_due=date(2029, 3, 1), specification_version=version)
    Deadline.objects.filter(module="monthly").update(opens_at=None, due_at=timezone.now() + timedelta(days=30))
    return s


def year_deadlines(s):
    return list(monthly.year_deadlines(s))


# --- choosing at set-up


def test_the_set_up_offers_the_versions_in_effect_newest_first_and_pins_the_chosen_one(cpuc_admin):
    first = current()
    newer = later_version()
    assert moving.choices("monthly") == [newer, first]
    s = set_up(cpuc_admin, version=first)
    deadlines = year_deadlines(s)
    assert len(deadlines) == 13  # twelve months and the 13th-month report
    months = [d for d in deadlines if d.kind == monthly.KIND]
    assert {d.specification_version for d in months} == {first}  # the older one, as chosen, not the current
    closing = next(d for d in deadlines if d.kind == monthly.CLOSING_KIND)
    assert closing.specification_version is None  # the 13th-month report is generated: it takes no filing


def test_a_version_not_in_effect_or_of_another_module_is_refused(cpuc_admin):
    claims = specifications.current(Specification.objects.get(code="claims"))
    assert moving.setup_problems(claims, "monthly") == [f"{claims} is not a version of the monthly specification."]
    with pytest.raises(AccessDenied, match="not a version of the monthly specification"):
        set_up(cpuc_admin, version=claims)


def test_the_versions_engine_release_must_have_the_sets_acc(cpuc_admin, monkeypatch):
    monkeypatch.setattr(editor, "_releases", [lambda: [{"release": "0.1.94", "active": True, "default": True, "accs": ["2024"]}]])
    v = current()
    [problem] = moving.setup_problems(v, "monthly", ["2026"])
    assert "ReCET 0.1.94" in problem and "does not have the 2026 ACC" in problem
    assert moving.setup_problems(v, "monthly", ["2024"]) == []
    with pytest.raises(AccessDenied, match="does not have the 2026 ACC"):
        set_up(cpuc_admin, version=v, acc="2026")


def test_the_set_up_page_offers_the_version(client, cpuc_admin):
    newer = later_version()
    sign_in(client, cpuc_admin)
    page = client.get(reverse("monthly:year_new")).content.decode()
    assert "Filing specification version" in page and f"{newer.name} (current)" in page


# --- moving a filing set


@pytest.fixture
def moved_set(cpuc_admin, filer, pge, programs):  # noqa: F811
    """2028's monthly reports on the starting version: January confirmed, February uploaded and priced (open)."""
    s = set_up(cpuc_admin)
    jan = monthly.upload(filer, month(1), pge, report("202801", measures=[], costs=[]), "j.zip")
    monthly.price(filer, jan)
    jan = monthly.confirm(filer, SubmissionVersion.objects.get(pk=jan.pk))
    feb = monthly.upload(filer, month(2), pge, report("202802", measures=[], costs=[]), "f.zip")
    monthly.price(filer, feb)
    feb.refresh_from_db()
    assert feb.calculation  # priced: "nothing to price" is still a calculation on record
    return s, jan, feb


def test_a_filing_set_moves_to_a_later_version(cpuc_admin, moved_set):
    s, jan, feb = moved_set
    first = current()
    deadlines = year_deadlines(s)
    assert moving.later(deadlines) == []  # nothing later yet
    newer = later_version()
    assert moving.later(deadlines) == [newer]
    preview = moving.preview(newer, deadlines)
    assert [f["submission_version"] for f in preview["filings"]] == [jan.pk] and not preview["filings"][0]["note"]
    with pytest.raises(AccessDenied, match="Say why"):
        moving.move(cpuc_admin, deadlines, newer, "")
    done = moving.move(cpuc_admin, deadlines, newer, "The August update fixes a rule")
    assert done == {"moved": len(deadlines), "rechecked": 1, "notes": []}
    months = Deadline.objects.filter(module="monthly", kind=monthly.KIND)
    assert {d.specification_version for d in months} == {newer}
    assert Deadline.objects.get(module="monthly", kind=monthly.CLOSING_KIND).specification_version is None
    jan.refresh_from_db()
    feb.refresh_from_db()
    assert jan.specification_version == first and jan.status == SubmissionVersion.Status.PUBLISHED  # confirmed: kept as checked
    assert feb.specification_version == newer and not feb.calculation  # checked again on the new version, to be priced again
    event = AuditEvent.objects.filter(action="deadline.specification_moved").first()
    assert event.reason == "The August update fixes a rule" and event.after == {"specification": str(newer)}
    assert notifications.event_type("deadline.specification_moved") is not None  # the filing calendar's subscribers hear it
    deadlines = year_deadlines(s)
    assert moving.later(deadlines) == []
    with pytest.raises(AccessDenied, match="not a later published version"):
        moving.move(cpuc_admin, deadlines, first, "Back again")


def test_only_a_cpuc_system_administrator_moves_a_set(moved_set, pge_admin):
    s, *_ = moved_set
    newer = later_version()
    with pytest.raises(AccessDenied):
        moving.move(pge_admin, year_deadlines(s), newer, "why")


def test_the_move_page(client, cpuc_admin, moved_set):
    s, jan, _ = moved_set
    d = year_deadlines(s)[0]
    sign_in(client, cpuc_admin)
    setup_page = client.get(reverse("monthly:setup", args=[2028])).content.decode()
    assert 'id="setup-specification"' in setup_page and "move 2028 monthly reports" not in setup_page
    url = reverse("specifications:move_filing_set", args=[d.pk])
    assert "No later published version is in effect yet" in client.get(url).content.decode()
    newer = later_version()
    setup_page = client.get(reverse("monthly:setup", args=[2028])).content.decode()
    assert url in setup_page
    page = client.get(url, {"to": newer.pk}).content.decode()
    assert f"Impact of moving to {newer.name}" in page and 'id="impact-filings"' in page and 'id="move-form"' in page
    refused = client.post(url, {"to": newer.pk, "reason": ""}).content.decode()  # the reason is asked for on the page, beside the impact
    assert 'id="move-form"' in refused and f"Impact of moving to {newer.name}" in refused
    assert Deadline.objects.get(pk=d.pk).specification_version != newer
    r = client.post(url, {"to": newer.pk, "reason": "The August update"})
    assert r.status_code == 302 and r.url == reverse("monthly:setup", args=[2028])
    assert Deadline.objects.get(pk=d.pk).specification_version == newer
    assert client.get(url).status_code == 200
    client.logout()
    assert client.get(url).status_code in (302, 403)
