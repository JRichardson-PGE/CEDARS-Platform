import io
import zipfile
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from cedars.modules.calculations import dispatch, drafts, service
from cedars.modules.calculations.models import InputDraft
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.retention.service import purgers

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

ed = pytest.importorskip("recet.editor", reason="Edit inputs uses the ReCET engine's editor code")
pytestmark = pytest.mark.django_db


def edit_zip(tmp_path, rows=3, name="job.zip", **kw):
    """A job zip with EUL_Yrs and RUL_Yrs filled in (the pinned engine's cell checks raise on a blank RUL_Yrs: ReCET PR #76)."""
    extra = {}
    path = job_zip(tmp_path, rows=rows, name=name, extra=kw.get("extra"))
    with zipfile.ZipFile(path) as z:
        m = z.read("Measure.csv").decode().splitlines()
        pc = z.read("ProgramCost.csv").decode()
        extra = {n: z.read(n).decode() for n in z.namelist() if n not in ("Measure.csv", "ProgramCost.csv")}
    lines = [m[0] + "|EUL_Yrs|RUL_Yrs"] + [line + "|10|0" for line in m[1:]]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Measure.csv", "\n".join(lines) + "\n")
        z.writestr("ProgramCost.csv", pc)
        for n, v in extra.items():
            z.writestr(n, v)
    return path


@pytest.fixture
def web(analyst):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    return c


def test_a_draft_from_a_job_zip_has_every_cet_column_and_the_engines_checks(analyst, pge, tmp_path):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    m, pc = drafts.frames(d)
    assert d.measure_count == 3 and len(pc) == 1
    assert list(m.columns[: len(ed.MEASURE_COLUMNS)]) == ed.MEASURE_COLUMNS
    assert m.at[0, "CEInputID"] == "M0" and m.at[1, "ClaimYearQuarter"] == "2029Q1"
    found = drafts.issues(d)
    assert any(i.severity == "error" and i.column == "IOU_AC_Territory" and i.row == 0 for i in found)  # blank: the run would stop
    e = AuditEvent.objects.get(action="calculation_draft.created")
    assert e.after == {"source": "Uploaded job", "measures": 3, "program_costs": 1}


def test_jobs_too_large_or_not_a_job_are_refused(analyst, pge, tmp_path):  # noqa: F811
    with pytest.raises(AccessDenied, match="up to 500"):
        drafts.create_from_zip(analyst, pge, "Big", edit_zip(tmp_path, rows=501))
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(AccessDenied, match="not a valid .zip"):
        drafts.create_from_zip(analyst, pge, "Bad", bad)
    with pytest.raises(AccessDenied, match="only Measure.csv and ProgramCost.csv"):
        drafts.create_from_zip(analyst, pge, "Extra", edit_zip(tmp_path, extra={"notes.txt": "x"}, name="extra.zip"))


def test_editing_rows(analyst, pge, tmp_path):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    changed = drafts.set_row(analyst, d, "Measure", 0, {"IOU_AC_Territory": " PGE ", "NumUnits": "1", "Nope": "x"})
    assert changed == {"IOU_AC_Territory": ("", "PGE")}
    assert not any(i.column == "IOU_AC_Territory" and i.row == 0 for i in drafts.issues(d))
    e = AuditEvent.objects.get(action="calculation_draft.row_changed")
    assert e.before == {"table": "Measure", "row": 1, "IOU_AC_Territory": ""} and e.after["IOU_AC_Territory"] == "PGE"
    with pytest.raises(AccessDenied, match="may not contain"):
        drafts.set_row(analyst, d, "Measure", 0, {"MeasDescription": "a|b"})

    i = drafts.add_row(analyst, d, "Measure", like=0)
    m = drafts.frame(d, "Measure")
    assert i == 3 and m.at[3, "CEInputID"] == "NEW_1" and m.at[3, "IOU_AC_Territory"] == "PGE" and m.at[3, "NumUnits"] == "1"
    j = drafts.add_row(analyst, d, "Measure", like=0, copy=True)
    assert drafts.frame(d, "Measure").at[j, "CEInputID"] == "M0_1"
    drafts.delete_row(analyst, d, "Measure", j)
    assert d.measure_count == 4
    k = drafts.add_row(analyst, d, "ProgramCost")
    assert drafts.frame(d, "ProgramCost").iloc[k].eq("").all()

    one = drafts.create_from_zip(analyst, pge, "One", edit_zip(tmp_path, rows=1, name="one.zip"))
    with pytest.raises(AccessDenied, match="at least one measure"):
        drafts.delete_row(analyst, one, "Measure", 0)
    with pytest.raises(AccessDenied, match="not in the table"):
        drafts.set_row(analyst, one, "Measure", 5, {"NumUnits": "2"})


def test_who_sees_and_changes_a_draft(analyst, pge, make_user, tmp_path):  # noqa: F811
    shared = drafts.create_from_zip(analyst, pge, "PG&E's", edit_zip(tmp_path))
    own = drafts.create_blank(analyst, None, "Mine")
    outsider = make_user("someone@example.com")
    assert set(drafts.visible(analyst)) == {shared, own}
    assert list(drafts.visible(outsider)) == []
    with pytest.raises(AccessDenied):
        drafts.set_row(outsider, own, "Measure", 0, {"NumUnits": "2"})
    with pytest.raises(AccessDenied, match="calculations.submit"):
        drafts.create_blank(outsider, pge, "Not theirs")


def test_the_job_zip_is_cet_files_that_round_trip(analyst, pge, tmp_path):  # noqa: F811
    d = drafts.create_blank(analyst, None, "New job")
    drafts.set_row(analyst, d, "Measure", 0, {"AC_Climate_Zone": "13"})
    data = drafts.job_zip_bytes(d)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ["Measure.csv", "ProgramCost.csv"]
        header = z.read("Measure.csv").decode().splitlines()[0].split("|")
    assert "E3ClimateZone" in header and "AC_Climate_Zone" not in header  # CET's own names: ready for CET or a run
    path = tmp_path / "again.zip"
    path.write_bytes(data)
    again = drafts.create_from_zip(analyst, None, "Again", path)
    assert drafts.frame(again, "Measure").at[0, "AC_Climate_Zone"] == "13"
    assert drafts.frame(again, "Measure").iloc[0].to_dict() == drafts.frame(d, "Measure").iloc[0].to_dict()


def test_edit_inputs_and_rerun_from_a_run(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    service.create_run(analyst, pge, request(release), edit_zip(tmp_path))
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    page = web.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "Edit inputs and rerun" in page
    r = web.post(f"/calculations/runs/{run.pk}/edit-inputs")
    d = InputDraft.objects.get(source_run=run)
    assert r.status_code == 302 and r["Location"] == f"/calculations/drafts/{d.pk}/"
    assert d.organisation == pge and d.name == "2028 claims (edited)" and d.measure_count == 3


def test_the_edit_inputs_pages(analyst, web):  # noqa: F811
    assert "No drafts yet" in web.get("/calculations/drafts/").content.decode()
    r = web.post("/calculations/drafts/", {"name": "Blank", "run_for": "", "start": "upload"})
    assert r.status_code == 200 and "Choose the job file" in r.content.decode()
    r = web.post("/calculations/drafts/", {"name": "Blank", "run_for": "", "start": "blank"})
    d = InputDraft.objects.get(name="Blank")
    assert r["Location"] == f"/calculations/drafts/{d.pk}/"

    page = web.get(f"/calculations/drafts/{d.pk}/").content.decode()
    assert "Input check" in page and "EXAMPLE_1" in page and 'class="no-effect"' in page

    row = web.get(f"/calculations/drafts/{d.pk}/Measure/1/").content.decode()
    assert '<select id="f-sector" name="Sector"' in row and "Changes results" in row
    r = web.post(f"/calculations/drafts/{d.pk}/Measure/1/", {"action": "save", "NumUnits": "abc"})
    d.refresh_from_db()
    assert r.status_code == 302 and drafts.frame(d, "Measure").at[0, "NumUnits"] == "abc"
    row = web.get(f"/calculations/drafts/{d.pk}/Measure/1/").content.decode()
    assert 'aria-invalid="true"' in row and "Stops the run: NumUnits must be a number" in row
    page = web.get(f"/calculations/drafts/{d.pk}/").content.decode()
    assert "would stop the run" in page and 'class="cell-error"' in page and "(Stops the run: NumUnits must be a number)" in page

    r = web.post(f"/calculations/drafts/{d.pk}/Measure/1/", {"action": "copy"})
    assert r["Location"] == f"/calculations/drafts/{d.pk}/Measure/2/"
    r = web.post(f"/calculations/drafts/{d.pk}/Measure/2/", {"action": "delete"})
    d.refresh_from_db()
    assert r.status_code == 302 and d.measure_count == 1
    assert web.get(f"/calculations/drafts/{d.pk}/Measure/9/").status_code == 404
    assert web.get(f"/calculations/drafts/{d.pk}/Nope/1/").status_code == 404

    r = web.get(f"/calculations/drafts/{d.pk}/job.zip")
    assert r.status_code == 200 and r["Content-Disposition"] == 'attachment; filename="blank.zip"'
    r = web.post(f"/calculations/drafts/{d.pk}/", {"action": "delete"})
    assert r["Location"] == "/calculations/drafts/" and not InputDraft.objects.filter(pk=d.pk).exists()


def test_idle_drafts_expire_with_scratch_runs(analyst):  # noqa: F811
    old = drafts.create_blank(analyst, None, "Old")
    drafts.create_blank(analyst, None, "New")
    InputDraft.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=40))
    found = list(purgers()["scratch_run"](timezone.now() - timedelta(days=30)))
    assert [ref for ref, _ in found] == [f"calculation_draft:{old.pk}"]
    found[0][1]()
    assert not InputDraft.objects.filter(pk=old.pk).exists()
    assert AuditEvent.objects.filter(action="calculation_draft.expired").exists()


def test_when_the_engines_checks_fail_the_pages_say_so(analyst, web, monkeypatch):  # noqa: F811
    d = drafts.create_blank(analyst, None, "Odd")

    def boom(*a, **k):
        raise TypeError("'>' not supported between instances of 'NoneType' and 'int'")

    monkeypatch.setattr(ed, "check_tables", boom)
    page = web.get(f"/calculations/drafts/{d.pk}/").content.decode()
    assert "cell checks could not run" in page and "Nothing here stops the run" not in page
    assert "cell checks could not run" in web.get(f"/calculations/drafts/{d.pk}/Measure/1/").content.decode()


def test_without_the_engine_the_page_says_so(web, monkeypatch):
    monkeypatch.setattr(drafts, "engine", lambda: None)
    assert "needs the ReCET engine package" in web.get("/calculations/drafts/").content.decode()
