import csv
import io

import pytest
from django.core.management import CommandError, call_command

from cedars.modules.claims import legacy as legacy_claims
from cedars.modules.data import parity

from .test_legacy_claims import _year

pytestmark = pytest.mark.django_db
HEADER = "dataset,year,pa,filing,metric,value,scope,source\n"


def _expected(tmp_path, rows, name="expected.csv"):
    path = tmp_path / name
    path.write_text(HEADER + "".join(",".join(map(str, r)) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def loaded(tmp_path):
    """The example 2023 claim year (PG&E two claims, SCE one, statewide unallocated one), loaded with its snapshots."""
    _year(tmp_path)
    assert legacy_claims.load(2023).snapshots == 2


def test_published_totals_that_agree_pass_and_the_report_has_every_row(tmp_path, loaded, capsys):
    path = _expected(
        tmp_path,
        [
            ("claims", 2023, "PGE", "", "kwh_first_year_net", "200", "cs;cca_ren", "page.html"),
            ("claims", 2023, "PGE", "", "expenditure", "100.4", "cs;cca_ren", "page.html"),  # within 0.5%
            ("claims", 2023, "ALL", "", "kwh_first_year_net", "300", "cs", "page.html"),
            ("claims", 2023, "PGE", "", "pac", "2.50", "cs", "page.html"),
            ("claims", 2023, "PGE", "", "filing_budget", "1", "cs", "page.html"),
            ("claims", 2023, "PGE", "", "kw_first_year_net", "0.4", "cs", "page.html"),  # within half a unit (pages print whole numbers)
            ("claims", 2023, "PGE", "", "rim", "0", "cs", "page.html"),  # no RIM cost: blank here, 0 on CEDARS' page
        ],
    )
    report = tmp_path / "report.csv"
    call_command("dashboard_parity", "--expected", str(path), "--report", str(report))
    out = capsys.readouterr().out
    assert "6 agrees, 0 differs, 0 known, 0 missing, 1 not checked" in out and "claims release 1" in out
    rows = list(csv.DictReader(report.open(encoding="utf-8")))
    assert [r["outcome"] for r in rows] == ["agrees", "agrees", "agrees", "agrees", "not checked", "agrees", "agrees"]
    assert float(rows[0]["dashboard"]) == pytest.approx(200.0)  # back in kWh, not the page's MWh
    assert rows[4]["note"] == "not a dashboard metric" and rows[6]["note"] == "blank over a zero cost; CEDARS prints 0"


def test_a_difference_fails_unless_it_is_known(tmp_path, loaded, capsys):
    path = _expected(
        tmp_path,
        [
            ("claims", 2023, "PGE", "", "expenditure", "110", "cs;cca_ren;statewide", "page.html"),
            ("claims", 2023, "SDGE", "", "tsb", "5", "cs", "page.html"),
        ],
    )
    with pytest.raises(CommandError, match="1 differ and 1 are missing"):
        call_command("dashboard_parity", "--expected", str(path))
    out = capsys.readouterr().out
    assert "differs: claims 2023 PGE expenditure: published 110.0000, dashboard 100.0000 (-9.091%)" in out

    known = tmp_path / "known.csv"
    known.write_text("dataset,year,pa,metric,reason\nclaims,,PGE,expenditure,cost outside the claims\nclaims,2023,SDGE,,not loaded\n")
    call_command("dashboard_parity", "--expected", str(path), "--known", str(known), "--report", str(tmp_path / "r.csv"))
    rows = list(csv.DictReader((tmp_path / "r.csv").open(encoding="utf-8")))
    assert [r["outcome"] for r in rows] == ["known", "known"]
    assert rows[0]["note"] == "cost outside the claims; the page includes statewide unallocated claims; the dashboard never does"


def test_the_scope_decides_what_is_left_out():
    assert parity._exclusions({"cs", "cca_ren"}) == ("esa",)  # Pension and Benefits always counted: CEDARS has no switch
    assert parity._exclusions({"cs", "esa"}) == ()
    assert parity._exclusions({"all"}) == ()
    assert parity._exclusions(set()) == ("esa", "cs")


def test_a_bad_expected_file_is_refused_with_its_row(tmp_path):
    with pytest.raises(parity.ParityError, match="row 2: 'lots' is not a number"):
        parity.read_expected(io.StringIO(HEADER + "claims,2023,PGE,,tsb,lots,cs,x\n"))
    with pytest.raises(parity.ParityError, match="unknown scope sw"):
        parity.read_expected(io.StringIO(HEADER + "claims,2023,PGE,,tsb,1,cs;sw,x\n"))
    with pytest.raises(CommandError, match="no source column"):
        call_command("dashboard_parity", "--expected", str(_write(tmp_path, "dataset,year,pa,filing,metric,value,scope\n")))


def _write(tmp_path, text):
    p = tmp_path / "bad.csv"
    p.write_text(text)
    return p
