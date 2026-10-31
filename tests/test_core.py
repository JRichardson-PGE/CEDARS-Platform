import importlib
import os
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured

pytestmark = pytest.mark.django_db


def test_the_site_opens_on_its_front_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "California Energy Data and Reporting System" in r.content.decode()


def test_about_lists_the_modules(client):
    r = client.get("/about/")
    assert r.status_code == 200
    html = r.content.decode()
    assert '<a class="skip" href="#main">' in html
    assert 'id="announcer"' in html
    for name in ("Programs", "Calculations", "Impact profiles", "Budgets", "Claims", "Monthly reports"):
        assert f'<th scope="row">{name}</th>' in html


def test_healthz_checks_the_database(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert "no-cache" in r["Cache-Control"]


def test_healthz_reports_a_database_failure(client):
    with mock.patch("cedars.core.views.connection.cursor", side_effect=RuntimeError("down")):
        r = client.get("/healthz")
    assert r.status_code == 503


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in r["Content-Security-Policy"]
    assert r["X-Frame-Options"] == "DENY"
    assert r["X-Content-Type-Options"] == "nosniff"
    assert "camera=()" in r["Permissions-Policy"]


def test_sandbox_banner(client, sandbox):
    assert "Sandbox: test data only" in client.get("/about/").content.decode()


def test_no_banner_outside_the_sandbox(client):
    assert "Sandbox" not in client.get("/about/").content.decode()


def test_prod_settings_need_a_secret_key():
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SECRET_KEY"}
    with mock.patch.dict(os.environ, env, clear=True), pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        importlib.reload(importlib.import_module("cedars.settings.prod"))
