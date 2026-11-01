from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations.models import Run
from cedars.modules.claims import service
from cedars.modules.claims.models import ChangeLetter
from cedars.services.accounts import service as access
from cedars.services.accounts import tokens
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claim, claims_zip, confirmed, deadline, filer, finish_run, linked, programs, q1, row, tables, withdraw, year  # noqa: F401
from .test_uploads import split

pytestmark = pytest.mark.django_db
API = "/api/v1/claims"


def bearer(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key}"}


def get(key, path, **params):
    return Client().get(path if path.startswith("http") else API + path, params, **bearer(key))


@pytest.fixture
def key(filer, pge):  # noqa: F811
    return tokens.create_token(filer, pge, name="claims system", scopes=["claims"], days=30)[1]


@pytest.fixture
def outsider(make_user, cpuc_admin, programs):  # noqa: F811
    """A token for SCE's claims reader: it reads PG&E's published versions and nothing else of PG&E's."""
    sce = programs
    u = make_user("claims@sce.example.org")
    admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, admin, sce, "org.admin", reason="first admin")
    access.grant(admin, u, sce, "claims.read")
    return tokens.create_token(u, sce, name="sce", scopes=["claims"], days=30)[1]


def storage_bytes(key: str) -> bytes:
    return storage.store("uploads").get_bytes(key)


def q2() -> bytes:
    return claims_zip(tables(claim("PGE-2028-004", quarter="2028Q2"), Site=[row("Site", SiteID="PGE-S1")]))


def test_published_versions_are_open_the_rest_to_readers(key, outsider, filer, year, pge, release, make_user, cpuc_admin):  # noqa: F811
    v1 = confirmed(filer, pge, q1())
    v2 = service.upload_claims(filer, deadline("Q2 claim"), pge, q2(), "q2.zip")
    assert v2.status == SubmissionVersion.Status.VALIDATED, service.findings(v2)

    body = get(key, "/years").json()
    assert [y["year"] for y in body["years"]] == [2028] and body["years"][0]["acc"] == "2026"
    confirmations = body["years"][0]["confirmations"]
    assert [c["kind"] for c in confirmations] == [k for k, _ in service.PERIODS] and confirmations[0]["label"] == "2028 Q1 claim"

    # the confirmation: the organization sees every version, others only the published ones
    q1_period = get(key, f"/periods/{deadline().pk}").json()
    assert q1_period["organisation"] == "PGE" and q1_period["standing"] == v1.pk
    assert [(v["number"], v["status"]) for v in q1_period["versions"]] == [(1, "published")]
    assert [v["id"] for v in get(key, f"/periods/{deadline('Q2 claim').pk}").json()["versions"]] == [v2.pk]
    seen = get(outsider, f"/periods/{deadline('Q2 claim').pk}", org="PGE").json()
    assert seen["organisation"] == "PGE" and seen["versions"] == [] and seen["standing"] is None
    assert get(outsider, f"/periods/{deadline().pk}", org="PGE").json()["versions"][0]["id"] == v1.pk
    assert get(key, "/periods/999999").status_code == 404

    # a version: check, priced totals, files; findings only for the organization and CPUC readers
    mine = get(key, f"/versions/{v1.pk}").json()
    assert mine["organisation"] == "PGE" and mine["status"] == "published" and mine["priced"] and mine["late"] is False
    assert mine["check"]["records"] and mine["check"]["rejected"] == 0 and mine["calculation"]["acc"] == "2026"
    assert mine["totals"]["TotalSystemBenefit"] == 4900 and mine["totals"]["claims"] == 2
    assert len(mine["files"]) == 5 and mine["findings"].endswith(f"/versions/{v1.pk}/findings") and mine["change_letter"] is None
    theirs = get(outsider, f"/versions/{v1.pk}").json()
    assert theirs["findings"] is None and theirs["totals"] == mine["totals"]
    assert get(outsider, f"/versions/{v1.pk}/findings").status_code == 404
    assert get(outsider, f"/versions/{v2.pk}").status_code == 404 and get(outsider, f"/versions/{v2.pk}/results").status_code == 404
    found = get(key, f"/versions/{v2.pk}/findings").json()
    assert found["count"] == len(service.findings(v2)) and found["next"] is None

    # results by program, the same rows as the page's CSV, and one credited PA's share
    res = get(outsider, f"/versions/{v1.pk}/results").json()
    rows = {r["PrgID"]: r for r in res["programs"]}
    assert res["priced"] and rows["PGE_Res"]["TotalSystemBenefit"] == 4500 and rows["PGE_Res"]["Claims"] == 1
    assert rows["PGE_Com"]["AdminCostsOverheadAndGA"] == 300 and res["total"]["TotalSystemBenefit"] == 4900
    assert res["total"]["Expenditures"] == mine["totals"]["expenditures"]
    assert get(key, f"/versions/{v1.pk}/results", pa="SCE").json()["total"]["TotalSystemBenefit"] is None

    # the published files, by their place in the list
    got = get(outsider, mine["files"][0]["url"])
    assert got.status_code == 200 and b"".join(got.streaming_content)
    assert get(outsider, f"/versions/{v1.pk}/files/99").status_code == 404

    # CPUC staff with claims.read read every organization's versions and findings; a token without the claims scope reaches nothing
    staff = make_user("analyst@cpuc.example.org")
    cpuc = access.cpuc()
    access.grant(cpuc_admin, staff, cpuc, "claims.read")
    cpuc_key = tokens.create_token(staff, cpuc, name="ed", scopes=["claims"], days=30)[1]
    assert get(cpuc_key, f"/versions/{v2.pk}").json()["findings"].endswith(f"/versions/{v2.pk}/findings")
    assert get(cpuc_key, f"/versions/{v2.pk}/findings").status_code == 200
    assert [v["id"] for v in get(cpuc_key, f"/periods/{deadline('Q2 claim').pk}", org="PGE").json()["versions"]] == [v2.pk]
    calc_key = tokens.create_token(filer, pge, name="calcs", scopes=["calculations"], days=30)[1]
    for path in ("/years", f"/versions/{v1.pk}", f"/versions/{v1.pk}/results", "/changes", "/claims/PGE-2028-001/chain"):
        assert get(calc_key, path).status_code == 403, path
    assert Client().get(f"{API}/years").status_code in (401, 403)


def test_change_letters_and_nmec_chains(key, outsider, filer, year, pge, programs, release, cpuc_admin):  # noqa: F811
    cost = {"ProgramCost": [row("ProgramCost", PrgID="PGE_Res")], "Site": [row("Site", SiteID="PGE-S1")]}
    v1 = confirmed(filer, pge, claims_zip(tables(linked("PGE-2028-001"), linked("PGE-2028-002", impact="Cust"), cost)))
    # a correction of Q1 after its due date: its change letter is listed and read by anyone
    assert get(outsider, "/changes").json() == {"count": 0, "next": None, "letters": []}
    submissions.move_deadline(cpuc_admin, deadline(), timezone.now() - timedelta(days=1), "test")
    submissions.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    v3 = service.request_reopen(filer, v1.submission, "PGE-2028-002 was canceled")
    v3 = service.upload_claims(
        filer, deadline(), pge, claims_zip(tables(linked("PGE-2028-001"), withdraw("PGE-2028-002"), cost)), "correction.zip"
    )
    assert v3.status == SubmissionVersion.Status.VALIDATED, service.findings(v3)
    finish_run(Run.objects.get(pk=service.price(filer, v3)))
    v3.refresh_from_db()
    v3 = service.confirm(filer, v3)
    letter = ChangeLetter.objects.get()
    listed = get(outsider, "/changes", org="PGE", year=2028).json()
    assert listed["count"] == 1 and listed["letters"][0]["id"] == letter.pk
    assert listed["letters"][0]["version"] == v3.number and listed["letters"][0]["replaces"] == v1.number
    assert get(outsider, "/changes", year=2027).json()["count"] == 0
    one = get(outsider, listed["letters"][0]["url"]).json()
    assert one["reason"] == "PGE-2028-002 was canceled" and one["changes"]["claims"]["withdrawn"]["count"] == 1
    assert get(key, f"/versions/{v3.pk}").json()["change_letter"] == listed["letters"][0]["url"]
    assert get(key, f"/versions/{v3.pk}").json()["withdrawals"][0]["claim"] == "PGE-2028-002"
    assert get(outsider, "/changes/999999").status_code == 404

    # a true-up of Q1's NMEC claim in Q2: its chain
    second = tables(linked("PGE-2028-010", "2028Q2", parent="PGE-2028-001", TotalFirstYearNetkWh="1000"))
    v2 = confirmed(filer, pge, claims_zip(second), kind="Q2 claim")

    body = get(outsider, "/claims/pge-2028-010/chain").json()
    assert body["claim"] == "pge-2028-010"
    assert [(c["claim"], c["depth"], c["parent"], c["true_up"]) for c in body["chain"]] == [
        ("PGE-2028-001", 0, None, False),
        ("PGE-2028-010", 1, "PGE-2028-001", True),
    ]
    assert body["chain"][1]["change_from_parent"]["TotalFirstYearNetkWh"] == 200
    assert body["chain"][1]["version"].endswith(f"/versions/{v2.pk}")
    assert get(outsider, "/claims/PGE-2028-404/chain").status_code == 404


def test_the_openapi_description_lists_the_claims_api(client):
    schema = client.get("/api/v1/schema", HTTP_ACCEPT="application/vnd.oai.openapi+json").json()
    assert {
        "/api/v1/claims/years",
        "/api/v1/claims/periods/{id}",
        "/api/v1/claims/periods/{id}/upload",
        "/api/v1/claims/versions/{id}",
        "/api/v1/claims/versions/{id}/check",
        "/api/v1/claims/versions/{id}/price",
        "/api/v1/claims/versions/{id}/confirm",
        "/api/v1/claims/versions/{id}/findings",
        "/api/v1/claims/versions/{id}/results",
        "/api/v1/claims/versions/{id}/files/{which}",
        "/api/v1/claims/changes",
        "/api/v1/claims/changes/{id}",
        "/api/v1/claims/claims/{claim_id}/chain",
    } <= set(schema["paths"])


# --- writes


def post(key, path, data=None, **kw):
    return Client().post(path if path.startswith("http") else API + path, data or {}, **bearer(key), **kw)


def upload_file(key, d, content: bytes, name="claims.zip"):
    return post(key, f"/periods/{d.pk}/upload", {"file": SimpleUploadedFile(name, content)})


def test_a_pa_uploads_checks_prices_and_confirms_through_the_api(key, outsider, filer, year, pge, release, make_user, pge_admin):  # noqa: F811
    q1_deadline = deadline()
    assert upload_file(key, q1_deadline, b"not a zip").status_code == 400

    # upload: checked at once, the version's url in Location
    up = upload_file(key, q1_deadline, q1())
    assert up.status_code == 201, up.json()
    v = up.json()
    assert v["status"] == "validated" and v["number"] == 1 and up["Location"] == v["url"]
    assert v["check"]["rejected"] == 0 and not v["priced"] and v["pricing_run"] is None and v["stale"] is False

    # confirming needs a finished run; pricing starts one, once
    refused = post(key, f"/versions/{v['id']}/confirm")
    assert refused.status_code == 409 and "Price this version first" in refused.json()["detail"]
    priced = post(key, f"/versions/{v['id']}/price")
    assert priced.status_code == 202, priced.json()
    run = priced.json()["pricing_run"]
    assert run["url"].endswith(f"/api/v1/calculations/runs/{run['id']}")
    assert post(key, f"/versions/{v['id']}/price").status_code == 409
    again = post(key, f"/versions/{v['id']}/price", {"again": True}, content_type="application/json")
    assert again.status_code == 202 and again.json()["pricing_run"]["id"] != run["id"]
    finish_run(Run.objects.get(pk=again.json()["pricing_run"]["id"]))
    assert get(key, f"/versions/{v['id']}").json()["priced"] is True
    assert post(key, f"/versions/{v['id']}/price").status_code == 409

    # who may: another organization's token sees no unpublished version; a reader cannot confirm; a token needs the claims scope
    assert post(outsider, f"/versions/{v['id']}/confirm").status_code == 404
    assert upload_file(outsider, q1_deadline, q1()).status_code == 403
    reader = make_user("reader@pge.example.org")
    access.grant(pge_admin, reader, pge, "claims.read")
    reader_key = tokens.create_token(reader, pge, name="r", scopes=["claims"], days=30)[1]
    assert post(reader_key, f"/versions/{v['id']}/confirm").status_code == 403
    assert post(reader_key, f"/versions/{v['id']}/price", {"again": True}, content_type="application/json").status_code == 403
    calc_key = tokens.create_token(filer, pge, name="calcs", scopes=["calculations"], days=30)[1]
    assert post(calc_key, f"/versions/{v['id']}/confirm").status_code == 403
    assert upload_file(calc_key, q1_deadline, q1()).status_code == 403
    assert Client().post(f"{API}/versions/{v['id']}/confirm").status_code in (401, 403)

    # confirm: published; a confirmed version is neither checked again nor uploaded over, and its files are untouched
    done = post(key, f"/versions/{v['id']}/confirm")
    assert done.status_code == 200, done.json()
    assert done.json()["status"] == "published" and done.json()["confirmed_by"] == filer.email
    published = SubmissionVersion.objects.get(pk=v["id"])
    findings_key = published.findings["compiled_key"]
    before = storage_bytes(findings_key)
    assert post(key, f"/versions/{v['id']}/check").status_code == 409
    assert storage_bytes(findings_key) == before
    assert upload_file(key, q1_deadline, q1()).status_code == 409

    # the next quarter, checked again when asked
    up2 = upload_file(key, deadline("Q2 claim"), q2())
    assert up2.status_code == 201 and up2.json()["status"] == "validated", up2.json()
    rechecked = post(key, f"/versions/{up2.json()['id']}/check")
    assert rechecked.status_code == 200 and rechecked.json()["check"]["base_label"] == "2028 Q1 claim version 1"


def test_a_step_acts_only_on_the_confirmations_latest_version(key, filer, year, pge, programs, release, cpuc_admin):  # noqa: F811
    v1 = confirmed(filer, pge, q1())
    submissions.move_deadline(cpuc_admin, deadline(), timezone.now() - timedelta(days=1), "test")
    submissions.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    v2 = service.request_reopen(filer, v1.submission, "a correction")
    for step in ("check", "price", "confirm"):
        r = post(key, f"/versions/{v1.pk}/{step}")
        assert r.status_code == 409 and f"version {v2.number} is" in r.json()["detail"], step
    assert post(key, f"/versions/{v2.pk}/check").status_code == 200


def test_the_api_takes_several_files(key, year, programs):  # noqa: F811
    r = post(key, f"/periods/{deadline().pk}/upload", {"file": split(q1())})
    assert r.status_code == 201 and r.json()["status"] == "validated", r.content
