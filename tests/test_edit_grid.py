"""Edit inputs as a spreadsheet (calculations/edit_grid.js): the grid's JSON (load, save both tables whole, a save on an older version
refused), its limits and audit, the end-use list, the page's markup, and the vendored Tabulator held to its SHA-256s."""

import hashlib
import json
from pathlib import Path

import pytest
from django.test import Client

from cedars.modules.calculations import drafts
from cedars.services.audit.models import AuditEvent

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures
from .test_edit_inputs import edit_zip, web  # noqa: F401 - fixtures

pytest.importorskip("recet.editor", reason="Edit inputs uses the ReCET engine's editor code")
pytestmark = pytest.mark.django_db

VENDOR = Path(__file__).resolve().parents[1] / "cedars/modules/calculations/static/calculations/vendor/tabulator-6.3.1"
SHA256 = {
    "tabulator.min.js": "e952272c3b2afa4ebb60cef5db8cbe9cbaabaa52b50c3cd3d22993ca5215a6ff",
    "tabulator_simple.min.css": "8a7431ff86fa6aa02943b70470df77fa7094e7eac9f2420b6780c6970c7345eb",
    "LICENSE": "6031e51b8450fa6047887f018951c8cf1456d98a5a08291774410c13696de18e",
}


def test_the_vendored_tabulator_is_the_published_package():
    for name, sha in SHA256.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == sha, name
        assert sha in (VENDOR / "README.md").read_text(), f"README.md does not list {name}'s SHA-256"


def _put(web, d, body):  # noqa: F811
    return web.put(f"/calculations/drafts/{d.pk}/grid.json", json.dumps(body), content_type="application/json")


def test_the_grid_loads_and_saves_both_tables_whole(analyst, pge, tmp_path, web):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    r = web.get(f"/calculations/drafts/{d.pk}/grid.json")
    assert r.status_code == 200 and r["Cache-Control"].startswith("max-age=0")
    g = r.json()
    assert g["can_change"] and g["rev"] == drafts.revision(d)
    m, pc = g["tables"]["Measure"], g["tables"]["ProgramCost"]
    assert len(m["rows"]) == 3 and "CEInputID" in m["columns"]
    assert set(g["layout"]) == {"Measure", "ProgramCost"} and "NumUnits" in g["layout"]["Measure"]["numeric"]
    assert g["layout"]["Measure"]["help"]["NumUnits"]
    assert "IOU_AC_Territory" in g["choices"]["Measure"] and "AC_Elec_Impact_Profile" not in g["choices"]["Measure"]

    # an edit, a copied row and a pasted value in one save, as the grid sends it
    cols = m["columns"]
    rows = [list(r) for r in m["rows"]]
    rows[0][cols.index("NumUnits")] = "abc"
    copy = list(rows[1])
    copy[cols.index("CEInputID")] = "M1_1"
    rows.insert(2, copy)
    r = _put(web, d, {"rev": g["rev"], "tables": {"Measure": {"columns": cols, "rows": rows}, "ProgramCost": pc}})
    assert r.status_code == 200, r.content
    saved = r.json()
    d.refresh_from_db()
    assert saved["rev"] == drafts.revision(d) != g["rev"]
    frame = drafts.frame(d, "Measure")
    assert len(frame) == 4 and frame.at[0, "NumUnits"] == "abc" and frame.at[2, "CEInputID"] == "M1_1"
    assert any(i["row"] == 0 and i["column"] == "NumUnits" and i["severity"] == "error" for i in saved["issues"] if i["table"] == "Measure")
    e = AuditEvent.objects.get(action="calculation_draft.tables_saved")
    # NumUnits, and row 3 is now row 2's copy (two cells differ from the old row 3)
    assert e.after["Measure"] == {"rows_before": 3, "rows_after": 4, "cells_changed": 3}
    assert e.before == {"Measure": 3, "ProgramCost": len(pc["rows"])}

    # the same tables again change nothing and write no event
    r = _put(web, d, {"rev": saved["rev"], "tables": {"Measure": {"columns": cols, "rows": rows}, "ProgramCost": pc}})
    assert r.status_code == 200 and r.json()["rev"] == saved["rev"]
    assert AuditEvent.objects.filter(action="calculation_draft.tables_saved").count() == 1

    # a save made on the version the page first loaded is refused, not written over the newer one
    r = _put(web, d, {"rev": g["rev"], "tables": g["tables"]})
    assert r.status_code == 409 and r.json()["rev"] == saved["rev"] and "another window" in r.json()["error"]
    assert len(drafts.frame(d, "Measure")) == 4


def test_the_grid_refuses_what_it_cannot_save(analyst, pge, make_user, tmp_path, web):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    g = web.get(f"/calculations/drafts/{d.pk}/grid.json").json()
    cols, rows, pc = g["tables"]["Measure"]["columns"], g["tables"]["Measure"]["rows"], g["tables"]["ProgramCost"]

    def tables(measure_rows=rows, measure_cols=cols):
        return {"Measure": {"columns": measure_cols, "rows": measure_rows}, "ProgramCost": pc}

    bad = [list(r) for r in rows]
    bad[0][cols.index("MeasDescription")] = "a|b"
    for body, words in [
        ({"rev": g["rev"], "tables": tables(bad)}, "'|'-delimited"),
        ({"rev": g["rev"], "tables": tables([rows[0][:-1]])}, "one value per column"),
        ({"rev": g["rev"], "tables": tables([rows[0]] * 501)}, "up to 500"),
        ({"rev": g["rev"], "tables": tables(measure_cols=[*cols[:-1], cols[0]])}, "column names"),
        ({"rev": g["rev"], "tables": {"Measure": "x"}}, "columns and rows"),
    ]:
        r = _put(web, d, body)
        assert r.status_code == 400 and words in r.json()["error"], (words, r.content)
    assert web.put(f"/calculations/drafts/{d.pk}/grid.json", "not json", content_type="application/json").status_code == 400
    assert web.put(f"/calculations/drafts/{d.pk}/grid.json", "[1, 2]", content_type="application/json").status_code == 400
    assert drafts.revision(d) == g["rev"]

    # someone outside the organization neither reads nor saves it
    outsider = sign_in(Client(), make_user("someone@example.org"))
    assert outsider.get(f"/calculations/drafts/{d.pk}/grid.json").status_code == 404
    body = json.dumps({"rev": g["rev"], "tables": tables()})
    r = outsider.put(f"/calculations/drafts/{d.pk}/grid.json", body, content_type="application/json")
    assert r.status_code == 404 and drafts.revision(d) == g["rev"]


def test_the_end_use_list_and_the_page(analyst, pge, tmp_path, web):  # noqa: F811
    r = web.get("/calculations/drafts/end-uses.json?iou=PGE&sector=Res")
    assert r.status_code == 200 and r.json()["values"][0] == ""
    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    page = web.get(f"/calculations/drafts/{d.pk}/").content.decode()
    assert 'id="editor"' in page and f'data-api="/calculations/drafts/{d.pk}/grid.json"' in page
    assert "tabulator-6.3.1/tabulator.min.js" in page and "calculations/edit_grid.js" in page
    assert 'id="run-link"' in page and "data-without-grid" in page and 'name="csrfmiddlewaretoken"' in page
    for needed in (
        "tab-Measure",
        "tab-ProgramCost",
        "grid-Measure",
        "grid-ProgramCost",
        "save-status",
        "help",
        "grid-summary",
        "grid-issues-box",
        "grid-issues",
        "grid-issues-count",
        "col-find",
        "col-names",
        "hide-noeff",
        "grid-confirm",
        "grid-confirm-text",
        "grid-confirm-yes",
        "grid-confirm-no",
    ):  # every element edit_grid.js looks up
        assert f'id="{needed}"' in page, needed
    # the row-by-row view is the page without the grid, and its links keep it
    rows = web.get(f"/calculations/drafts/{d.pk}/?view=rows").content.decode()
    assert 'id="editor"' not in rows and "edit_grid.js" not in rows and "view=rows" in rows


def test_the_grid_marks_the_engine_checks_cells_while_the_check_applies(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    from cedars.core import storage
    from cedars.modules.calculations import dispatch

    d = drafts.create_from_zip(analyst, pge, "Lighting", edit_zip(tmp_path))
    web.post(f"/calculations/drafts/{d.pk}/", {"action": "check", "acc": "2024", "filing_validation": ""})
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    report = {
        "ok": False, "errors": 1, "stats": {},
        "issues": [{"severity": "error", "code": "duplicate_id", "message": "CEInputID must be unique", "count": 1, "examples": [],
                    "cells": [["Measure", 1, "CEInputID"]], "impact": {}}],
    }  # fmt: skip
    storage.store("results").put_bytes(run.storage_prefix + "out/preflight.json", json.dumps(report).encode())
    marked = [i for i in web.get(f"/calculations/drafts/{d.pk}/grid.json").json()["issues"] if i["column"] == "CEInputID"]
    assert any(i["row"] == 1 and i["severity"] == "error" and "must be unique" in i["message"] for i in marked)

    drafts.set_row(analyst, d, "Measure", 0, {"MeasDescription": "changed"})
    marked = [i for i in web.get(f"/calculations/drafts/{d.pk}/grid.json").json()["issues"] if i["column"] == "CEInputID"]
    assert not any("must be unique" in i["message"] for i in marked)  # the tables changed: the check no longer applies
