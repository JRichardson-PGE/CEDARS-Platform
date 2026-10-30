import io
import json
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from cedars.modules.calculations.models import Run
from cedars.modules.claims import service
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion
from cedars.services.vault import service as vault
from cedars.services.vault.models import PIIContact, PIISite, PIIUpload

from .conftest import sign_in
from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import deadline, filer, finish_run, programs, q1, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db(databases=["default", "vault"])

NAME, PHONE, EMAIL, ADDRESS, ACCOUNT = "Jane Customer", "4155550123", "jane@example.com", "1 Market St", "9988776655"


def pii_zip(contacts=None, sites=None) -> bytes:
    if contacts is None:
        contacts = [{"ContactID": "PGE-C1", "ContactName": NAME, "ContactPhoneNumber": PHONE, "ContactEmail": EMAIL}]
    sites = [{"SiteID": "PGE-S1", "SiteAddress": ADDRESS, "ElectricServiceAccountID": ACCOUNT}] if sites is None else sites
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, rows in (("PII_Contact", contacts), ("PII_Site", sites)):
            cols = list(dict.fromkeys(c for r in rows for c in r))
            z.writestr(f"{name}.csv", ",".join(cols) + "\n" + "".join(",".join(r.get(c, "") for c in cols) + "\n" for r in rows))
    return out.getvalue()


@pytest.fixture
def pii_filer(filer, pge, pge_admin, cpuc_admin):  # noqa: F811
    access.approve_pii(cpuc_admin, access.grant(pge_admin, filer, pge, perms.PII))
    return filer


@pytest.fixture
def checked(pii_filer, year, pge, programs):  # noqa: F811
    return service.upload_claims(pii_filer, deadline(), pge, q1(), "q1.zip")


def no_values(text: str) -> bool:
    return not any(x in text for x in (NAME, PHONE, EMAIL, ADDRESS, ACCOUNT, "415-555", "jane.example.com"))


def test_a_pii_file_is_checked_and_kept_in_the_vault(pii_filer, pge, checked):
    up = service.upload_pii(pii_filer, checked, pii_zip(), "pii.zip", "mfa")
    assert up.status == PIIUpload.Status.ACCEPTED and up.counts == {"PII_Contact": 1, "PII_Site": 1}
    c = PIIContact.objects.get(upload=up)
    assert c.contact_id == "PGE-C1" and c.values == {"ContactName": NAME, "ContactPhoneNumber": PHONE, "ContactEmail": EMAIL}
    assert PIISite.objects.get(upload=up).values["SiteAddress"] == ADDRESS
    e = AuditEvent.objects.get(action="vault.pii_accepted")
    assert e.after["counts"] == {"PII_Contact": 1, "PII_Site": 1} and no_values(json.dumps(e.after))
    assert service.pii(checked) == up


def test_a_failing_pii_file_is_not_kept_and_its_findings_carry_no_values(pii_filer, pge, checked):
    good = service.upload_pii(pii_filer, checked, pii_zip(), "pii.zip", "mfa")
    bad = pii_zip(
        contacts=[
            {"ContactID": "PGE-C1", "ContactName": NAME, "ContactPhoneNumber": "415-555", "ContactEmail": "jane.example.com"},
            {"ContactID": "PGE-C1", "ContactName": NAME},
            {"ContactID": "PGE-C9", "ContactName": NAME},
            {"ContactID": "SCE-C1", "ContactName": NAME},
            {"ContactID": "", "ContactName": NAME},
        ],
        sites=[{"SiteID": "PGE-S1", "SiteAddress": ADDRESS}],
    )
    up = service.upload_pii(pii_filer, checked, bad, "pii.zip", "mfa")
    assert up.status == PIIUpload.Status.REJECTED
    assert {(f["line"], f["rule"]) for f in up.findings} == {
        (2, "PII-NUM"), (2, "PII-EMAIL"), (3, "PII-KEY"), (4, "PII-REF"), (5, "PII-ID"), (5, "PII-REF"), (6, "PII-REQ"), (2, "PII-ACCT"),
    }  # fmt: skip
    assert not PIIContact.objects.filter(upload=up).exists() and no_values(json.dumps(up.findings))
    assert no_values(json.dumps(AuditEvent.objects.get(action="vault.pii_rejected").after))
    good.refresh_from_db()
    assert good.status == PIIUpload.Status.ACCEPTED and service.pii(checked) == up  # the page shows the latest; the kept one stands
    assert vault.accepted([checked.pk]) == good
    renamed = pii_zip(contacts=[{"ContactID": "PGE-C1", "ContactName": "J. Customer"}])
    newer = service.upload_pii(pii_filer, checked, renamed, "p.zip", "mfa")
    good.refresh_from_db()
    assert good.status == PIIUpload.Status.SUPERSEDED and not PIIContact.objects.filter(upload=good).exists()
    assert service.pii(checked) == newer and PIIContact.objects.get().values == {"ContactName": "J. Customer"}


def test_pii_files_hold_only_pii_tables_and_need_checked_claims(pii_filer, pge, checked, year, programs):  # noqa: F811
    with pytest.raises(vault.PIIError, match="only PII_Contact and PII_Site"):
        service.upload_pii(pii_filer, checked, _zip({"Claim.csv": "ClaimID\nPGE-2028-001\n"}), "x.zip", "mfa")
    with pytest.raises(vault.PIIError, match="not a valid"):
        service.upload_pii(pii_filer, checked, b"nope", "x.zip", "mfa")


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


def test_pii_needs_the_stewards_grant_and_the_session_the_policy_asks_for(
    filer,  # noqa: F811
    pge,
    pge_admin,
    cpuc_admin,
    cpuc,
    year,  # noqa: F811
    programs,  # noqa: F811
    make_user,
):
    v = service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    g = access.grant(pge_admin, filer, pge, perms.PII)
    with pytest.raises(AccessDenied, match="claims.pii"):
        service.upload_pii(filer, v, pii_zip(), "pii.zip", "mfa")  # pending until a steward approves
    access.approve_pii(cpuc_admin, g)
    up = service.upload_pii(filer, v, pii_zip(), "pii.zip", "mfa")
    staff = make_user("analyst@cpuc.ca.gov")
    access.grant(cpuc_admin, staff, cpuc, "claims.read")
    with pytest.raises(AccessDenied, match="PII grant"):
        vault.export(staff, pge, up, "mfa")
    second = make_user("steward2@cpuc.ca.gov")
    access.grant(cpuc_admin, second, cpuc, perms.STEWARD)
    access.approve_pii(second, access.grant(cpuc_admin, staff, cpuc, perms.PII))
    with zipfile.ZipFile(io.BytesIO(vault.export(staff, pge, up, "mfa", "EM&V sample"))) as z:
        assert NAME in z.read("PII_Contact.csv").decode() and ACCOUNT in z.read("PII_Site.csv").decode()
    e = AuditEvent.objects.get(action="vault.pii_read")
    assert e.reason == "EM&V sample" and e.actor == staff and no_values(json.dumps(e.after))
    with pytest.raises(AccessDenied):
        vault.export(staff, cpuc, up, "mfa")  # not CPUC's PII
    change = access.propose_policy_change(cpuc_admin, "pii_requires_federated", True, "CPUC security review")
    access.confirm_policy_change(second, change)
    with pytest.raises(AccessDenied):
        service.upload_pii(filer, v, pii_zip(), "pii.zip", "mfa")
    pge.federated = True
    pge.save()
    v = SubmissionVersion.objects.get(pk=v.pk)
    assert service.upload_pii(filer, v, pii_zip(), "pii.zip", "federated").status == PIIUpload.Status.ACCEPTED


def test_an_environment_without_a_vault_takes_no_pii(pii_filer, pge, checked, monkeypatch):
    monkeypatch.setattr(vault, "settings", SimpleNamespace(DATABASES={"default": {}}))
    with pytest.raises(AccessDenied, match="no PII vault"):
        service.upload_pii(pii_filer, checked, pii_zip(), "pii.zip", "mfa")
    assert service.pii(checked) is None


def test_a_confirmed_versions_pii_changes_only_through_a_reopened_version(pii_filer, pge, checked, cpuc_admin, release):  # noqa: F811
    first = service.upload_pii(pii_filer, checked, pii_zip(), "pii.zip", "mfa")
    finish_run(Run.objects.get(pk=service.price(pii_filer, checked)))
    checked.refresh_from_db()
    v1 = service.confirm(pii_filer, checked)
    with pytest.raises(AccessDenied, match="Ask to reopen"):
        service.upload_pii(pii_filer, v1, pii_zip(), "pii.zip", "mfa")
    submissions.move_deadline(cpuc_admin, deadline(), timezone.now() - timedelta(days=1), "test")
    submissions.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    v2 = service.request_reopen(pii_filer, v1.submission, "a corrected phone number")
    assert service.pii(v2) == first  # the predecessor's until its own
    own = service.upload_pii(pii_filer, v2, pii_zip(), "pii.zip", "mfa")
    assert service.pii(v2) == own and service.pii(v1) == first


def test_the_pii_section_of_the_page(client, pii_filer, pge, checked, filer):  # noqa: F811
    sign_in(client, pii_filer)
    url = reverse("claims:period", args=[deadline().pk])
    r = client.get(url)
    assert b'id="pii-h"' in r.content and b"No PII file has been kept" in r.content and b"Upload and check the PII" in r.content
    bad = pii_zip(contacts=[{"ContactID": "PGE-C9", "ContactName": NAME}])
    r = client.post(reverse("claims:pii_upload", args=[deadline().pk]), {"file": SimpleUploadedFile("p.zip", bad)}, follow=True)
    assert b"was not kept: 1 findings" in r.content and b"PII-REF" in r.content and no_values(r.content.decode())
    r = client.post(reverse("claims:pii_upload", args=[deadline().pk]), {"file": SimpleUploadedFile("p.zip", pii_zip())}, follow=True)
    assert b"passed its checks" in r.content and b"Download the PII" in r.content and no_values(r.content.decode())
    r = client.post(reverse("claims:pii_download", args=[deadline().pk]), {"org": "PGE", "reason": "site visit"})
    assert r.status_code == 200 and r["Cache-Control"] == "no-store"
    assert NAME in zipfile.ZipFile(io.BytesIO(r.content)).read("PII_Contact.csv").decode()
    assert AuditEvent.objects.get(action="vault.pii_read").reason == "site visit"


def test_without_the_pii_grant_the_page_has_no_pii_section(client, make_user, pge, pge_admin, year, programs):  # noqa: F811
    u = make_user("claimer@pge.com")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    service.upload_claims(u, deadline(), pge, q1(), "q1.zip")
    sign_in(client, u)
    r = client.get(reverse("claims:period", args=[deadline().pk]))
    assert r.status_code == 200 and b'id="pii-h"' not in r.content
    assert client.post(reverse("claims:pii_download", args=[deadline().pk]), {"org": "PGE"}).status_code == 404
