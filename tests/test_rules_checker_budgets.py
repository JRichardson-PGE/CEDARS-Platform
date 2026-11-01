"""The pre-upload checker on a budget filing (BR-03.4, gap 23): the same findings as the upload's rule-set step, year by year."""

import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from cedars.modules.budgets import service
from cedars.services.accounts import tokens
from cedars.services.rules import service as rules

from .test_budgets import COSTS, MEASURE, filer, filing, job_rows, programs  # noqa: F401
from .test_rules_checker import api_part, rule_set_part

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")


def test_the_api_gives_a_budget_upload_s_own_rule_findings(filer, filing, pge, programs, cpuc_admin):  # noqa: F811
    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    files = {}
    for table, rows in (("ProgramCost", job_rows(COSTS)), ("Measure", job_rows(MEASURE))):
        cols = list(dict.fromkeys(list(rows[0]) + rules.spec_columns(rs, table)))
        files[table] = ("\n".join(["|".join(cols)] + ["|".join(r.get(c, "") for c in cols) for r in rows]) + "\n").encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for k, content in files.items():
            z.writestr(f"{k}.csv", content)
    v = service.upload(filer, filing, pge, out.getvalue(), "full.zip")
    filed = service.findings(v)
    key = tokens.create_token(filer, pge, name="budget system", scopes=["budgets"], days=30)[1]
    for table, content in files.items():
        r = Client().post(
            "/api/v1/rules/check",
            {"module": "budgets", "table": table, "file": SimpleUploadedFile(f"{table}.csv", content)},
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["rule_set"]["id"] == rs.pk and body["records"] == 3
        assert api_part(body) == rule_set_part(filed, table, rs), table
    # one year only: the other year's records are not run, and say so
    r = (
        Client()
        .post(
            "/api/v1/rules/check",
            {"module": "budgets", "table": "ProgramCost", "filing_year": 2029, "file": SimpleUploadedFile("pc.csv", files["ProgramCost"])},
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )
        .json()
    )
    assert {f["line"] for f in r["findings"]} <= {3} and any("2 records are not for 2029" in n for n in r["notes"])
