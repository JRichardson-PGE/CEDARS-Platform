"""A new account's first sign-in (UI review 3, U6): where it is in its set-up (confirm the email, set up the second factor, ask for
access), the set-up key shown whole and the code field six digits wide, and recovery codes that say what they are for, offer Download and
Print, and Continue to where the sign-in was heading, or to Ask for access for an account in no organization."""

import pytest
from allauth.account.models import EmailAddress

from cedars.services.accounts import service as access

from .conftest import page_only
from .test_administration_pages import _totp_code

PASSWORD = "correct horse battery staple"


def _new_account(make_user, email="fresh@example.org"):
    u = make_user(email)
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    return u


def _sign_in_and_set_up(client, user, next_url=""):
    """Sign in with a password, follow the middleware to the set-up page and activate the authenticator, as a person would."""
    r = client.post("/accounts/login/", {"login": user.email, "password": PASSWORD})
    assert r.status_code == 302
    r = client.get(next_url or "/")  # the first page sends the session to the set-up, carrying where it was heading
    assert r.status_code == 302 and r["Location"].startswith("/accounts/2fa/totp/activate/")
    setup = r["Location"]
    page = client.get(setup).content.decode()
    r = client.post(setup, {"code": _totp_code(client.session["mfa.totp.secret"]), **_hidden_next(page)})
    assert r.status_code == 302, r.content.decode()[:500]
    return r, page


def _hidden_next(page: str) -> dict:
    marker = '<input type="hidden" name="next" value="'
    if marker not in page:
        return {}
    return {"next": page.split(marker, 1)[1].split('"', 1)[0].replace("&#x2F;", "/").replace("&amp;", "&")}


@pytest.mark.django_db(transaction=True)  # allauth records the activation on commit, inside the view
def test_the_set_up_page_shows_the_steps_the_whole_key_and_a_six_digit_field(client, make_user):
    u = _new_account(make_user)
    client.post("/accounts/login/", {"login": u.email, "password": PASSWORD})
    html = page_only(client.get("/accounts/2fa/totp/activate/?next=/calculations/").content.decode())
    steps = html.split('<nav class="set-up-steps"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-label="Setting up your account"' in steps
    assert 'Confirm your email<span class="state"> (done)' in steps
    assert '<li aria-current="step" class="now"><span class="n" aria-hidden="true">2</span> Set up your second factor' in steps
    secret = client.session["mfa.totp.secret"]
    assert (
        f'id="authenticator_secret" aria-labelledby="authenticator-key-label" aria-describedby="authenticator-key-help">{secret}</code>'
        in html
    )
    assert 'data-copy="authenticator_secret"' in html
    field = html.split('id="id_code"', 1)[1].split(">", 1)[0]
    assert 'inputmode="numeric"' in field and 'autocomplete="one-time-code"' in field
    assert '<label for="id_code">Authenticator code</label>' in html
    assert '<input type="hidden" name="next" value="/calculations/">' in html


@pytest.mark.django_db(transaction=True)
def test_an_account_in_no_organization_continues_to_ask_for_access(client, make_user):
    u = _new_account(make_user)
    r, _ = _sign_in_and_set_up(client, u, "/calculations/")
    assert r["Location"] == "/accounts/2fa/recovery-codes/"
    html = page_only(client.get(r["Location"]).content.decode())
    assert "Each recovery code signs you in once in place of your authenticator app&#x27;s code" in html or (
        "Each recovery code signs you in once in place of your authenticator app's code" in html
    )
    assert '<nav class="set-up-steps"' in html
    assert 'class="recovery-codes"' in html and html.count("<li><code>") == 10
    assert ">Download the codes</a>" in html and "data-print hidden>Print the codes</button>" in html
    assert '<a class="button primary" href="/account/continue/">Continue to Ask for access</a>' in html
    assert "Generate new codes" not in html  # not on the first visit: Continue is the way on
    r = client.get("/account/continue/")
    assert r.status_code == 302 and r["Location"] == "/account/request-access/"
    html = page_only(client.get("/accounts/2fa/recovery-codes/").content.decode())
    assert "/account/continue/" not in html and "Back to your security settings" in html  # the set-up is over


@pytest.mark.django_db(transaction=True)
def test_a_member_continues_where_the_sign_in_was_heading(client, make_user, pge, pge_admin):
    u = _new_account(make_user, "member@example.org")
    access.grant(pge_admin, u, pge, "claims.read")
    r, _ = _sign_in_and_set_up(client, u, "/claims/")
    html = page_only(client.get(r["Location"]).content.decode())
    assert '<a class="button primary" href="/account/continue/">Continue</a>' in html
    assert "(done: you belong to one)" in html
    r = client.get("/account/continue/")
    assert r.status_code == 302 and r["Location"] == "/claims/"


@pytest.mark.django_db(transaction=True)
def test_continue_never_leaves_the_site(client, make_user, pge, pge_admin):
    u = _new_account(make_user, "member@example.org")
    access.grant(pge_admin, u, pge, "claims.read")
    client.post("/accounts/login/", {"login": u.email, "password": PASSWORD})
    page = client.get("/accounts/2fa/totp/activate/?next=https://example.com/").content.decode()
    r = client.post("/accounts/2fa/totp/activate/", {"code": _totp_code(client.session["mfa.totp.secret"]), **_hidden_next(page)})
    assert r.status_code == 302
    r = client.get("/account/continue/")
    assert r["Location"] == "/"


@pytest.mark.django_db
def test_the_first_steps_show_on_the_email_pages(client):
    html = page_only(client.get("/accounts/confirm-email/").content.decode())
    assert '<li aria-current="step" class="now"><span class="n" aria-hidden="true">1</span> Confirm your email' in html
