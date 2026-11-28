"""Each of CEDARS' 165 claims data rules of 2026 (https://cedars.cpuc.ca.gov/claims/specification/, kept with what checks it in
tests/data/cedars_2026_claims_data_rules.csv; lines counted from the first rule) names the platform's checks that make it, or says
why none does. The checks named exist (a built-in check, a rule of CEDARS' claims rules in force, or a rule of the 2026 claims rule
pack), and the rules not checked are listed here, so a gap closed or opened changes this test."""

import csv
import re
from pathlib import Path

import pytest

from cedars.services.rules import coded, packs
from cedars.services.rules import service as rules

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="CEDARS' claims rules come from the pinned ReCET engine")

TRACE = Path(__file__).parent / "data" / "cedars_2026_claims_data_rules.csv"
PACK = "claims-cedars-2026"
NOT_CHECKED = {
    "ProgramCost has no ClaimYearQuarter": {2, 3},
    "claim totals within 0.1%": set(range(80, 95)),
    "dates within the codes' effective dates": {95, 100, 101, 107, 108, 109},
    "the CMPA tool's lists": {99, 137},
    "restricted permutations": {103},
    "CEDARS' 2026 value lists, or no list": {115, 120, 121, 122, 125, 126, 127, 128, 129, 164},
}


def _trace() -> list[dict]:
    with TRACE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_every_rule_is_traced_to_a_check_that_exists_or_says_why_not(cpuc_admin):
    rows = _trace()
    assert [int(r["line"]) for r in rows] == list(range(1, 166))
    pack = packs.pack(PACK)
    known = (
        set(coded.by_id())
        | set(rules.import_from_engine(cpuc_admin, "claims").rules.values_list("rule_id", flat=True))
        | {packs.rule_id_of(d) for d in pack.rules}
    )
    for r in rows:
        ids = r["checked_by"].split()
        assert not set(ids) - known, f"line {r['line']} names {set(ids) - known}, which no check has"
        assert not set(ids) & set(pack.replaces), f"line {r['line']} names a rule the 2026 pack replaces"
        if not ids:
            assert r["note"].startswith(("Not done", "Not applicable")), f"line {r['line']} says neither what checks it nor why not"
        else:
            assert not r["note"].startswith(("Not done", "Not applicable")), f"line {r['line']} names checks and says it is not done"
    unchecked = {int(r["line"]) for r in rows if not r["checked_by"]}
    assert unchecked == set().union(*NOT_CHECKED.values())


def test_each_pack_rule_is_on_the_line_it_cites():
    rows = {int(r["line"]): r for r in _trace()}
    for d in packs.pack(PACK).rules:
        m = re.search(r"line (\d+)$", d["source"])
        if m:  # the pack's rules from CEDARS' data rules (two come from the budget filing rules)
            row = rows[int(m.group(1))]
            assert packs.rule_id_of(d) in row["checked_by"].split(), (d["source"], row["rule"])
