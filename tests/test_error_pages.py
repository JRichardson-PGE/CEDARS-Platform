"""The error pages (UI review 3, U4; cedars/core/errors.py): in the site's layout, with one sentence, search and the ways on. A page that
does not exist and a page the account cannot open read the same."""

import pytest
from django.template import loader
from django.test import Client, RequestFactory

from cedars.core import errors

from .conftest import page_only, sign_in

SENTENCE = "This page does not exist, or your account cannot open it."


@pytest.mark.django_db
def test_a_missing_page_is_in_the_site_layout_with_search_and_ways_on(client):
    response = client.get("/admin/no-such-thing/")
    assert response.status_code == 404
    html = page_only(response.content.decode())
    assert "<header>" in html and 'class="foot"' in html  # the site's header and footer
    assert "<h1>Page not available</h1>" in html and SENTENCE in html
    assert 'name="q" value="no such thing"' in html  # the address's words, ready to search
    assert 'href="/"' in html and 'href="/help/"' in html
    assert "/accounts/login/?next=/admin/no-such-thing/" in html  # signed out: sign in and come back
    assert 'href="/queue/"' not in html.split("<main", 1)[1]  # My queue is for members


@pytest.mark.django_db
def test_not_found_and_not_allowed_read_the_same(signed_in, pge_admin):
    """/admin/retention/ refuses (403) and /admin/role-templates/ hides itself (404) from an organization administrator: one page."""
    client = signed_in(pge_admin)
    refused, hidden = client.get("/admin/retention/"), client.get("/admin/role-templates/")
    assert (refused.status_code, hidden.status_code) == (403, 404)
    for response in (refused, hidden):
        main = page_only(response.content.decode()).split("<main", 1)[1]
        assert "<h1>Page not available</h1>" in main and SENTENCE in main
        assert "Only a CPUC system administrator" not in main  # the refusal's reason is not told: one sentence for both
        assert 'href="/queue/"' in main  # a member's My queue
        assert "sign in" not in main


@pytest.mark.django_db
def test_an_api_address_answers_in_json(client):
    response = client.get("/api/v1/no-such-endpoint/")
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert response.json() == {"detail": "Not found."}


def test_the_500_page_renders_with_no_context_and_no_database():
    """Whatever failed (the database, a context processor) must not take the error page down too. No django_db mark: a query fails."""
    html = loader.get_template("500.html").render()  # no context, no request
    assert "<h1>Something went wrong</h1>" in html and "<header>" in html
    assert 'action="/search/"' in html and 'href="/help/"' in html and 'href="/"' in html
    assert "Sign in" not in html and "Sign out" not in html  # it cannot know who is looking
    response = errors.server_error(RequestFactory().get("/claims/"))
    assert response.status_code == 500 and b"Something went wrong" in response.content


def test_the_500_page_has_a_last_resort(monkeypatch):
    def broken(name):
        raise RuntimeError("templates unreadable")

    monkeypatch.setattr(errors.loader, "get_template", broken)
    response = errors.server_error(RequestFactory().get("/"))
    assert response.status_code == 500 and b"Something went wrong" in response.content


@pytest.mark.django_db
def test_a_form_without_its_security_token_says_why(pge_admin):
    client = sign_in(Client(enforce_csrf_checks=True), pge_admin)
    response = client.post("/feedback/", {"message": "hello"})
    assert response.status_code == 403
    main = page_only(response.content.decode()).split("<main", 1)[1]
    assert "<h1>Form not sent</h1>" in main and "reload the page" in main
    assert 'action="/search/"' in main
