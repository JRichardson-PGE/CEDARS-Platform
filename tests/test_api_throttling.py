"""The API's rate limits: per user across their tokens and sessions, per address without one; 429 with Retry-After over the limit."""

import pytest
from django.core.cache import cache
from django.test import Client
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from cedars.services.accounts import service, tokens

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def low_limits(monkeypatch):
    # DRF reads the rates once, when the classes are defined, so a test sets them on the classes
    monkeypatch.setattr(UserRateThrottle, "THROTTLE_RATES", {"user": "3/minute", "anon": "2/minute"})
    monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"user": "3/minute", "anon": "2/minute"})
    cache.clear()
    yield
    cache.clear()


def member(make_user, org, org_admin, email):
    u = make_user(email)
    service.grant(org_admin, u, org, "calculations.read")
    return u


def call(key, path="/api/v1/me"):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {key}")


def test_a_user_over_the_limit_gets_429_with_retry_after_and_others_do_not(make_user, pge, pge_admin):
    a, b = member(make_user, pge, pge_admin, "a@pge.com"), member(make_user, pge, pge_admin, "b@pge.com")
    _, key_a = tokens.create_token(a, pge, name="a", scopes=["calculations"], days=30)
    _, key_a2 = tokens.create_token(a, pge, name="a again", scopes=["calculations"], days=30)
    _, key_b = tokens.create_token(b, pge, name="b", scopes=["calculations"], days=30)
    assert [call(key_a).status_code for _ in range(3)] == [200, 200, 200]
    over = call(key_a2)  # a second token of the same person shares the limit
    assert over.status_code == 429
    assert int(over["Retry-After"]) > 0
    assert "throttled" in over.json()["detail"].lower() or "request was throttled" in over.json()["detail"].lower()
    assert call(key_b).status_code == 200


def test_calls_without_a_user_are_limited_per_address():
    c = Client(REMOTE_ADDR="10.0.0.7")
    assert [c.get("/api/v1/schema").status_code for _ in range(2)] == [200, 200]
    assert c.get("/api/v1/schema").status_code == 429
    assert Client(REMOTE_ADDR="10.0.0.8").get("/api/v1/schema").status_code == 200


def test_a_forwarded_for_header_does_not_change_the_address_without_a_proxy_in_front():
    c = Client(REMOTE_ADDR="10.0.0.9")
    codes = [c.get("/api/v1/schema", HTTP_X_FORWARDED_FOR=f"192.168.1.{i}").status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_pages_are_not_throttled(client):
    assert all(client.get("/healthz").status_code == 200 for _ in range(5))
