"""A fully valid claim and budget filing: the example records CEDARS writes for a new draft pass every rule of CEDARS' published rule
sets with no finding of any severity (before and after the draft's rules follow its fields, and with CEDARS' 2026 claims data rules
added), and the same records, as the platform hands them to the engine to price, pass the engine's input check."""

import csv
import io
import zipfile

import pytest

from cedars.services.rules import service as rules
from cedars.services.specifications import editor, examples
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="CEDARS' rule sets and the input check come from the pinned ReCET engine")

# what the engine's input check says because the test has no reference data folder or program list, never about the records
NO_REFERENCE_DATA = {"no_profile_library", "no_electric_profile", "program_not_in_metadata", "not_pipe_delimited"}


def _draft(cpuc_admin, code):
    published = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, code))
    v = editor.start_draft(cpuc_admin, specifications.current(Specification.objects.get(code=code)), "Valid filing", "why")
    return published, v


def _findings(v) -> list[dict]:
    examples.check_all(v)
    return v.examples.get(slug=examples.GENERATED).report["failing"]


@pytest.mark.parametrize("code", ["budget_filing", "claims"])
def test_a_valid_filing_gets_no_finding_from_any_rule(cpuc_admin, code):
    published, v = _draft(cpuc_admin, code)
    assert published.rules.count() > 100 and v.rule_set.rules.count() == published.rules.count()
    assert _findings(v) == []  # every rule CEDARS publishes, as the draft starts
    editor.align_rules(cpuc_admin, v, "The fields decide")
    if code == "claims":
        editor.add_rule_pack(cpuc_admin, v, "claims-cedars-2026", "CEDARS' 2026 claims data rules")
    v.refresh_from_db()
    examples.generate(cpuc_admin, v, "Written again for the rules")
    assert _findings(v) == []  # and as the draft would be published
    rows = v.examples.get(slug=examples.GENERATED).rows
    # a directly installed measure, so the rules for everything but codes and standards apply
    assert rows["Measure"][0]["DeliveryType"] == "DnDeemDI" and rows["Measure"][0]["NTG_ID"]


@pytest.mark.parametrize("code", ["budget_filing", "claims"])
def test_a_valid_filing_passes_the_engines_input_check(cpuc_admin, code, tmp_path):
    from recet import preflight

    _published, v = _draft(cpuc_admin, code)
    rows = examples.generate(cpuc_admin, v, "Written again").rows
    assert _findings(v) == []
    if code == "claims":  # what a claims filing is priced on: its compiled Measure (ClaimID as CEInputID) and ProgramCost
        from cedars.modules.claims import service as claims
        from cedars.modules.claims import spec, upload

        lay = spec.Layout(specifications.layout(v))
        own = {t: upload.Table(t, lay.columns(t), rs) for t, rs in rows.items() if t in lay.TABLES and rs and t != "Withdrawal"}
        zipfile.ZipFile(io.BytesIO(claims.job_zip(own, lay=lay))).extractall(tmp_path)
    else:  # a budget filing is priced on its upload as it is
        for t in ("Measure", "ProgramCost"):
            with open(tmp_path / f"{t}.csv", "w", newline="") as fh:
                w = csv.DictWriter(fh, list(rows[t][0]))
                w.writeheader()
                w.writerows(rows[t])
    rep = preflight.check_inputs(tmp_path / "Measure.csv", tmp_path / "ProgramCost.csv", base_year=examples.example_year())
    assert rep.ok, rep.text()
    assert {i.code for i in rep.issues if i.severity != "info"} <= NO_REFERENCE_DATA, rep.text()
    assert rep.stats["measures"] == "1"
