"""Hardening: sign-in rate limits per caller behind the load balancer, a limit on what an uploaded .zip or workbook unpacks to, text
kept as text in the workbooks the platform writes, no Django admin site, and form redirects that stay on this site."""

import io
import os
import subprocess
import sys
import zipfile

import pytest
from django.core.cache import cache
from django.urls import NoReverseMatch, reverse
from openpyxl import load_workbook

from cedars.core import uploads, workbooks
from cedars.services.accounts import service
from cedars.services.accounts.models import Grant
from cedars.services.releases import limits

from .conftest import create_user


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()  # the rate limits count in the cache
    yield
    cache.clear()


# --- sign-in rate limits


def _sign_in(client, email, password, address):
    return client.post("/accounts/login/", {"login": email, "password": password}, HTTP_X_FORWARDED_FOR=address)


def test_one_callers_failed_sign_ins_do_not_lock_out_others_behind_the_load_balancer(client, settings, db):
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1
    create_user("analyst@pge.example.org")
    for i in range(12):  # over the per-address limit of failed sign-ins, each for a made-up account
        _sign_in(client, f"nobody{i}@example.org", "wrong password", "198.51.100.7")
    refused = _sign_in(client, "nobody@example.org", "wrong password", "198.51.100.7")
    assert "Too many" in refused.content.decode()
    ok = _sign_in(client, "analyst@pge.example.org", "correct horse battery staple", "198.51.100.8")
    assert ok.status_code == 302 and "Too many" not in ok.content.decode()


def test_production_reads_the_callers_address_from_the_load_balancer():
    env = {**os.environ, "DJANGO_SECRET_KEY": "test-only", "DJANGO_SETTINGS_MODULE": "cedars.settings.prod"}
    env.pop("CEDARS_NUM_PROXIES", None)
    code = "from django.conf import settings; print(settings.ALLAUTH_TRUSTED_PROXY_COUNT, settings.REST_FRAMEWORK['NUM_PROXIES'])"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == ["1", "1"]


# --- what an upload unpacks to


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


@pytest.fixture
def bomb(settings):
    settings.CEDARS_UPLOAD_MAX_UNZIPPED_MB = 1
    data = _zip({"Measure.csv": b"CEInputID\n" + b"0" * (2 * 1024 * 1024)})
    assert len(data) < 20_000  # small to send, large once unpacked
    return data


def test_the_unzipped_size_is_read_before_anything_is_unpacked(bomb, settings):
    assert "unpacks to 2.0 MB" in uploads.too_large_unzipped("jobs.zip", bomb)
    assert uploads.too_large_unzipped("small.zip", _zip({"Measure.csv": b"CEInputID\nA\n"})) == ""
    assert uploads.too_large_unzipped("notes.csv", b"not a zip") == ""  # the reader refuses that in its own words
    settings.CEDARS_UPLOAD_MAX_UNZIPPED_MB = 3
    assert uploads.too_large_unzipped("jobs.zip", bomb) == ""


def test_every_filing_reader_refuses_a_zip_that_unpacks_too_large(bomb):
    from cedars.modules.budgets import jobfile
    from cedars.modules.claims import upload as claims_upload
    from cedars.modules.monthly import upload as monthly_upload
    from cedars.services.submissions import corrections
    from cedars.services.vault import service as vault

    with pytest.raises(claims_upload.UploadError, match="unpacks to"):
        claims_upload.read(bomb, "claims.zip")
    with pytest.raises(monthly_upload.UploadError, match="unpacks to"):
        monthly_upload.read_tables(bomb, "monthly.zip")
    with pytest.raises(jobfile.JobError, match="unpacks to"):
        jobfile.read_tables(bomb, "budget.zip")
    with pytest.raises(vault.PIIError, match="unpacks to"):
        vault.read(bomb, "pii.zip")
    with pytest.raises(corrections.CorrectionRefused, match="unpacks to"):
        corrections._split_deletes(bomb, "correction.zip", lambda name: name)
    with pytest.raises(uploads.CombineError, match="unpacks to"):
        uploads.combine([("a.zip", bomb), ("b.csv", b"CEInputID\nA\n")], lambda name: name if name == "Measure" else None)


def test_a_workbook_that_unpacks_too_large_is_refused_too(bomb):
    from cedars.modules.claims import upload as claims_upload

    with pytest.raises(claims_upload.UploadError, match="unpacks to"):
        claims_upload.read(bomb, "claims.xlsx")


def test_the_unzipped_limit_is_on_the_limits_page(client, settings, db):
    settings.CEDARS_UPLOAD_MAX_UNZIPPED_MB = 1024
    assert limits.current("upload_unzipped_mb") == "1,024 MB"
    assert "once unpacked" in client.get("/limits/").content.decode()


# --- workbooks keep text as text


@pytest.mark.parametrize("write_only", [False, True])
def test_filed_text_that_looks_like_a_formula_stays_text(write_only):
    book = workbooks.Book("Test", write_only=write_only)
    book.sheet("Records", ["PrgID", "MeasDescription", "Units"], [["PGE_1", '=HYPERLINK("https://example.org","x")', 3], ["PGE_2", "=", 4]])
    data = book.save()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
        assert not any(b"<f>" in z.read(n) for n in sheets)
    ws = load_workbook(io.BytesIO(data))["Records"]
    assert ws["B2"].value == '=HYPERLINK("https://example.org","x")' and ws["B2"].data_type == "s"
    assert ws["C2"].value == 3 and ws["B3"].value == "="


# --- no Django admin site


def test_there_is_no_django_admin_site(client, db):
    with pytest.raises(NoReverseMatch):
        reverse("admin:index")
    assert client.get("/django-admin/").status_code == 404


# --- redirects stay on this site


@pytest.mark.parametrize("target", ["https://example.org/", "//example.org/", "/\\example.org/", "javascript:alert(1)"])
def test_switching_organization_never_redirects_off_the_site(signed_in, make_user, pge, pge_admin, target):
    u = make_user("analyst@pge.example.org")
    service.grant(pge_admin, u, pge, "claims.read")
    r = signed_in(u).post("/account/organization", {"organisation": "PGE", "next": target})
    assert r.status_code == 302 and r["Location"] == reverse("administration:account")


def test_switching_organization_goes_back_to_the_page_on_this_site(signed_in, make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org")
    service.grant(pge_admin, u, pge, "claims.read")
    r = signed_in(u).post("/account/organization", {"organisation": "PGE", "next": "/claims/"})
    assert r["Location"] == "/claims/"


def test_revoking_never_redirects_off_the_site(signed_in, make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org")
    service.grant(pge_admin, u, pge, "claims.read")
    g = Grant.objects.get(membership__user=u, permission="claims.read")
    r = signed_in(pge_admin).post(f"/admin/grants/{g.pk}/revoke", {"reason": "moved teams", "next": "https://example.org/"})
    assert r["Location"] == f"/admin/organizations/{pge.code}/"
