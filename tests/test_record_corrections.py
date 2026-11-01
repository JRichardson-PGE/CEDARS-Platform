"""Correcting a filing's records in place (BR-02.4): the submissions service's corrections, with the Claims module's editor."""

import io
import zipfile

import pytest

from cedars.core import storage
from cedars.modules.claims import service
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import corrections
from cedars.services.submissions.corrections import CorrectionRefused
from cedars.services.submissions.models import SubmissionVersion

from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claim, claims_zip, confirmed, deadline, filer, programs, q1, row, tables, year  # noqa: F401

pytestmark = pytest.mark.django_db


def bad_q1() -> bytes:
    """Q1 with one rejected record: PGE-2028-002's measure is for a program not in the registry."""
    return claims_zip(
        tables(
            claim("PGE-2028-001"),
            claim("PGE-2028-002", prg="NOPE"),
            claim("PGE-2028-003", prg="PGE_Com"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res"), row("ProgramCost", PrgID="PGE_Com")],
            Site=[row("Site", SiteID="PGE-S1")],
        )
    )


def lines_of(v, table):
    return {(f["row"], f["rule_id"]) for f in service.findings(v) if f["table"] == table}


def test_a_rejected_record_is_corrected_in_place_and_only_its_records_are_checked_again(filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    assert (3, "CLM-PRG") in lines_of(v, "Measure")
    before_sha = service.own_file(v)["sha256"]
    v, scope = corrections.correct(
        filer, v, "Measure", ["PGE-2028-002"], {"PrgID": "PGE_Res"}, reason="typo", expected=corrections.state(v)
    )
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    assert service.own_file(v)["sha256"] != before_sha and service.own_file(v)["corrected"]
    assert scope.changed == {"Measure": {3}} and scope.line_map["Measure"] == {2: 2, 3: 3, 4: 4}
    s = v.findings
    # the measure, its claim (same ClaimID) and the program's cost record's other measures are not all re-checked: the claim's records
    assert s["checked_again"]["records"] == 2 and "Checked again after a correction" in s["notes"][0]
    ev = AuditEvent.objects.get(action="submission.record_corrected")
    assert ev.after["key"] == "PGE-2028-002" and ev.after["fields"] == ["PrgID"] and ev.before["values"]["PrgID"] == "NOPE"
    assert ev.after["values"]["PrgID"] == "PGE_Res" and ev.reason == "typo" and ev.after["via"] == {"how": "record editor"}
    assert AuditEvent.objects.filter(action="submission.files_attached", object_id=str(v.pk)).count() == 2
    assert corrections.summary(v) == {
        "records": 1,
        "edit": 1,
        "add": 0,
        "delete": 0,
        "tables": {"Measure": {"edit": 1, "add": 0, "delete": 0}},
        "partial": 0,
    }


def test_other_records_keep_their_findings_on_their_new_lines(filer, year, pge, programs):  # noqa: F811
    data = claims_zip(
        tables(
            claim("PGE-2028-001", prg="NOPE"),
            claim("PGE-2028-002", prg="NOPE"),
            claim("PGE-2028-003", prg="NOPE"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res")],
            Site=[row("Site", SiteID="PGE-S1")],
        )
    )
    v = service.upload_claims(filer, deadline(), pge, data, "q1.zip")
    assert {r for r, rule in lines_of(v, "Measure") if rule == "CLM-PRG"} == {2, 3, 4}
    v, _ = corrections.delete(filer, v, "Measure", ["PGE-2028-001"])
    v, _ = corrections.delete(filer, v, "Claim", ["PGE-2028-001"])
    # PGE-2028-003 moved from line 4 to 3 and kept its finding; 002 moved to 2 and kept its
    assert {r for r, rule in lines_of(v, "Measure") if rule == "CLM-PRG"} == {2, 3}
    full = service.check(filer, v)
    assert sorted((f["table"], f["row"], f["rule_id"]) for f in service.findings(full)) == sorted(
        (f["table"], f["row"], f["rule_id"]) for f in service.findings(v)
    )


def test_a_scoped_check_finds_what_a_full_check_finds(filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    # deleting a program's cost record breaks its measures' references: they are checked again too
    v, _ = corrections.delete(filer, v, "ProgramCost", ["PGE_Com", "2028"])
    scoped = sorted((f["table"], f["row"], f["rule_id"], f["message"]) for f in service.findings(v))
    assert ("Measure", 4, "CLM-REF", "PGE_Com has no 2028 ProgramCost record") in scoped
    full = service.check(filer, v)
    assert scoped == sorted((f["table"], f["row"], f["rule_id"], f["message"]) for f in service.findings(full))


def test_adding_and_carrying_in_an_earlier_quarters_record(filer, year, pge, programs, release):  # noqa: F811
    first = tables(
        claim("PGE-2028-001"),
        ProgramCost=[row("ProgramCost", PrgID="PGE_Res")],
        Site=[row("Site", SiteID="PGE-S1")],
        ContactClaim=[row("ContactClaim", ClaimID="PGE-2028-001", ContactID=c) for c in ("PGE-C1", "PGE-C2")],
    )
    confirmed(filer, pge, claims_zip(first))
    q2 = claims_zip(tables(claim("PGE-2028-010", quarter="2028Q2")))
    v = service.upload_claims(filer, deadline("Q2 claim"), pge, q2, "q2.zip")
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    found, n = corrections.find(v, query="PGE-2028-001")
    assert n == 4 and {f.table for f in found} == {"Claim", "Measure", "ContactClaim"} and all(f.earlier for f in found)
    v, scope = corrections.correct(filer, v, "Measure", ["PGE-2028-001"], {"NumUnits": "12"})
    assert scope.changes[0].earlier == "the 2028 Q1 claim (version 1)" and v.status == SubmissionVersion.Status.VALIDATED
    assert v.findings["replaced"] == {"Measure": 1}
    found, _ = corrections.find(v, "Measure", "PGE-2028-001")
    assert [f.line for f in found] == [3] and not found[0].earlier  # now this version's own record
    with pytest.raises(CorrectionRefused, match="Withdrawal table"):
        corrections.delete(filer, v, "Claim", ["PGE-2028-001"])
    with pytest.raises(CorrectionRefused, match="find it and correct it"):
        corrections.add(filer, v, "Claim", row("Claim", ClaimID="PGE-2028-001", SiteID="PGE-S1"))
    # a claim's contacts are one set: deleting an earlier one carries the rest of the set in, so this version's set stands
    v, scope = corrections.delete(filer, v, "ContactClaim", ["PGE-2028-001", "PGE-C2"])
    assert [(c.kind, c.key) for c in scope.changes] == [("carry", "PGE-2028-001 / PGE-C1"), ("delete", "PGE-2028-001 / PGE-C2")]
    contacts = service.compiled_tables(v)["ContactClaim"].rows
    assert [r["ContactID"] for r in contacts] == ["PGE-C1"]
    v, _ = corrections.add(filer, v, "ContactClaim", {"ClaimID": "PGE-2028-001", "ContactID": "PGE-C3"})
    assert sorted(r["ContactID"] for r in service.compiled_tables(v)["ContactClaim"].rows) == ["PGE-C1", "PGE-C3"]
    with pytest.raises(CorrectionRefused, match="cannot be left empty|not one of"):
        corrections.delete(filer, v, "ContactClaim", ["PGE-2028-001", "PGE-C9"])
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)


def test_who_and_when(filer, year, pge, programs, make_user, pge_admin, release):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    reader = make_user("reader@pge.com")
    access.grant(pge_admin, reader, pge, "claims.read")
    assert "claims.submit" in corrections.refusal(reader, v)
    with pytest.raises(AccessDenied):
        corrections.correct(reader, v, "Measure", ["PGE-2028-002"], {"PrgID": "PGE_Res"})
    with pytest.raises(CorrectionRefused, match="changed after you opened"):
        corrections.correct(filer, v, "Measure", ["PGE-2028-002"], {"PrgID": "PGE_Res"}, expected="stale")
    with pytest.raises(CorrectionRefused, match="Nothing changed"):
        corrections.correct(filer, v, "Measure", ["PGE-2028-002"], {"PrgID": "NOPE"})
    v, _ = corrections.correct(filer, v, "Measure", ["PGE-2028-002"], {"PrgID": "PGE_Res"})
    from cedars.modules.calculations.models import Run

    from .test_claims import finish_run

    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    v = service.confirm(filer, v)
    assert "never changes" in corrections.refusal(filer, v)


def partial(t: dict, deletes: list[dict] | None = None) -> bytes:
    data = claims_zip(t) if t else b""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        if data:
            for info in zipfile.ZipFile(io.BytesIO(data)).infolist():
                z.writestr(info.filename, zipfile.ZipFile(io.BytesIO(data)).read(info))
        if deletes:
            cols = list(dict.fromkeys(c for d in deletes for c in d))
            z.writestr("Delete.csv", ",".join(cols) + "\n" + "".join(",".join(d.get(c, "") for c in cols) + "\n" for d in deletes))
    return out.getvalue()


def test_a_partial_correction_is_previewed_then_applied(filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    data = partial(
        {"Measure": [{"ClaimID": "PGE-2028-002", "PrgID": "PGE_Res"}, {"ClaimID": "PGE-2028-001", "PrgID": "PGE_Res"}]},
        deletes=[{"Table": "Measure", "ClaimID": "PGE-2028-003"}, {"Table": "Claim", "ClaimID": "PGE-2028-003"}],
    )
    p = corrections.preview(filer, v, data, "fix.zip")
    assert p.counts == {"Measure": {"edit": 1, "delete": 1}, "Claim": {"delete": 1}} and p.unchanged == 1
    assert [c.fields for c in p.changes if c.kind == "edit"] == [["PrgID"]]
    v.refresh_from_db()
    assert v.status == SubmissionVersion.Status.DRAFT  # nothing changed by a preview
    assert "PrgID,NOPE,PGE_Res" in corrections.changes_csv(p, corrections.tables_of(v)).replace("Changed,", "")
    v, scope = corrections.apply_preview(filer, v, p.token, p.state)
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    assert v.findings["tables"]["Measure"] == 2
    vias = {e.after["via"]["how"] for e in AuditEvent.objects.filter(action="submission.record_corrected")}
    assert vias == {"partial correction"} and corrections.summary(v)["partial"] == 1
    with pytest.raises(CorrectionRefused, match="no longer kept|changed after"):
        corrections.apply_preview(filer, v, p.token, p.state)


def test_a_partial_correction_refuses_what_it_cannot_apply(filer, year, pge, programs, settings):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    with pytest.raises(CorrectionRefused, match="Delete table"):
        corrections.preview(filer, v, partial({}, [{"Table": "Nope", "ClaimID": "X"}]), "fix.zip")
    with pytest.raises(CorrectionRefused, match="not one of this version"):
        corrections.preview(filer, v, partial({}, [{"Table": "Claim", "ClaimID": "PGE-2028-999"}]), "fix.zip")
    two = partial({"Measure": [{"ClaimID": "PGE-2028-002", "PrgID": "A"}, {"ClaimID": "pge-2028-002 ", "PrgID": "B"}]})
    with pytest.raises(CorrectionRefused, match="keep one"):
        corrections.preview(filer, v, two, "fix.zip")
    settings.CEDARS_CORRECTION_MAX_ROWS = 1
    many = partial({"Measure": [{"ClaimID": "PGE-2028-002", "PrgID": "PGE_Res"}, {"ClaimID": "PGE-2028-003", "PrgID": "PGE_Res"}]})
    with pytest.raises(CorrectionRefused, match="at most 1"):
        corrections.preview(filer, v, many, "fix.zip")
    # one table's CSV on its own
    p = corrections.preview(filer, v, b"ClaimID,PrgID\nPGE-2028-002,PGE_Res\n", "Measure.csv")
    assert p.counts == {"Measure": {"edit": 1}}
    store = storage.store("uploads")
    assert any(store.list(f"submissions/versions/{v.pk}/corrections/"))


# --- the pages


def test_the_pages_find_change_add_and_delete_records(client, filer, year, pge, programs, make_user, pge_admin):  # noqa: F811
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    from .conftest import page_only, sign_in

    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    sign_in(client, filer)
    period = client.get(reverse("claims:period", args=[deadline().pk])).content.decode()
    assert reverse("submissions:records", args=[v.pk]) in period
    url = reverse("submissions:records", args=[v.pk])
    r = client.get(url)
    assert r.status_code == 200 and "Find a record" in r.content.decode() and "Upload a partial correction" in r.content.decode()
    r = client.get(url, {"table": "Measure", "q": "2028-002"}).content.decode()
    assert "Found: 1 record" in r and "This version, line 3" in r
    edit = reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-002"
    r = client.get(edit).content.decode()
    assert "Findings on this record: 1, rejected" in r and 'aria-invalid="true"' in r and "CLM-PRG" in r
    state = corrections.state(v)
    form = {f"f-{c}": val for c, val in service.own_tables(v)["Measure"].rows[1].items()}
    r = client.post(edit, {**form, "f-PrgID": "PGE_Res", "state": state, "reason": "typo", "k": "PGE-2028-002"}, follow=True)
    text = r.content.decode()
    assert "Saved 1 record and checked 2 records again: none is rejected" in text and "No findings on this record" in text
    v.refresh_from_db()
    assert v.status == SubmissionVersion.Status.VALIDATED
    # a stale form is refused
    r = client.post(edit, {**form, "f-NumUnits": "3", "state": state, "k": "PGE-2028-002"}, follow=True)
    assert "changed after you opened" in r.content.decode()
    # add, then delete
    new = reverse("submissions:record_new", args=[v.pk, "Site"])
    assert "Add a Site record" in client.get(new).content.decode()
    r = client.post(new, {"f-SiteID": "PGE-S9", "f-SiteState": "CA", "f-SiteZipCode": "94111", "f-Residential_Flag": "1",
                          "state": corrections.state(v)}, follow=True)  # fmt: skip
    assert "Saved 1 record" in r.content.decode()
    v.refresh_from_db()
    gone = reverse("submissions:record_delete", args=[v.pk, "Site"]) + "?k=PGE-S9"
    assert "Delete Site PGE-S9?" in client.get(gone).content.decode()
    r = client.post(gone, {"state": corrections.state(v), "k": "PGE-S9"}, follow=True)
    v.refresh_from_db()
    assert "Saved 1 record" in r.content.decode() and service.own_tables(v)["Site"].rows[-1]["SiteID"] == "PGE-S1"
    # a partial correction: preview (nothing changes), the CSV, then apply
    v.refresh_from_db()
    f = SimpleUploadedFile("Measure.csv", b"ClaimID,NumUnits\nPGE-2028-001,11\n")
    r = client.post(reverse("submissions:correction_upload", args=[v.pk]), {"file": f})
    assert r.status_code == 302 and "/records/correction/" in r.url
    page = client.get(r.url).content.decode()
    assert "What would change: 1 record" in page and "NumUnits: 10, now 11" in page
    token = r.url.rstrip("/").rsplit("/", 1)[1]
    csv_text = client.get(reverse("submissions:correction_csv", args=[v.pk, token])).content.decode()
    assert "Measure,PGE-2028-001,Changed,NumUnits,10,11" in csv_text
    r = client.post(reverse("submissions:correction_apply", args=[v.pk, token]), {"state": corrections.state(v)})
    v.refresh_from_db()
    assert r.status_code == 302 and service.own_tables(v)["Measure"].rows[0]["NumUnits"] == "11"
    # a reader of the filing does not reach the editor
    reader = make_user("reader@pge.com")
    access.grant(pge_admin, reader, pge, "claims.read")
    client.logout()
    sign_in(client, reader)
    assert client.get(url).status_code == 404
    assert reverse("submissions:records", args=[v.pk]) not in page_only(client.get(reverse("claims:period", args=[deadline().pk])).content.decode())


def test_a_confirmed_version_says_why_it_is_not_corrected(client, filer, year, pge, programs, release):  # noqa: F811
    from django.urls import reverse

    from .conftest import page_only, sign_in

    v = confirmed(filer, pge, q1())
    sign_in(client, filer)
    r = page_only(client.get(reverse("submissions:records", args=[v.pk])).content.decode())
    assert "These records cannot be corrected now" in r and "never changes" in r and "Find a record" not in r
    r = client.post(reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-001", {"f-NumUnits": "1"})
    assert r.status_code == 200 and "never changes" in r.content.decode()
    v.refresh_from_db()
    assert v.status == SubmissionVersion.Status.PUBLISHED and not AuditEvent.objects.filter(action=corrections.ACTION).exists()


def test_the_change_letter_and_notice_say_what_was_corrected_in_place(filer, year, pge, programs, release, cpuc_admin):  # noqa: F811
    from cedars.services.submissions import service as submissions

    from .test_claims import finish_run

    v = confirmed(filer, pge, q1())
    from datetime import timedelta

    from django.utils import timezone

    submissions.move_deadline(cpuc_admin, deadline(), timezone.now() - timedelta(days=1), "test")
    submissions.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    v2 = service.request_reopen(filer, v.submission, "PGE-2028-002 had 5 units")
    service.ensure_checked(v2)
    v2, _ = corrections.correct(filer, v2, "Measure", ["PGE-2028-002"], {"NumUnits": "5"})
    from cedars.modules.calculations.models import Run

    finish_run(Run.objects.get(pk=service.price(filer, v2)))
    v2.refresh_from_db()
    v2 = service.confirm(filer, v2)
    letter = v2.change_letter
    assert letter.changes["in_place"]["edit"] == 1 and letter.changes["claims"]["changed"]["ids"] == ["PGE-2028-002"]
    from types import SimpleNamespace

    body = service.notice(v2, SimpleNamespace(action="submission.published")).body
    assert "Corrected record by record: 1 record (1 changed)." in body
