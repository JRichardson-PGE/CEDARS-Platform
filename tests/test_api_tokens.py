from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from cedars.services.accounts import service, tokens
from cedars.services.accounts.models import ApiToken
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def analyst(make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org", "Ana Lyst")
    service.grant(pge_admin, u, pge, "calculations.submit")
    service.grant(pge_admin, u, pge, "claims.read")
    return u


def call(key, path="/api/v1/me"):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {key}")


def test_a_token_acts_for_its_organisation_within_its_scopes(analyst, pge):
    token, key = tokens.create_token(analyst, pge, name="claims system", scopes=["calculations"], days=90)
    assert key.startswith(f"cedars_{token.prefix}_") and len(key) > 50
    assert ApiToken.objects.get().key_sha256 != key  # only the hash is kept
    r = call(key)
    assert r.status_code == 200
    assert r.json() == {
        "email": "analyst@pge.example.org",
        "organisation": "PGE",
        "assurance": "token",
        "token": f"{token.prefix}…",
        "permissions": ["calculations.submit"],  # claims.read is the user's, but not in the token's scopes
    }
    assert tokens.token_allows(token, "calculations.submit") and not tokens.token_allows(token, "claims.read")
    assert ApiToken.objects.get().last_used_at is not None


def test_a_token_never_holds_more_than_its_user(analyst, pge, pge_admin):
    token, key = tokens.create_token(analyst, pge, name="x", scopes=["calculations", "claims"], days=30)
    assert not tokens.token_allows(token, "calculations.admin")
    grant = analyst.memberships.get().grants.get(permission="calculations.submit")
    service.revoke(pge_admin, grant, "moved teams")
    assert not tokens.token_allows(token, "calculations.submit")
    assert call(key).json()["permissions"] == ["claims.read"]


def test_special_permissions_are_out_of_a_tokens_reach(pge, pge_admin):
    token, _ = tokens.create_token(pge_admin, pge, name="admin script", scopes=["calculations"], days=30)
    assert not tokens.token_allows(token, "org.admin")


def test_revoked_expired_and_unknown_keys_are_refused(analyst, pge, pge_admin):
    token, key = tokens.create_token(analyst, pge, name="x", scopes=["calculations"], days=30)
    assert call("cedars_nope_nope").status_code in (401, 403)
    assert call(key + "x").status_code in (401, 403)
    ApiToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert call(key).status_code in (401, 403)
    ApiToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() + timedelta(days=1))
    assert call(key).status_code == 200
    tokens.revoke_token(pge_admin, token, "leaked in a log")  # the organization's admin can revoke a member's token
    r = call(key)
    assert r.status_code in (401, 403) and "not valid" in r.json()["detail"]
    e = AuditEvent.objects.get(action="api_token.revoked")
    assert e.reason == "leaked in a log" and e.actor == pge_admin and e.organisation == pge


def test_leaving_the_organisation_stops_its_tokens(analyst, pge, pge_admin):
    _, key = tokens.create_token(analyst, pge, name="x", scopes=["calculations"], days=30)
    service.end_membership(pge_admin, analyst.memberships.get(), "left PG&E")
    assert call(key).status_code in (401, 403)


def test_token_rules(analyst, pge, make_user, cpuc_admin):
    with pytest.raises(AccessDenied, match="name"):
        tokens.create_token(analyst, pge, name=" ", scopes=["calculations"], days=30)
    with pytest.raises(AccessDenied, match="Not modules"):
        tokens.create_token(analyst, pge, name="x", scopes=["everything"], days=30)
    with pytest.raises(AccessDenied, match="between 1 and 365"):
        tokens.create_token(analyst, pge, name="x", scopes=["calculations"], days=400)
    with pytest.raises(AccessDenied, match="not a member"):
        tokens.create_token(make_user("other@example.com"), pge, name="x", scopes=["calculations"], days=30)
    with pytest.raises(AccessDenied, match="only your own calculations"):
        tokens.create_token(analyst, None, name="x", scopes=["claims"], days=30)
    stranger = make_user("stranger@example.com")
    token, _ = tokens.create_token(analyst, pge, name="x", scopes=["calculations"], days=30)
    with pytest.raises(AccessDenied, match="owner"):
        tokens.revoke_token(stranger, token)
    created = AuditEvent.objects.filter(action="api_token.created").first()
    assert created.after["scopes"] == ["calculations"] and "key" not in str(created.after)


def test_a_community_token_reaches_no_organisation(make_user):
    u = make_user("community@example.com")
    token, key = tokens.create_token(u, None, name="my laptop", scopes=["calculations"], days=30)
    body = call(key).json()
    assert body["organisation"] is None and body["permissions"] == []
    assert not tokens.token_allows(token, "calculations.read")


def test_calls_without_a_token_need_a_session(client, signed_in, analyst):
    assert client.get("/api/v1/me").status_code in (401, 403)
    body = signed_in(analyst).get("/api/v1/me").json()
    assert body["assurance"] == "mfa" and body["token"] is None and "claims.read" in body["permissions"]


def test_the_openapi_description_is_public_and_documents_bearer_tokens(client):
    r = client.get("/api/v1/schema", HTTP_ACCEPT="application/json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "CEDARS API" and "/api/v1/me" in schema["paths"]
    assert schema["components"]["securitySchemes"]["token"] == {"type": "http", "scheme": "bearer"}


# --- pages


def test_a_member_makes_a_token_and_sees_its_key_once(signed_in, analyst):
    c = signed_in(analyst)
    assert "Make a token" in c.get("/account/").content.decode()
    r = c.post("/account/tokens", {"name": "claims system", "organisation": "PGE", "scopes": ["calculations"], "days": "90"})
    assert r.status_code == 200 and r["Cache-Control"] == "no-store"
    token = ApiToken.objects.get()
    key = r.context["key"]
    assert key in r.content.decode() and token.organisation.code == "PGE"
    assert call(key).status_code == 200
    html = c.get("/account/").content.decode()
    assert f"cedars_{token.prefix}" in html and key not in html
    r = c.post(f"/account/tokens/{token.pk}/revoke")
    assert r.status_code == 302 and ApiToken.objects.get().status == "revoked"


def test_an_organisation_admin_sees_and_revokes_members_tokens(signed_in, analyst, pge, pge_admin):
    token, _ = tokens.create_token(analyst, pge, name="claims system", scopes=["calculations"], days=30)
    c = signed_in(pge_admin)
    html = c.get("/admin/organizations/PGE/").content.decode()
    assert "analyst@pge.example.org" in html and f"cedars_{token.prefix}" in html
    r = c.post(f"/account/tokens/{token.pk}/revoke", {"from": "organisation"})
    assert r["Location"] == "/admin/organizations/PGE/"
    assert ApiToken.objects.get().status == "revoked"


def test_someone_else_cannot_revoke_a_token(signed_in, analyst, pge, make_user):
    token, _ = tokens.create_token(analyst, pge, name="x", scopes=["calculations"], days=30)
    assert signed_in(make_user("stranger@example.com")).post(f"/account/tokens/{token.pk}/revoke").status_code == 404
    assert ApiToken.objects.get().status == "active"
