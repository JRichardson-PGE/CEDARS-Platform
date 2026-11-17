"""Signing in comes back to the page the Sign in link was clicked on: the header's (and the front page's) Sign in link carries the
page as `next`, and sign-in keeps it through the second-factor step (a first set-up's Continue goes there too: test_first_sign_in).
Only this site's own pages are carried."""

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.test import RequestFactory

from cedars.core.context_processors import sign_in_url

from .test_administration_pages import _totp_code
from .test_first_sign_in import PASSWORD


def _header_link(html: str) -> str:
    link = html.split('data-tour="sign-in"', 1)[0].rsplit('href="', 1)[1].split('"', 1)[0]
    return link.replace("&amp;", "&")


@pytest.mark.django_db
def test_the_header_sign_in_link_carries_the_page(client):
    assert _header_link(client.get("/help/").content.decode()) == "/accounts/login/?next=%2Fhelp%2F"
    link = _header_link(client.get("/dashboards/?year=2024&pa=PGE").content.decode())
    assert link == "/accounts/login/?next=%2Fdashboards%2F%3Fyear%3D2024%26pa%3DPGE"  # the query too: the same view comes back


@pytest.mark.django_db
def test_the_front_page_sign_in_button_comes_back_to_the_front_page(client):
    html = client.get("/").content.decode()
    assert '<a class="button primary" href="/accounts/login/?next=%2F">Sign in</a>' in html


@pytest.mark.parametrize("path", ["/accounts/login/", "/accounts/signup/", "/accounts/password/reset/", "/accounts/logout/"])
def test_account_pages_carry_nothing(path):
    assert sign_in_url(RequestFactory().get(path)) == "/accounts/login/"


@pytest.mark.django_db
def test_a_link_to_another_site_is_never_followed(client, make_user):
    u = make_user("returning@example.org")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    r = client.post("/accounts/login/?next=https://elsewhere.example/", {"login": u.email, "password": PASSWORD})
    assert r.status_code == 302 and "elsewhere" not in r["Location"]


@pytest.mark.django_db
def test_an_account_with_a_second_factor_comes_back_after_its_code(client, make_user):
    u = make_user("returning@example.org")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    secret = generate_totp_secret()
    TOTP.activate(u, secret)
    assert Authenticator.objects.filter(user=u, type=Authenticator.Type.TOTP).exists()
    r = client.post("/accounts/login/?next=/help/", {"login": u.email, "password": PASSWORD, "next": "/help/"})
    assert r.status_code == 302 and r["Location"].startswith("/accounts/2fa/authenticate/")
    r = client.post(r["Location"], {"code": _totp_code(secret)})
    assert r.status_code == 302 and r["Location"] == "/help/"


@pytest.mark.django_db
def test_without_a_second_factor_required_sign_in_goes_straight_back(client, make_user, settings):
    settings.CEDARS_REQUIRE_MFA = False
    u = make_user("returning@example.org")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    r = client.post("/accounts/login/", {"login": u.email, "password": PASSWORD, "next": "/dashboards/?year=2024"})
    assert r.status_code == 302 and r["Location"] == "/dashboards/?year=2024"
