"""Correcting a budget filing's records in place (BR-02.4): the submissions service's corrections with the Budgets module's editor."""

import io
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.modules.budgets import jobfile, service
from cedars.modules.budgets.models import BudgetLine, ChangeLetter
from cedars.modules.calculations.models import Run
from cedars.services.audit.models import AuditEvent
from cedars.services.rules import service as rules
from cedars.services.submissions import corrections
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .conftest import page_only, sign_in
from .test_budgets import filer, filing, finish_run, job, programs, release  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

BAD_PROGRAM = [
    "PGE_Res_HVAC|2028|2028Q1|PGE|1000|500|200|0|0|0|0|100|0|5000|0",
    "NOPE|2029|2029Q1|PGE|1100|500|200|0|0|0|0|100|0|0|0",
    "PGE_Com_Ltg|2028|2028Q1|PGE|300|0|0|0|0|0|0|0|0|0|0",
]


def measures(v) -> list[dict]:
    return corrections.editor(v).rows(v)["Measure"].rows


def test_a_rejected_record_is_corrected_and_the_lines_follow(filer, filing, pge, programs):  # noqa: F811
    v = service.upload(filer, filing, pge, job(costs=BAD_PROGRAM), "tual.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    assert {(f["row"], f["rule_id"]) for f in service.findings(v)} >= {(3, "BUD-PRG")}
    sha = service.job_file(v)["sha256"]
    v, scope = corrections.correct(
        filer,
        v,
        "ProgramCost",
        ["NOPE", "2029"],
        {"PrgID": "PGE_Res_HVAC", "PrgYear": "2029"},
        reason="typo",
        expected=corrections.state(v),
    )
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    f = service.job_file(v)
    assert f["sha256"] != sha and f["corrected"] and f["uploaded_as"] == "tual.zip"
    assert scope.changed == {"ProgramCost": {3}}
    assert BudgetLine.objects.get(version=v, prg_id="PGE_Res_HVAC", year=2029).program_costs["AdminCostsOverheadAndGA"] == "1100"
    assert "Checked again after a correction" in v.findings["notes"][0] and v.findings["checked_again"]["records"] == 1
    ev = AuditEvent.objects.get(action=corrections.ACTION)
    assert (
        ev.after["key"] == "PGE_Res_HVAC / 2029"
        and ev.before["values"]["PrgID"] == "NOPE"
        and ev.after["fields"] == ["PrgID"]
        and ev.reason == "typo"
    )
    # the kept job is the engine's: two CSV tables, the correction in place, every other row as filed
    tables, _ = jobfile.read_upload(_bytes(f), "job.zip")
    header, costs = jobfile.rows(tables["ProgramCost"])
    assert [r["PrgID"] for r in costs] == ["PGE_Res_HVAC", "PGE_Res_HVAC", "PGE_Com_Ltg"] and "OnBillFinancing" in header
    assert len(jobfile.rows(tables["Measure"])[1]) == 3


def _bytes(f) -> bytes:
    return storage.store("uploads").get_bytes(f["key"])


@pytest.fixture
def fake_rules(monkeypatch):
    """A rule set in force whose one rule rejects a measure with NumUnits "bad": the budget filing rules without the engine."""
    rs = SimpleNamespace(label="Test rules", effective_from="2026-01-01")

    def on(frames, covers, _rs):
        out = []
        for t, rows in frames.items():
            for i, r in enumerate(rows, start=2):
                if jobfile.get(r, "NumUnits") == "bad":
                    out.append(service.Problem(i, jobfile.get(r, "CEInputID"), t, "NumUnits", "V-TEST", "reject", "bad", "fix"))
        return out, ["The test rules ran."]

    def full(tables, cycle):
        out, notes = on({t: jobfile.rows(tables[t])[1] for t in jobfile.TABLES}, cycle.covers, rs)
        return out, notes, "Test rules (2026-01-01)"

    monkeypatch.setattr(service, "_rule_set", lambda: rs)
    monkeypatch.setattr(service, "_rule_findings", full)
    monkeypatch.setattr(service, "_rule_findings_on", on)
    monkeypatch.setattr(rules, "read_table", lambda content: jobfile.rows(content)[1])
    return full


def test_only_the_changed_records_go_through_the_rules_again(filer, filing, pge, programs, fake_rules):  # noqa: F811
    data = job(
        measures=[
            "M1|PGE_Res_HVAC|2028Q1|bad|20|30|5|5",
            "M2|PGE_Res_HVAC|2029Q2|2|100|0|0|0",
            "M3|PGE_Com_Ltg|2028Q3|bad|25|0|0|0",
        ]
    )
    v = service.upload(filer, filing, pge, data, "tual.zip")
    assert {(f["row"], f["rule_id"]) for f in service.findings(v) if f["table"] == "Measure"} >= {(2, "V-TEST"), (4, "V-TEST")}
    v, _ = corrections.correct(filer, v, "Measure", ["M1"], {"NumUnits": "10"})
    v, scope = corrections.delete(filer, v, "Measure", ["M2"])
    assert scope.line_map["Measure"] == {2: 2, 3: None, 4: 3}
    found = {(f["row"], f["record_id"], f["rule_id"]) for f in service.findings(v) if f["rule_id"] == "V-TEST"}
    assert found == {(3, "M3", "V-TEST")}  # M3 was not checked again: its finding moved to its new line
    assert v.findings["checked_again"]["records"] == 0 and v.findings["rule_set"] == "Test rules (2026-01-01)"
    # the scoped check finds what a full check finds
    tables, _ = jobfile.read_upload(_bytes(service.job_file(v)), "job.zip")
    full, _, _ = fake_rules(tables, filing.cycle)
    assert {(p.row, p.record_id) for p in full} == {(r, rid) for r, rid, _ in found}
    # a key shared with a changed record is checked again with it
    v, _ = corrections.correct(filer, v, "Measure", ["M1"], {"NumUnits": "bad"})
    assert v.findings["checked_again"]["records"] == 1
    assert {(f["row"], f["record_id"]) for f in service.findings(v) if f["rule_id"] == "V-TEST"} == {(2, "M1"), (3, "M3")}


def test_a_partial_correction_of_a_budget_filing(filer, filing, pge, programs):  # noqa: F811
    v = service.upload(filer, filing, pge, job(costs=BAD_PROGRAM), "tual.zip")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Measure.csv", "CEInputID,PrgID,ClaimYearQuarter,NumUnits\nM1,PGE_Res_HVAC,2028Q1,12\nM4,PGE_Com_Ltg,2028Q2,3\n")
        z.writestr("Delete.csv", "Table,PrgID,PrgYear\nProgramCost,NOPE,2029\n")
    p = corrections.preview(filer, v, out.getvalue(), "fix.zip")
    assert p.counts == {"Measure": {"edit": 1, "add": 1}, "ProgramCost": {"delete": 1}}
    v, _ = corrections.apply_preview(filer, v, p.token, expected=p.state, reason="units")
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    rows = {r["CEInputID"]: r for r in measures(v)}
    assert rows["M1"]["NumUnits"] == "12" and rows["M1"]["UnitEndUserRebate"] == "20" and rows["M4"]["NumUnits"] == "3"
    assert rows["M4"]["UnitEndUserRebate"] == ""  # an added record has only the columns given
    assert not BudgetLine.objects.filter(version=v, prg_id="NOPE").exists()
    # a lone CSV is one table, named by its file
    p = corrections.preview(filer, v, b"PrgID|PrgYear|EMV\nPGE_Com_Ltg|2028|50\n", "ProgramCost.csv")
    assert p.counts == {"ProgramCost": {"edit": 1}}


def test_the_filing_page_and_record_pages(client, filer, filing, pge, programs):  # noqa: F811
    v = service.upload(filer, filing, pge, job(costs=BAD_PROGRAM), "tual.zip")
    sign_in(client, filer)
    page = page_only(client.get(reverse("budgets:filing", args=[filing.pk])).content.decode())
    assert "Correct records" in page and reverse("submissions:records", args=[v.pk]) in page
    page = page_only(client.get(reverse("submissions:records", args=[v.pk]) + "?q=NOPE").content.decode())
    assert "Found: 1 record" in page and "ProgramCost" in page
    url = reverse("submissions:record", args=[v.pk, "ProgramCost"]) + "?k=NOPE&k=2029"
    page = page_only(client.get(url).content.decode())
    assert "BUD-PRG" in page and "Administration: overhead and G&amp;A" in page and "OnBillFinancing" in page
    r = client.post(url, {"f-PrgID": "PGE_Res_HVAC", "f-PrgYear": "2029", "reason": "typo", "state": corrections.state(v)})
    assert r.status_code == 302
    v.refresh_from_db()
    assert v.status == SubmissionVersion.Status.VALIDATED


def test_a_reopened_filing_is_checked_then_corrected_and_its_letter_says_so(
    client,
    filer,  # noqa: F811
    filing,  # noqa: F811
    pge,
    programs,  # noqa: F811
    release,  # noqa: F811
    cpuc_admin,
):
    v = service.upload(filer, filing, pge, job(), "tual.zip")
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    v = service.confirm(filer, v)
    submissions.move_deadline(cpuc_admin, filing, timezone.now() - timedelta(minutes=1), "closed")
    submissions.set_policy(cpuc_admin, "budgets", reopen_after_due="open")
    v2 = submissions.request_reopen(filer, v.submission, "M2 had 3 units")
    sign_in(client, filer)
    assert client.get(reverse("submissions:records", args=[v2.pk])).status_code == 200
    v2.refresh_from_db()
    assert v2.status == SubmissionVersion.Status.VALIDATED and v2.findings  # its predecessor's job, checked on opening
    assert all(storage.store("uploads").exists(f["key"]) for f in v.files)
    v2, _ = corrections.correct(filer, v2, "Measure", ["M2"], {"NumUnits": "3"})
    assert all(storage.store("uploads").exists(f["key"]) for f in v.files)  # the published version's files stay
    finish_run(Run.objects.get(pk=service.price(filer, v2)))
    v2.refresh_from_db()
    v2 = service.confirm(filer, v2)
    letter = ChangeLetter.objects.get(version=v2)
    assert letter.changes["in_place"]["edit"] == 1
    assert "1 changed" in page_only(client.get(reverse("budgets:change_letter", args=[letter.pk])).content.decode())
    body = service.notice(v2, SimpleNamespace(action="submission.published")).body
    assert "Corrected record by record: 1 record (1 changed)." in body
