"""The run page's input check, log and downloads (ReCET's own run page has them: the Input check card from preflight.json, the
Log card, Download everything and inputs.zip)."""

import io
import json
import zipfile

import pytest

from cedars.core import storage, zipstream
from cedars.modules.calculations import dispatch, service
from cedars.modules.calculations.models import Run

from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

PREFLIGHT = {
    "ok": False,
    "errors": 1,
    "stats": {"Measures": "3", "ACC check": "Claims: 2 of 3 measures ..."},
    "acc_check": {
        "purpose": "claims",
        "label": "Claims",
        "measures": 3,
        "flagged": 2,
        "not_checked": 0,
        "by_acc_and_year": [
            {"acc": "2024", "program_year": 2028, "measures": 1, "status": "ok"},
            {"acc": "2024", "program_year": 2029, "measures": 2, "status": "flagged"},
        ],
    },
    "issues": [
        {
            "severity": "warning",
            "code": "no_gas_profile",
            "message": "No gas profile",
            "count": 4,
            "examples": ["M9"],
            "cells": [],
            "impact": {"first-year net therms": -26024.0},
        },
        {"severity": "info", "code": "note_x", "message": "A note", "count": 0, "examples": [], "cells": [], "impact": {}},
        {"severity": "error", "code": "blank_required_value", "message": "NTGRkWh is blank", "count": 1, "examples": ["M1", "M2"]},
    ],
}


def put(run, name, data: bytes):
    storage.store("results").put_bytes(run.storage_prefix + "out/" + name, data)


def ended(run, status=Run.Status.FAILED):
    Run.objects.filter(pk=run.pk).update(status=status)
    run.refresh_from_db()
    return run


def test_the_input_check_is_on_the_run_page(tmp_path, signed_in, analyst, pge, release):  # noqa: F811
    run = ended(service.create_run(analyst, pge, request(release), job_zip(tmp_path)))
    put(run, "preflight.json", json.dumps(PREFLIGHT).encode())
    put(run, "preflight_findings.xlsx", b"x")
    html = signed_in(analyst).get(f"/calculations/runs/{run.pk}/").content.decode()
    assert 'id="check-h">Input check' in html and "1 problem stopped the run." in html
    # unpriced measures first, with the energy they carry; the ACC check in words
    assert "4 measures (-26,024 first-year net therms): No gas profile" in html
    assert "2 of 3 measures are priced on an ACC" in html and "2024 ACC for 2029: 2" in html
    # every finding, errors first, severity in words (not color alone)
    order = [html.index(t) for t in ("Stops the run:</strong> NTGRkWh", "Warning:</strong> No gas profile", "Note:</strong> A note")]
    assert order == sorted(order)
    assert "For example: M1, M2" in html and 'id="check-details" open' in html
    assert "preflight_findings.xlsx</a>" in html


def test_no_check_card_before_the_check_has_run(tmp_path, signed_in, analyst, pge, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    put(run, "preflight.json", json.dumps(PREFLIGHT).encode())  # queued: not read
    html = signed_in(analyst).get(f"/calculations/runs/{run.pk}/").content.decode()
    assert 'id="check-h"' not in html and 'id="log-h"' not in html


def test_the_log_shows_while_running_and_downloads(tmp_path, signed_in, analyst, pge, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    Run.objects.filter(pk=run.pk).update(status=Run.Status.RUNNING)
    c = signed_in(analyst)
    html = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert 'id="log-h">Log' in html and "Nothing yet." in html and 'data-live="1"' in html
    put(run, "log.txt", b"10:00:01 recet worker 0.1.90: job x\n10:00:02 pricing <part 1>\n")
    html = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "pricing &lt;part 1&gt;" in html  # escaped
    r = c.get(f"/calculations/runs/{run.pk}/log.txt")
    assert r.status_code == 200 and r.content.endswith(b"<part 1>\n") and r["Content-Type"].startswith("text/plain")
    assert "attachment" in c.get(f"/calculations/runs/{run.pk}/log.txt?download=1")["Content-Disposition"]


def test_a_long_log_shows_its_end(tmp_path, signed_in, analyst, pge, release):  # noqa: F811
    run = ended(service.create_run(analyst, pge, request(release), job_zip(tmp_path)))
    put(run, "log.txt", b"".join(f"line {i}\n".encode() for i in range(40_000)))
    html = signed_in(analyst).get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "line 39999" in html and "line 0\n" not in html and "the whole log is a download" in html
    assert 'id="log-details" open' in html  # a failed run's log is open


def test_every_file_and_every_input_download_as_one_zip(tmp_path, worker, signed_in, analyst, pge, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    run.refresh_from_db()
    c = signed_in(analyst)
    html = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "Download every file (.zip)" in html
    r = c.get(f"/calculations/runs/{run.pk}/files.zip")
    assert r.status_code == 200 and r["Content-Type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(b"".join(r.streaming_content)))
    names = {f["name"] for f in service.output_files(run)}
    assert set(z.namelist()) == names and z.read("measure_level_run.csv") == b"CEInputID,TRCRatio\nM1,1.5\n"
    assert not any(n.startswith("_results/") or n == "progress.json" for n in z.namelist())
    # the inputs, each as given
    name = next(iter(run.inputs))
    r = c.get(f"/calculations/runs/{run.pk}/inputs/{name}")
    assert r.status_code == 200 and b"".join(r.streaming_content) == storage.store("uploads").get_bytes(run.inputs[name]["key"])
    assert c.get(f"/calculations/runs/{run.pk}/inputs/nothing").status_code == 404
    z = zipfile.ZipFile(io.BytesIO(b"".join(c.get(f"/calculations/runs/{run.pk}/inputs.zip").streaming_content)))
    assert len(z.namelist()) == len(run.inputs)


def test_a_running_run_has_no_zip_yet_and_strangers_get_nothing(tmp_path, signed_in, make_user, analyst, pge, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert signed_in(analyst).get(f"/calculations/runs/{run.pk}/files.zip").status_code == 404
    stranger = signed_in(make_user("stranger@example.org"))
    for path in ("log.txt", "files.zip", "inputs.zip", f"inputs/{next(iter(run.inputs))}"):
        assert stranger.get(f"/calculations/runs/{run.pk}/{path}").status_code == 404, path


def test_the_zip_stream_round_trips_large_and_stored_entries():
    big = bytes(range(256)) * 20_000  # 5 MB, several chunks
    entries = [("a.csv", lambda: io.BytesIO(big)), ("b.parquet", lambda: io.BytesIO(b"PAR1")), ("empty.txt", lambda: io.BytesIO(b""))]
    pieces = list(zipstream.stream(entries))
    assert len(pieces) > 3  # sent as it is read
    z = zipfile.ZipFile(io.BytesIO(b"".join(pieces)))
    assert z.read("a.csv") == big and z.read("b.parquet") == b"PAR1" and z.read("empty.txt") == b""
    assert z.getinfo("b.parquet").compress_type == zipfile.ZIP_STORED and z.getinfo("a.csv").compress_type == zipfile.ZIP_DEFLATED
