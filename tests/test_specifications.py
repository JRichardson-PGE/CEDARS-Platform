"""Filing specifications (cedars.services.specifications): the starting versions, the database's freeze, filings pinned to a version, and
what the filing modules read of it (their tables, fields, keys and the columns an upload is refused without)."""

import io
import zipfile
from datetime import date, timedelta

import pytest
from django.db import IntegrityError, InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.modules.budgets import jobfile
from cedars.modules.claims import service as claims
from cedars.modules.monthly import upload as monthly_upload
from cedars.services.rules import coded
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import CheckSetting, SpecField, Specification, SpecificationVersion, SpecTable
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, SubmissionVersion

from .test_claims import claim, claims_zip, deadline, filer, programs, q1, row, tables, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
CODES = ("budget_filing", "claims", "monthly")
REFUSED = (InternalError, ProgrammingError, IntegrityError)


def current(code: str) -> SpecificationVersion:
    return specifications.current(Specification.objects.get(code=code))


# --- the starting versions


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
    assert lay.names(vault=False) == [
        "Claim", "Measure", "ProgramCost", "Site", "ContactClaim", "Participant", "InfrastructureCost", "ClaimSite", "Withdrawal",
    ]  # fmt: skip
    assert lay.names(vault=True) == ["PII_Contact", "PII_Site"]
    assert v.rule_set_code == "claims"
    assert lay.table("PII_Contact").keys == ("ContactID",) and lay.table("PII_Site").keys == ("SiteID",)
    assert lay.table("PII_Contact").field("ContactEmail").type == "email"
    assert lay.table("PII_Site").field("CensusTract").max_length == 11 and lay.table("PII_Site").field("SiteAddress").max_length == 255
    assert len(lay.table("Measure").columns()) == 74 and lay.table("ContactClaim").required_fields() == ["ClaimID", "ContactID"]
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


# --- what the filing modules read of their version


def _zip(tables_: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in tables_.items():
            z.writestr(f"{name}.csv", text)
    return out.getvalue()


def test_a_database_with_no_version_reads_the_starting_version_as_shipped(monkeypatch):
    """A database emptied and not loaded again (as some tests leave it) still has every filing module's layout: the stored document's."""
    stored = {code: specifications.document_layout(specifications.stored_starting(code)) for code in CODES}
    for code, module in (("budget_filing", "budgets"), ("claims", "claims"), ("monthly", "monthly")):
        assert specifications.layout_for_module(module).tables == specifications.layout(current(code)).tables == stored[code].tables
    monkeypatch.setattr(specifications, "for_module", lambda module: None)  # no version of any specification
    for code, module in (("budget_filing", "budgets"), ("claims", "claims"), ("monthly", "monthly")):
        assert specifications.layout_for_module(module).tables == stored[code].tables
        assert specifications.layout_for_module(module).version is None


def test_budget_and_monthly_uploads_are_refused_without_their_versions_columns():
    budget = _zip({"Measure": "CEInputID,PrgID,ClaimYearQuarter\nPGE-1,PGE_Res,2028Q1\n", "ProgramCost": "PrgID,PrgYear,PA\n"})
    monthly = _zip({"Commitments": "PA,PrgID\n", "Measure": "CEInputID\n", "ProgramCost": "PrgID\n"})
    required = {}
    for code in ("budget_filing", "monthly"):
        required[code] = {t.name: list(t.refuse_without) for t in specifications.layout(current(code)).tables}
    with pytest.raises(jobfile.JobError, match="^Measure has no NumUnits column.$"):
        jobfile.read_upload(budget, "x.zip", required["budget_filing"])
    with pytest.raises(monthly_upload.UploadError, match="^Commitments has no YearMonth, PriorCommittedFundsBalance"):
        monthly_upload.read_upload(monthly, "x.zip", required["monthly"])
    jobfile.read_upload(budget, "x.zip", {"Measure": ["CEInputID"]})  # a version refusing it without fewer columns takes it
    jobfile.read_upload(budget, "kept.zip")  # a job already kept is read without the check


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
