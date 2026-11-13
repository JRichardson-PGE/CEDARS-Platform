"""Filings read their specification version: the rule set a filing is checked against, and the engine release and cost-test set it is
priced with, are the ones its version names, whatever the date it is checked, corrected or priced. A version that names none keeps
what filings did before: the rule set of its code in force, the default release, the filing set's cost-test set."""

from datetime import date, timedelta

import pytest
from django.test import RequestFactory

from cedars.core.templatetags import ui
from cedars.modules.budgets import service as budgets
from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.rules import precheck
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification
from cedars.services.submissions.models import Deadline

from .test_budgets import filer, filing, job, programs, release  # noqa: F401 - fixtures
from .test_cost_test_sets import registered

pytestmark = pytest.mark.django_db


def engine_rules():
    pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")
    from cedars.services.rules import service as rules

    return rules


def version_of(code: str, name: str, **kw):
    """A published version of a specification, a copy of its starting version under another name; kw are load's (rule_set,
    cost_test_set) and, as `engine_release`, the release it names."""
    doc = specifications.stored_starting(code)
    doc["version"]["name"] = name
    if "engine_release" in kw:
        doc["version"]["engine_release"] = kw.pop("engine_release")
    return specifications.load(doc, **kw)


def pin(deadline, version):
    Deadline.objects.filter(pk=deadline.pk).update(specification_version=version)
    deadline.refresh_from_db()
    return deadline


def published_rules(rules, admin, code: str):
    """(the rule set the engine ships, a newer copy of it in force from today)."""
    first = rules.publish(admin, rules.import_from_engine(admin, code))
    newer = rules.publish(admin, rules.new_version(admin, first, date.today(), "a later version of the same rules"))
    assert rules.in_force(code) == newer
    return first, newer


# --- rules


def test_a_filing_is_checked_against_its_versions_rule_set_not_the_one_in_force(filer, filing, pge, programs, cpuc_admin):  # noqa: F811
    rules = engine_rules()
    first, newer = published_rules(rules, cpuc_admin, "budget_filing")
    pin(filing, version_of("budget_filing", "Budgets on the first rules", rule_set=first))
    v = budgets.upload(filer, filing, pge, job(), "tual.zip")
    assert v.rule_set_id == first.pk and v.findings["rule_set"] == f"{first.label} ({first.effective_from})"
    assert v.specification_version.name == "Budgets on the first rules"


def test_a_version_naming_no_rule_set_takes_its_codes_rule_set_in_force(cpuc_admin):
    rules = engine_rules()
    _, newer = published_rules(rules, cpuc_admin, "claims")
    starting = specifications.current(Specification.objects.get(code="claims"))
    assert starting.rule_set_id is None and starting.rule_set_code == "claims"  # no rule set existed when the migration ran here
    assert specifications.rule_set_of(starting) == newer
    assert specifications.rule_set_of(None, "claims") == newer  # a module with no version: its code's
    monthly = specifications.current(Specification.objects.get(code="monthly"))
    assert specifications.rule_set_of(monthly, "claims") is None  # a version that takes no rules takes none


def test_a_deadline_pinned_to_no_version_takes_its_modules_current_one(filing):  # noqa: F811
    Deadline.objects.filter(pk=filing.pk).update(specification_version=None)
    filing.refresh_from_db()
    assert specifications.of_filing(filing) == specifications.for_module("budgets")
    assert specifications.of_filing(None) is None


def test_the_pre_upload_checker_defaults_to_the_current_specifications_rules(cpuc_admin):
    rules = engine_rules()
    first, newer = published_rules(rules, cpuc_admin, "claims")
    version_of("claims", "Claims on the first rules", rule_set=first)  # now the current claims version
    checker, rs = precheck.resolve("claims", None)
    assert checker.module == "claims" and rs == first
    assert [v.name for v in precheck.specification_versions(first)] == ["Claims on the first rules"]
    assert precheck.resolve(None, newer)[1] == newer  # any other published version can still be chosen


def test_before_you_upload_links_the_rules_of_the_filings_version(cpuc_admin, pge_admin):
    rules = engine_rules()
    first, newer = published_rules(rules, cpuc_admin, "budget_filing")
    v = version_of("budget_filing", "Budgets on the first rules", rule_set=first)
    request = RequestFactory().get("/")
    request.user = pge_admin
    parts = ui.before_upload({"request": request}, specification=v, rule_set="budget_filing")["parts"]
    links = [p["url"] for p in parts if "url" in p]
    assert f"/rules/{first.pk}/" in links and f"/rules/{newer.pk}/" not in links


# --- pricing


def test_a_filing_is_priced_on_the_engine_release_its_version_names(filer, filing, pge, programs, release, cpuc_admin):  # noqa: F811
    named = calculations.add_release(cpuc_admin, release="0.1.81", calculations=["recet"], accs={"recet": ["2026"]})
    pin(filing, version_of("budget_filing", "Budgets on 0.1.81", engine_release="0.1.81"))
    v = budgets.upload(filer, filing, pge, job(), "tual.zip")
    run = Run.objects.get(pk=budgets.price(filer, v))
    assert run.engine_release == named.release == "0.1.81" and release.default


def test_a_version_naming_no_release_is_priced_on_the_default(filer, filing, pge, programs, release):  # noqa: F811
    assert specifications.engine_release_of(filing.specification_version) == ""
    v = budgets.upload(filer, filing, pge, job(), "tual.zip")
    assert Run.objects.get(pk=budgets.price(filer, v)).engine_release == release.release


def test_a_release_the_version_names_must_be_registered_and_carry_the_acc(filer, filing, pge, programs, release, cpuc_admin):  # noqa: F811
    pin(filing, version_of("budget_filing", "Budgets on 0.1.99", engine_release="0.1.99"))
    v = budgets.upload(filer, filing, pge, job(), "tual.zip")
    with pytest.raises(AccessDenied, match="not finished setting up pricing"):
        budgets.price(filer, v)  # the filer is told pricing is not set up; the CPUC's administrators are told why
    assert "not an active release" in refused()
    calculations.add_release(cpuc_admin, release="0.1.99", calculations=["recet"], accs={"recet": ["2024"]})
    with pytest.raises(AccessDenied):
        budgets.price(filer, v)
    assert "names, ReCET 0.1.99, has no 2026 ACC" in refused()


def refused() -> str:
    return AuditEvent.objects.filter(action="submission.pricing_unavailable").order_by("-pk").first().after["reason"]


def test_a_filing_is_priced_with_the_cost_test_set_its_version_names(filer, filing, pge, programs, release, cpuc_admin):  # noqa: F811
    registered(cpuc_admin)  # EE-2026, in force for the filing's years
    ee28 = registered(cpuc_admin, sid="EE-2028", years=(2030, None))
    pin(filing, version_of("budget_filing", "Budgets with EE-2028", cost_test_set=ee28))
    assert filing.budget_setup.cost_test_set is None
    v = budgets.upload(filer, filing, pge, job(), "tual.zip")
    run = Run.objects.get(pk=budgets.price(filer, v))
    assert run.cost_tests["pack"] == ee28.pk and run.settings["cost_test_set"] == "EE-2028"


def test_a_release_a_version_in_use_names_cannot_be_retired(filing, release, cpuc_admin):  # noqa: F811
    named = calculations.add_release(cpuc_admin, release="0.1.81", calculations=["recet"], accs={"recet": ["2026"]})
    v = version_of("budget_filing", "Budgets on 0.1.81", engine_release="0.1.81")
    calculations.retire_release(cpuc_admin, named, "nothing uses it yet")  # no filing is on the version
    calculations.restore_release(cpuc_admin, named, "back")
    pin(filing, v)
    with pytest.raises(AccessDenied, match="cannot be retired"):
        calculations.retire_release(cpuc_admin, named, "replaced")
    assert specifications.naming_release("0.1.81") == [v]
    filing.retired_at = filing.due_at + timedelta(days=1)
    filing.save(update_fields=["retired_at"])
    assert specifications.naming_release("0.1.81") == []
