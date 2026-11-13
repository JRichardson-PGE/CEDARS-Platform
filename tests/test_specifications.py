"""Filing specifications (cedars.services.specifications): the starting versions, the database's freeze, filings pinned to a version, and
parity: until filings read their version, the template, the required columns and the findings the starting version gives must be the
ones the modules' constants give today."""

import io
import zipfile
from datetime import date, timedelta

import pytest
from django.db import IntegrityError, InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.modules.budgets import jobfile
from cedars.modules.claims import service as claims
from cedars.modules.claims import spec as claims_spec
from cedars.modules.monthly import upload as monthly_upload
from cedars.services.rules import coded
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import CheckSetting, SpecField, Specification, SpecificationVersion, SpecTable
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, SubmissionVersion
from cedars.services.vault import spec as vault_spec

from .test_claims import claim, claims_zip, deadline, filer, programs, q1, row, tables, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
CODES = ("budget_filing", "claims", "monthly")
REFUSED = (InternalError, ProgrammingError, IntegrityError)


def current(code: str) -> SpecificationVersion:
    return specifications.current(Specification.objects.get(code=code))


# --- the starting versions


def test_every_filing_module_registers_its_layout():
    assert [(x.code, x.module) for x in specifications.layouts()] == [
        ("budget_filing", "budgets"),
        ("claims", "claims"),
        ("monthly", "monthly"),
    ]


@pytest.mark.parametrize("code", CODES)
def test_the_stored_starting_document_is_what_the_code_makes_today(code):
    """The two sources agree: a module's layout and its specification's starting document. Without the engine the value lists (read from
    the rule set it ships) are left out of the comparison."""
    made, kept = specifications.starting_document(code), specifications.stored_starting(code)
    try:
        import recet  # noqa: F401
    except ImportError:
        for doc in (made, kept):
            for t in doc["tables"]:
                for f in t["fields"]:
                    f["value_list"] = ""
    assert specifications.differences_between(kept, made) == [], (
        f"The {code} layout in the code no longer matches its starting specification. Until filings read their version, run "
        "`manage.py specification_starting --write` and publish the new document as a new version in a data migration."
    )


@pytest.mark.parametrize("code", CODES)
def test_the_migration_published_each_starting_version(code):
    v = current(code)
    assert v.name == "Starting version" and v.status == SpecificationVersion.Status.PUBLISHED and v.published_at
    doc = specifications.as_document(v)
    assert specifications.differences_between(specifications.stored_starting(code), doc) == []
    assert v.fingerprint == specifications.fingerprint(doc) == specifications.fingerprint(specifications.stored_starting(code))
    assert v.specification.module == {"claims": "claims", "budget_filing": "budgets", "monthly": "monthly"}[code]


def test_the_claims_version_holds_the_claim_tables_and_the_two_vault_tables():
    v = current("claims")
    lay = specifications.layout(v)
    assert lay.names(vault=False) == list(claims_spec.TABLES)
    assert lay.names(vault=True) == ["PII_Contact", "PII_Site"]
    assert v.rule_set_code == "claims"
    for name in vault_spec.TABLES:
        assert lay.table(name).columns() == vault_spec.columns(name)
        assert [f.max_length for f in lay.table(name).fields] == [f.length for f in vault_spec.TABLES[name]]
        assert lay.table(name).keys == (vault_spec.KEYS[name],)
    assert lay.table("ContactClaim").replace == "claim" and lay.table("Claim").replace == "key"
    assert lay.table_name("fuel_sub_infrastructure_cost") == "InfrastructureCost"
    assert lay.table("Measure").canonical("ceinputid") == "ClaimID"
    assert lay.table("Measure").canonical("E3ClimateZone") == "AC_Climate_Zone"
    assert lay.table("Withdrawal").system_fields() == ["WithdrawnIn"]


@pytest.mark.parametrize("code", CODES)
def test_each_version_lists_the_built_in_checks_of_its_filings(code):
    groups = {g.module: g for g in coded.groups()}
    want = {"claims": ["claims", "vault"], "budget_filing": ["budgets"], "monthly": ["monthly"]}[code]
    expected = [c.rule_id for g in want for c in groups[g].checks]
    assert list(current(code).checks.values_list("rule_id", flat=True)) == expected


# --- parity with the constants


def test_the_required_columns_are_the_constants():
    claims_layout = specifications.layout(current("claims"))
    for t in claims_spec.TABLES:
        assert claims_layout.table(t).required_fields() == claims_spec.required(t), t
        assert claims_layout.table(t).upload_columns() == claims_spec.upload_columns(t), t
        assert claims_layout.table(t).keys == claims_spec.KEYS[t]
    budgets = specifications.layout(current("budget_filing"))
    assert budgets.names() == list(jobfile.TABLES)
    assert {t: list(budgets.table(t).refuse_without) for t in jobfile.TABLES} == jobfile.REQUIRED
    monthly = specifications.layout(current("monthly"))
    assert monthly.names() == list(monthly_upload.TABLES)
    assert {t: list(monthly.table(t).refuse_without) for t in monthly_upload.TABLES} == monthly_upload.REQUIRED


TYPE_CODES = {("text", None): "text", ("text", 6): "text6", ("text", 255): "text255", ("number", None): "number"}
TYPE_CODES |= {("integer", None): "integer", ("flag", None): "bit", ("date", None): "date"}


def claims_constants_from(version) -> dict:
    """The claims module's constants (spec.py) as the version gives them: what the module reads once filings read their version."""
    lay = specifications.layout(version)
    names = lay.names(vault=False)

    def field(t, f):
        return claims_spec.Field(
            t, f.name, f.other_names[0] if f.other_names else f.name, TYPE_CODES[(f.type, f.max_length)], f.requirement == "required",
            f.definition,
        )  # fmt: skip

    fields = {t: [field(t, f) for f in lay.table(t).fields] for t in names}
    return {
        "TABLES": tuple(names),
        "KEYS": {t: lay.table(t).keys for t in names},
        "BY_CLAIM": tuple(t for t in names if lay.table(t).replace == "claim"),
        "SYSTEM_FIELDS": {t: tuple(lay.table(t).system_fields()) for t in names if lay.table(t).system_fields()},
        "TABLE_NAMES": {
            **{t.lower(): t for t in names},
            **{o.lower(): t.name for t in lay.tables if not t.vault for o in t.other_names},
        },
        "fields": lambda: fields,
        "_lookup": lambda table: {n.lower(): f.name for f in lay.table(table).fields for n in (f.name, *f.other_names)},
    }


def test_the_claims_template_from_the_version_has_the_constants_columns():
    from openpyxl import load_workbook

    constants = claims_constants_from(current("claims"))
    book = load_workbook(io.BytesIO(claims_spec.template_xlsx()), read_only=True)
    assert book.sheetnames == ["Fields", *constants["TABLES"]]
    for t in constants["TABLES"]:
        header = [c for c in next(book[t].iter_rows(values_only=True)) if c]
        assert header == [f.name for f in constants["fields"]()[t] if f.name not in constants["SYSTEM_FIELDS"].get(t, ())]
    fields = [r[:5] for r in book["Fields"].iter_rows(min_row=2, values_only=True) if r[0]]
    assert [(r[0], r[1], r[4] == "yes") for r in fields] == [
        (t, f.name, f.required) for t in constants["TABLES"] for f in constants["fields"]()[t]
    ]


def findings_of(v) -> list[tuple]:
    return sorted(
        tuple(str(f.get(k, "")) for k in ("table", "row", "record_id", "field", "rule_id", "severity", "message", "fix", "value"))
        for f in claims.findings(v)
    )


def test_claims_findings_from_the_version_are_the_constants(monkeypatch, filer, year, pge, programs):  # noqa: F811
    """The same uploads give the same findings when the claims module reads its tables, fields, keys and names from the starting version
    instead of its constants."""
    bad = tables(
        claim("SCE-2028-001"),
        claim("PGE-2028-002", quarter="2028Q2"),
        claim("PGE-2028-006", MeasImpactType="Cust-SEM"),
        Measure=[row("Measure", ClaimID="PGE-2028-007", PrgID="PGE_Res")],
        ProgramCost=[row("ProgramCost", PrgID="PGE_Res"), row("ProgramCost", PrgID="PGE_Res", PrgYear="2027")],
        Site=[row("Site", SiteID="PGE-S1"), row("Site", SiteID="PGE-S1", SiteZipCode="9411")],
        ContactClaim=[row("ContactClaim", ClaimID="PGE-2028-001", ContactID="PGE-C1")],
        Withdrawal=[row("Withdrawal", ClaimID="PGE-2028-555")],
    )
    bad["Claim"].append(row("Claim", ClaimID="PGE-2028-008", SiteID="PGE-S1", NMEC_TU_Flag="maybe", ApplicationDate="soon"))
    bad["Claim"].append({"ClaimID": "PGE-2028-009"})  # its required fields blank
    files = {"q1": q1(), "bad": claims_zip(bad), "renamed": claims_zip(bad, names={"AC_Climate_Zone": "E3ClimateZone"})}
    files["missing column"] = claims_zip({"Site": [{"SiteID": "PGE-S9", "SiteCity": "Oakland"}]})
    before = {}
    for name, data in files.items():
        before[name] = findings_of(claims.upload_claims(filer, deadline(), pge, data, f"{name}.zip"))
    assert before["bad"] and before["missing column"] and not before["q1"]
    for attr, value in claims_constants_from(current("claims")).items():
        monkeypatch.setattr(claims_spec, attr, value)
    for name, data in files.items():
        assert findings_of(claims.upload_claims(filer, deadline(), pge, data, f"{name}.zip")) == before[name], name


def _zip(tables_: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in tables_.items():
            z.writestr(f"{name}.csv", text)
    return out.getvalue()


def test_budget_and_monthly_uploads_are_refused_alike_from_the_version(monkeypatch):
    budget = _zip({"Measure": "CEInputID,PrgID,ClaimYearQuarter\nPGE-1,PGE_Res,2028Q1\n", "ProgramCost": "PrgID,PrgYear,PA\n"})
    monthly = _zip({"Commitments": "PA,PrgID\n", "Measure": "CEInputID\n", "ProgramCost": "PrgID\n"})
    errors = {}
    for name, read, data in (("budget", jobfile.read_upload, budget), ("monthly", monthly_upload.read_upload, monthly)):
        with pytest.raises(ValueError) as e:
            read(data, "x.zip")
        errors[name] = str(e.value)
    for code, module in (("budget_filing", jobfile), ("monthly", monthly_upload)):
        lay = specifications.layout(current(code))
        monkeypatch.setattr(module, "REQUIRED", {t.name: list(t.refuse_without) for t in lay.tables})
    for name, read, data in (("budget", jobfile.read_upload, budget), ("monthly", monthly_upload.read_upload, monthly)):
        with pytest.raises(ValueError, match="^" + errors[name].replace("(", r"\(").replace(")", r"\)") + "$"):
            read(data, "x.zip")


# --- the database's freeze


def test_a_published_version_and_its_parts_never_change():
    v = current("monthly")
    for change in (
        lambda: SpecificationVersion.objects.filter(pk=v.pk).update(summary="changed"),
        lambda: SpecificationVersion.objects.filter(pk=v.pk).update(status="draft"),
        lambda: SpecTable.objects.filter(version=v).update(guidance="changed"),
        lambda: SpecField.objects.filter(table__version=v).update(requirement="optional"),
        lambda: SpecField.objects.filter(table__version=v).delete(),
        lambda: CheckSetting.objects.filter(version=v).update(settings={"tolerance": 0.01}),
        lambda: SpecTable.objects.create(version=v, position=99, name="Extra"),
        lambda: SpecificationVersion.objects.filter(pk=v.pk).delete(),
    ):
        with pytest.raises(REFUSED), transaction.atomic():
            change()
    with transaction.atomic():  # withdrawn, and nothing else, is allowed
        SpecificationVersion.objects.filter(pk=v.pk).update(status="withdrawn", withdrawn_reason="test", withdrawn_at=timezone.now())
        with pytest.raises(REFUSED), transaction.atomic():
            SpecificationVersion.objects.filter(pk=v.pk).update(status="published")
        transaction.set_rollback(True)


def test_a_draft_is_edited_freely_and_named_uniquely():
    doc = specifications.stored_starting("monthly")
    doc["version"]["name"] = "Monthly reports 2028"
    draft = specifications.load(doc, status="draft")
    SpecField.objects.filter(table__version=draft, name="Comments").update(guidance="Say why")
    SpecTable.objects.filter(version=draft, name="Commitments").update(guidance="changed")
    assert draft.slug == "monthly-reports-2028" and draft.fingerprint == ""
    with pytest.raises(specifications.SpecificationError, match="already has a version called"):
        specifications.load(doc)
    doc["version"]["name"] = "Another draft"
    with pytest.raises(IntegrityError), transaction.atomic():  # one draft at a time
        specifications.load(doc, status="draft")


# --- filings and their versions


def test_a_deadline_put_on_the_calendar_is_pinned_to_the_current_version(filer, year, pge):  # noqa: F811
    v = current("claims")
    assert all(d.specification_version == v for d in claims.periods(2028))
    sv = claims.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    assert sv.specification_version == v and sv.rule_set_id == sv.specification_version.rule_set_id


def test_a_newer_version_is_used_by_new_deadlines_only(cpuc_admin, year):  # noqa: F811
    doc = specifications.stored_starting("claims")
    doc["version"]["name"] = "Claims 2029"
    newer = specifications.load(doc)
    assert current("claims") == newer
    due = {kind: timezone.now() + timedelta(days=30 * (i + 1)) for i, (kind, _) in enumerate(claims.PERIODS)}
    claims.add_year(cpuc_admin, year=2029, acc_version="2026", due=due)
    assert {d.specification_version.name for d in claims.periods(2029)} == {"Claims 2029"}
    assert {d.specification_version.name for d in claims.periods(2028)} == {"Starting version"}


def test_a_legacy_year_imported_as_published_names_no_version(cpuc_admin, pge, year):  # noqa: F811
    d = deadline("Annual (final) claim")
    assert d.specification_version is not None
    submissions.import_version(None, deadline=d, organisation=pge, files=[], reason="legacy CEDARS download")
    d.refresh_from_db()
    assert d.specification_version is None
    assert not SubmissionVersion.objects.filter(specification_version__isnull=False).exists()


def test_the_13th_month_report_has_no_specification(cpuc_admin):
    from cedars.modules.monthly import service as monthly

    monthly.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=20, closing_due=date(2029, 3, 1))
    months = Deadline.objects.filter(module="monthly")
    assert {d.specification_version.name for d in months.exclude(kind=monthly.CLOSING_KIND)} == {"Starting version"}
    assert months.get(kind=monthly.CLOSING_KIND).specification_version is None
