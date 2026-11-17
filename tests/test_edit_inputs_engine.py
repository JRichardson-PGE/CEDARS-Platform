"""Edit inputs as ReCET's own (UI parity PR 4): end uses offered and checked from the impact-profile library in force, "Check with the
engine" (a check-only run of the draft whose findings are marked on the cells), and the draft as a job workbook."""

# ruff: noqa: F811 - pytest fixtures imported by name
import io
import json
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from cedars.core import storage
from cedars.modules.calculations import dispatch, drafts, jobfiles
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import service as refdata

from .test_calculations import analyst, release, worker  # noqa: F401 - fixtures
from .test_edit_inputs import edit_zip, web  # noqa: F401 - fixtures

ed = pytest.importorskip("recet.editor", reason="Edit inputs uses the ReCET engine's editor code")
pytestmark = pytest.mark.django_db

CATALOG = [
    # end_use_name, iou_ac_territory, target_sector, shape_id (None: no hourly data)
    ("Indoor Lighting", "PGE", "Commercial", 1),
    ("Indoor Lighting", "PGE", "Residential", 2),
    ("HVAC Cooling", "PGE", "Commercial", 3),
    ("HVAC Cooling 2", "PGE", "Commercial", 3),
    ("Calculated Only", "PGE", "Commercial", None),
    ("Pool Pump", "SCE", "Residential", 4),
]


def library(cpuc_admin, tmp_path, rows=CATALOG):
    """A published impact-profile library pack whose catalog.parquet holds `rows` (the engine's catalog format)."""
    table = pa.Table.from_pylist(
        [
            {"id": i, "end_use_name": n, "iou_ac_territory": iou, "target_sector": sec, "shape_id": shape, "profile_version": 1}
            for i, (n, iou, sec, shape) in enumerate(rows, start=1)
        ]
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    folder = tmp_path / "library"
    folder.mkdir(exist_ok=True)
    (folder / "catalog.parquet").write_bytes(buf.getvalue())
    uri = f"refdata/test-library-{len(rows)}"
    default_storage.save(f"{uri}/catalog.parquet", ContentFile(buf.getvalue()))
    pack = refdata.register_pack(
        cpuc_admin,
        kind="impact_profiles",
        label="Impact profiles",
        version="export 1",
        valid_from=date(2020, 1, 1),
        manifest=refdata.manifest_for(folder),
        storage_uri=uri,
    )
    refdata.record_tie_out(cpuc_admin, pack, check="profile_checks", passed=True)
    return refdata.publish_pack(cpuc_admin, pack)


def lighting(analyst, pge, tmp_path):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    for i, use in enumerate(("Indoor Lighting", "HVAC Cooling", "Nope")):
        drafts.set_row(
            analyst,
            d,
            "Measure",
            i,
            {"IOU_AC_Territory": "PGE", "AC_Elec_Target_Sector": "Residential" if i == 1 else "Commercial", "AC_Elec_Impact_Profile": use,
             "UnitkWh1stBaseline": "100"},
        )  # fmt: skip
    return d


def profile_issues(d):
    return {(i.row, i.severity) for i in drafts.issues(d) if i.column == "AC_Elec_Impact_Profile"}


def test_end_uses_are_offered_and_checked_from_the_library_in_force(analyst, pge, cpuc_admin, tmp_path, web):  # noqa: F811
    d = lighting(analyst, pge, tmp_path)
    assert profile_issues(d) == set()  # no library: end uses are not checked
    assert drafts.choices().library is None
    page = web.get(f"/calculations/drafts/{d.pk}/Measure/1/").content.decode()
    assert "No impact-profile library is in force" in page and "<datalist" not in page

    library(cpuc_admin, tmp_path)
    src = drafts.choices()
    assert src.library is not None
    assert src.end_use_choices("PGE", "Commercial") == ["HVAC Cooling", "HVAC Cooling 2", "Indoor Lighting"]  # no "Calculated Only"
    assert src.end_use_choices("PGE", "Industrial") == ["HVAC Cooling", "HVAC Cooling 2", "Indoor Lighting"]  # the IOU's, any sector
    assert src.end_use_choices("SCE", "Residential") == ["Pool Pump"]
    # row 1 exact, row 2 on end use and IOU only (a note), row 3 unknown (its electric savings would not be priced)
    assert profile_issues(d) == {(1, "info"), (2, "warning")}
    found = {i.row: i.message for i in drafts.issues(d) if i.column == "AC_Elec_Impact_Profile"}
    assert "will NOT be priced" in found[2] and "end use and IOU only" in found[1]

    page = web.get(f"/calculations/drafts/{d.pk}/Measure/1/").content.decode()
    assert 'list="f-ac_elec_impact_profile-list"' in page and '<option value="HVAC Cooling 2">' in page
    assert "Pool Pump" not in page  # another IOU's
    assert "with end uses checked against" in web.get(f"/calculations/drafts/{d.pk}/").content.decode()


def test_check_with_the_engine_marks_the_cells_its_check_names(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    page = web.get(f"/calculations/drafts/{d.pk}/").content.decode()
    assert "Check with the engine" in page and 'name="acc"' in page and '<option value="2026" selected>' in page  # 2028's claims ACC

    r = web.post(f"/calculations/drafts/{d.pk}/", {"action": "check", "acc": "2024", "filing_validation": "claims"})
    assert r.status_code == 302 and r["Location"].endswith("#engine-check-h")
    d.refresh_from_db()
    run = d.check_run
    assert run.settings["preflight_only"] is True and run.settings["filing_validation"] == "claims" and run.acc_version == "2024"
    assert run.name == "Input check: Lighting" and run.organisation == pge and d.check_hash == drafts.content_hash(d)
    assert "The check is queued" in web.get(f"/calculations/drafts/{d.pk}/").content.decode()

    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    report = {
        "ok": False,
        "errors": 1,
        "stats": {"measures": "3"},
        "issues": [
            {"severity": "error", "code": "duplicate_id", "message": "CEInputID must be unique", "count": 1, "examples": ["M1"],
             "cells": [["Measure", 1, "CEInputID"]], "impact": {}},
            {"severity": "warning", "code": "no_electric_profile", "message": "no 8,760 profile", "count": 1, "examples": [],
             "cells": [["Measure", 2, "E3MeaElecEndUseShape"]], "impact": {}},  # a CET name: shown under ReCET's
            {"severity": "info", "code": "acc_check", "message": "ACC check not made", "count": 0, "examples": [], "cells": [],
             "impact": {}},
        ],
    }  # fmt: skip
    storage.store("results").put_bytes(run.storage_prefix + "out/preflight.json", json.dumps(report).encode())
    check = drafts.check_findings(d)
    assert check.ended and check.current and check.stops == 1
    assert {(i.row, i.column, i.severity) for i in check.cells} == {
        (1, "CEInputID", "error"),
        (2, "AC_Elec_Impact_Profile", "warning"),
    }
    assert [i["code"] for i in check.other] == ["acc_check"]
    page = web.get(f"/calculations/drafts/{d.pk}/").content.decode()
    assert "found 1 problem that stops the run" in page and "2 cells marked from it" in page
    assert "CEInputID must be unique (the engine&#x27;s check: duplicate_id)" in page and "ACC check not made" in page

    drafts.set_row(analyst, d, "Measure", 0, {"MeasDescription": "changed"})
    d.refresh_from_db()
    check = drafts.check_findings(d)
    assert not check.current and check.cells == [] and len(check.other) == 3  # listed, not marked on cells
    assert "The tables changed since the last check" in web.get(f"/calculations/drafts/{d.pk}/").content.decode()


def test_a_check_is_refused_on_an_acc_the_release_lacks_or_by_someone_who_cannot_change_the_draft(
    analyst, pge, release, tmp_path, web, make_user
):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    web.post(f"/calculations/drafts/{d.pk}/", {"action": "check", "acc": "2030", "filing_validation": ""})
    d.refresh_from_db()
    assert d.check_run is None and not Run.objects.exists()
    stranger = make_user("x@pge.example.org")
    with pytest.raises(AccessDenied):
        drafts.start_check(stranger, d, None)


def test_the_draft_downloads_as_an_accessible_job_workbook_that_reads_back(analyst, pge, tmp_path, web):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    drafts.set_row(analyst, d, "Measure", 0, {"NumUnits": "2.5", "IOU_AC_Territory": "PGE"})
    r = web.get(f"/calculations/drafts/{d.pk}/job.xlsx")
    assert r.status_code == 200 and r["Content-Disposition"] == 'attachment; filename="lighting.xlsx"'
    path = tmp_path / "back.xlsx"
    path.write_bytes(r.content)

    from openpyxl import load_workbook

    wb = load_workbook(path)
    assert wb.sheetnames == ["Measure", "ProgramCost"] and wb.properties.language == "en-US"
    assert wb["Measure"].tables and wb["Measure"]["A1"].value == "CEInputID"
    header = [c.value for c in wb["Measure"][1]]
    assert wb["Measure"].cell(2, header.index("NumUnits") + 1).value == 2.5  # a number, not text

    assert jobfiles.inspect_workbook(path).records == 3
    back = drafts.create_from_zip(analyst, pge, "Back", path)
    m, pc = drafts.frames(back)
    assert m.at[0, "IOU_AC_Territory"] == "PGE" and m.at[0, "NumUnits"] == "2.5" and len(pc) == 1
