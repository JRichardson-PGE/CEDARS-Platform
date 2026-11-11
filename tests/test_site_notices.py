"""Site notices (releases.SiteNotice): a CPUC system administrator's short dated notice, shown at the top of every page."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.releases import service
from cedars.services.releases.service import ReleaseError

pytestmark = pytest.mark.django_db


def hours(n):
    return timezone.now() + timedelta(hours=n)


def post(user, text="CEDARS is down Saturday 8 to 10 a.m.", start=-1, end=2, **kw):
    return service.post_notice(user, text=text, starts_at=hours(start), ends_at=hours(end), reason=kw.pop("reason", "maintenance"), **kw)


def test_only_a_system_administrator_posts_and_withdraws(cpuc_admin, pge_admin, client, signed_in):
    with pytest.raises(AccessDenied):
        post(pge_admin)
    n = post(cpuc_admin)
    with pytest.raises(AccessDenied):
        service.withdraw_notice(pge_admin, n, "no")
    signed_in(pge_admin)
    assert client.get(reverse("releases:notices")).status_code == 404
    assert client.post(reverse("releases:withdraw_notice", args=[n.pk]), {"reason": "x"}).status_code == 404


def test_a_notice_needs_its_words_a_reason_a_safe_link_and_an_end_after_its_start(cpuc_admin):
    for kw, message in (
        ({"text": "  "}, "Write what"),
        ({"reason": " "}, "why the notice"),
        ({"link": "javascript:alert(1)"}, "link"),
        ({"link": "//elsewhere.example.org/"}, "link"),
        ({"start": 3, "end": 2}, "end after"),
        ({"start": -3, "end": -1}, "has passed"),
    ):
        with pytest.raises(ReleaseError, match=message):
            post(cpuc_admin, **kw)
    assert post(cpuc_admin, link="/submissions/calendar/").link == "/submissions/calendar/"
    assert post(cpuc_admin, link="https://www.cpuc.ca.gov/").link == "https://www.cpuc.ca.gov/"


def test_every_page_shows_it_from_its_start_until_its_end(cpuc_admin, client):
    now_shown = post(cpuc_admin, text="Claims for 2027Q3 are due a week later.", link="/submissions/calendar/")
    later = post(cpuc_admin, text="Later notice", start=5, end=6)
    assert service.site_notices() == [now_shown]
    assert [n.pk for n in service.site_notices(hours(5.5))] == [later.pk]
    assert service.site_notices(hours(7)) == []
    html = client.get("/").content.decode()
    assert '<p class="coming-changes site-notice"><strong>Notice:</strong> Claims for 2027Q3 are due a week later.' in html
    assert '<a href="/submissions/calendar/">More about this notice</a>' in html and "Later notice" not in html
    e = AuditEvent.objects.get(action="site_notice.posted", object_id=str(now_shown.pk))
    assert e.reason == "maintenance" and e.after["text"] == now_shown.text


def test_a_withdrawn_notice_leaves_every_page_and_stays_listed(cpuc_admin, client, signed_in):
    n = post(cpuc_admin, text="Outage tonight")
    with pytest.raises(ReleaseError, match="why"):
        service.withdraw_notice(cpuc_admin, n, " ")
    service.withdraw_notice(cpuc_admin, n, "fixed early")
    with pytest.raises(ReleaseError, match="already"):
        service.withdraw_notice(cpuc_admin, n, "again")
    assert service.site_notices() == []
    assert AuditEvent.objects.filter(action="site_notice.withdrawn", reason="fixed early").exists()
    signed_in(cpuc_admin)
    html = client.get(reverse("releases:notices")).content.decode()
    assert "Outage tonight" in html and "fixed early" in html and "Withdrawn" in html
    assert '<p class="coming-changes site-notice">' not in html


def test_the_page_posts_and_withdraws(cpuc_admin, client, signed_in):
    signed_in(cpuc_admin)
    url = reverse("releases:notices")
    end = timezone.localtime(hours(3)).strftime("%Y-%m-%dT%H:%M")
    r = client.post(url, {"text": "Uploads paused for an hour.", "link": "", "starts_at": "", "ends_at": end, "reason": "storage"})
    assert r.status_code == 302
    n = service.site_notices()[0]
    assert n.text == "Uploads paused for an hour." and n.created_by == cpuc_admin
    html = client.get(url).content.decode()
    assert "Uploads paused for an hour." in html and f'action="{reverse("releases:withdraw_notice", args=[n.pk])}"' in html
    client.post(reverse("releases:withdraw_notice", args=[n.pk]), {"reason": "done"})
    assert service.site_notices() == []
    r = client.post(url, {"text": "x", "ends_at": "2020-01-01T00:00", "reason": "y"})
    assert "has passed" in r.content.decode()
