"""UAT 2 (2026-11-01): pages link only where the reader can open the target (U03, U06); a filing the CPUC's set-up cannot price tells
the filer so in their words and tells the system administrators why (U04); Engine releases names the filings a release could not price
(U05)."""

import pytest
from django.urls import reverse

from cedars.modules.budgets import service
from cedars.modules.calculations import service as calculations
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Submission

from .conftest import sign_in
from .test_budgets import filer, filing, job, priced_version, programs, release  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def test_a_published_version_links_its_filing_only_for_those_who_may_open_it(client, filer, filing, pge, programs, release, make_user):  # noqa: F811
    v, _ = priced_version(filer, filing, pge, release)
    v = service.confirm(filer, v)
    filing_url = reverse("budgets:filing", args=[filing.pk])
    page = sign_in(client, filer).get(reverse("budgets:version", args=[v.pk])).content.decode()
    assert f'href="{filing_url}?org=PGE"' in page
    other = make_user("reader@example.org", "Rea Der")
    page = sign_in(client, other).get(reverse("budgets:version", args=[v.pk])).content.decode()
    assert filing_url not in page and service.label(filing) in page
    assert sign_in(client, other).get(f"{filing_url}?org=PGE").status_code == 404  # what the link would have led to


def test_the_budgets_list_does_not_link_a_filing_its_reader_cannot_open(client, filer, filing, pge, pge_admin, programs):  # noqa: F811
    service.upload(filer, filing, pge, job(), "tual.zip")
    filing_url = reverse("budgets:filing", args=[filing.pk])
    assert not service.can_read_submission(pge_admin, Submission.objects.get())  # an organization admin without budgets.read
    page = sign_in(client, pge_admin).get(reverse("budgets:index")).content.decode()
    assert filing_url not in page and filing.kind in page
    assert f'href="{filing_url}"' in sign_in(client, filer).get(reverse("budgets:index")).content.decode()


def test_fund_shifts_offer_the_operating_budget_only_to_its_readers(client, filer, pge_admin, pge, filing, programs):  # noqa: F811
    assert "operating budget:" in sign_in(client, filer).get(reverse("budgets:shifts")).content.decode()
    assert "operating budget:" not in sign_in(client, pge_admin).get(reverse("budgets:shifts")).content.decode()


# --- pricing the CPUC's set-up cannot do (U04, U05)


@pytest.fixture
def old_release(cpuc_admin):
    """The default release has only the 2024 ACC; the filing is priced on the 2026 ACC."""
    return calculations.add_release(cpuc_admin, release="0.1.70", calculations=["recet"], accs={"recet": ["2024"]}, default=True)


def test_a_filer_is_told_pricing_is_not_set_up_and_the_administrators_why(filer, filing, pge, programs, old_release, cpuc_admin):  # noqa: F811
    v = service.upload(filer, filing, pge, job(), "tual.zip")
    with pytest.raises(AccessDenied) as e:
        service.price(filer, v)
    assert str(e.value) == submissions.PRICING_UNAVAILABLE and "ReCET" not in str(e.value)
    [n] = Notification.objects.filter(user=cpuc_admin)
    assert n.title.startswith("A filing cannot be priced: PGE") and "has no 2026 ACC" in n.body and "0.1.70" in n.body
    assert AuditEvent.objects.filter(action="submission.pricing_unavailable").count() == 1
    with pytest.raises(AccessDenied):
        service.price(filer, v)  # asked again: the administrators are not told twice
    assert Notification.objects.filter(user=cpuc_admin).count() == 1


def test_engine_releases_name_the_filings_a_release_cannot_price(client, filing, old_release, release, cpuc_admin):  # noqa: F811
    assert calculations.missing_accs(old_release) == [(service.label(filing), "2026")]
    assert calculations.missing_accs(release) == []
    page = sign_in(client, cpuc_admin).get(reverse("calculations:engines")).content.decode()
    assert "Cannot price" in page and f"{service.label(filing)} (2026 ACC)" in page
    r = client.post(reverse("calculations:engines"), {"action": "default", "release": old_release.pk}, follow=True)
    assert "cannot price" in r.content.decode()


def test_an_ordinary_access_denied_still_reaches_the_filer(filer, filing, pge, programs, release):  # noqa: F811
    v = service.upload(filer, filing, pge, job(), "tual.zip")
    access.end_membership(None, filer.memberships.get(), "left")
    with pytest.raises(AccessDenied) as e:
        service.price(filer, v)
    assert str(e.value) != submissions.PRICING_UNAVAILABLE
