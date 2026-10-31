import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from cedars.modules.documents import service, studies
from cedars.modules.documents.models import AttributeOption, Study
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from tests.conftest import sign_in
from tests.test_documents import details, make_people, option, upload

pytestmark = pytest.mark.django_db


def data(manager, title="Residential HVAC impact evaluation", **kw):
    kw.setdefault("lead", option("study_lead", "CPUC"))
    return studies.StudyData(title=title, manager=manager, **kw)


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


@pytest.fixture
def hvac(pge):
    return registry.create_program(None, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1))


@pytest.fixture
def study(people):
    """A CPUC-led impact evaluation managed by an evaluation contractor."""
    d = data(
        people["dnv"],
        study_type=option("study_type", "Impact Evaluation"),
        sectors=[option("sector", "Residential")],
        funding_cycles=[option("portfolio_cycle", "2022")],
        status=option("study_status", "Data/Analysis"),
        budget=Decimal("850000"),
        contacts="Dana at DNV, 555-0100",
    )
    return studies.create_study(people["ed"], None, d)


def test_the_register_starts_with_the_psrs_lists():
    assert AttributeOption.objects.filter(attribute_list__code="study_status").count() == 11
    assert option("study_status", "Completed").attribute_list.collections == []
    assert option("study_lead", "REN")
    assert option("portfolio_cycle", "2022") and option("portfolio_cycle", "2026")


def test_who_adds_and_changes_studies(people, pge, study):
    assert study.number == 1 and study.study_id == "2022_Residential_1"
    # A PA adds its own studies only, as their manager.
    writer = people["pge_writer"]
    pa_led = data(pge, "PG&E market study", lead=option("study_lead", "IOU"))
    with pytest.raises(AccessDenied):
        studies.create_study(writer, pge, data(people["dnv"], "Not mine"))
    s2 = studies.create_study(writer, pge, pa_led)
    assert s2.number == 2 and s2.study_id == f"{timezone.localdate().year}_Cross-Cutting_2"
    # An evaluator's organization has no right to add studies, but updates the ones it manages.
    assert not studies.can_create(people["evaluator"], people["dnv"])
    assert studies.can_edit(people["evaluator"], study) and not studies.can_edit(writer, study)
    with pytest.raises(AccessDenied):
        studies.change_study(writer, study, data(people["dnv"]), reason="x")
    # Only CPUC ED hands a study to another manager.
    with pytest.raises(AccessDenied, match="another manager"):
        studies.change_study(people["evaluator"], study, data(pge), reason="handover")
    with pytest.raises(service.DocumentError, match="reason"):
        studies.change_study(people["evaluator"], study, data(people["dnv"]), reason=" ")


def test_a_studys_history_keeps_status_and_budget_and_hides_contacts_from_the_public(people, study, client):
    evaluator = people["evaluator"]
    d = data(
        people["dnv"],
        study_type=study.study_type,
        sectors=list(study.sectors.all()),
        funding_cycles=list(study.funding_cycles.all()),
        status=option("study_status", "Completed"),
        budget=Decimal("910000"),
        contacts="Dana at DNV, 555-0199",
        calmac_id="CPU0350.01",
    )
    studies.change_study(evaluator, study, d, reason="Final report published")
    study.refresh_from_db()
    assert study.study_id == "2022_Residential_1"  # an ID never changes
    public = studies.history(study, public=True)
    assert public[0]["action"] == "Final report published" and public[0]["status_or_budget"]
    fields = {c[0] for c in public[0]["changes"]}
    assert {"status", "budget", "calmac_id"} <= fields and "contacts" not in fields
    assert all("@" not in h["who"] for h in public)
    assert "contacts" in {c[0] for c in studies.history(study, public=False)[0]["changes"]}
    page = client.get(f"/documents/studies/{study.number}/").content.decode()
    assert "Completed" in page and "555-01" not in page
    sign_in(client, evaluator)
    assert "555-0199" in client.get(f"/documents/studies/{study.number}/").content.decode()


def test_retired_values_are_refused_on_new_studies_but_kept_on_old_ones(people, study):
    ed = people["ed"]
    service.retire_option(ed, option("study_status", "Data/Analysis"), reason="renamed")
    with pytest.raises(service.DocumentError, match="retired"):
        studies.create_study(ed, None, data(people["dnv"], "New", status=option("study_status", "Data/Analysis")))
    keep = data(people["dnv"], study.title, status=study.status, sectors=list(study.sectors.all()), notes="kept")
    studies.change_study(ed, study, keep, reason="notes")


def test_programs_evaluated_are_listed_on_the_program_page(people, pge, cpuc, study, hvac, client):
    evaluator = people["evaluator"]
    with pytest.raises(AccessDenied):
        studies.set_coverage(people["pge_writer"], study, hvac, 2022, 2024)
    with pytest.raises(service.DocumentError, match="in order"):
        studies.set_coverage(evaluator, study, hvac, 2025, 2022)
    row = studies.set_coverage(evaluator, study, hvac, 2022, 2024, whole=False, note="Heat pumps only")
    assert row.years == "2022 to 2024"
    studies.set_coverage(evaluator, study, hvac, 2022, 2025, whole=False, note="Heat pumps only")
    assert study.coverage.get().last_year == 2025
    assert [s.pk for s in studies.covering(hvac, 2025)] == [study.pk] and studies.covering(hvac, 2021) == []
    # The program page reads it through the registry, without Programs importing Documents.
    report = details("emv", "Final Evaluation Report", "HVAC impact final report", study=study)
    doc = service.create_document(people["ed"], cpuc, "emv", report, [upload()], publish=True)
    evals = registry.evaluations(hvac)
    assert evals[0]["study_id"] == study.study_id and not evals[0]["whole"]
    assert evals[0]["deliverable"]["url"] == doc.get_absolute_url()
    sign_in(client, people["pge_writer"])
    page = client.get(f"/programs/{hvac.prg_id}/").content.decode()
    assert "Evaluations" in page and study.study_id in page and "Part of the program" in page and "HVAC impact final report" in page
    # Removing a program needs a reason; both are in the study's audit trail.
    with pytest.raises(service.DocumentError):
        studies.remove_coverage(evaluator, row, reason="")
    studies.remove_coverage(evaluator, study.coverage.get(), reason="Out of scope")
    assert registry.evaluations(hvac) == []
    actions = list(AuditEvent.objects.filter(action__startswith="documents.study_program").values_list("action", flat=True))
    assert actions.count("documents.study_program_set") == 2 and "documents.study_program_removed" in actions


def test_coverage_through_the_page(people, study, hvac, client):
    sign_in(client, people["evaluator"])
    r = client.post(
        f"/documents/studies/{study.number}/programs",
        {"program": "pge_res_hvac", "first_year": 2022, "last_year": 2024, "whole": "on"},
    )
    assert r.status_code == 302 and study.coverage.get().program == hvac
    r = client.post(f"/documents/studies/{study.number}/programs", {"program": "NOPE", "first_year": 2022, "last_year": 2024}, follow=True)
    assert "Not in the program registry" in r.content.decode()


def test_the_register_filters_and_its_file(people, pge, study, hvac, client):
    other = studies.create_study(
        people["pge_writer"],
        pge,
        data(pge, "Commercial lighting process evaluation", lead=option("study_lead", "IOU"), sectors=[option("sector", "Commercial")]),
    )
    studies.set_coverage(people["evaluator"], study, hvac, 2022, 2024)

    def ids(**kw):
        return [s.study_id for s in studies.register(studies.RegisterFilter(**kw))]

    assert set(ids()) == {study.study_id, other.study_id}
    assert ids(q="lighting") == [other.study_id]
    assert ids(sectors=[option("sector", "Residential")]) == [study.study_id]
    assert ids(leads=[option("study_lead", "IOU")]) == [other.study_id]
    assert ids(program="pge_res_hvac") == [study.study_id]
    assert ids(managers=[pge]) == [other.study_id]
    page = client.get("/documents/studies/", {"q": "lighting"}).content.decode()
    assert other.title in page and study.title not in page
    rows = list(csv.reader(io.StringIO(client.get("/documents/studies/studies.csv").content.decode())))
    header = rows[0]
    assert header[0] == "StudyID" and "Contacts" not in header
    by_id = {r[0]: dict(zip(header, r, strict=True)) for r in rows[1:]}
    assert by_id[study.study_id]["ProgramsEvaluated"] == "PGE_Res_HVAC 2022 to 2024"
    assert by_id[study.study_id]["Budget"] == "850000.00"
    report = client.get(f"/documents/studies/{study.number}/report.csv").content.decode()
    assert 'ProgramEvaluated,"PGE_Res_HVAC, 2022 to 2024, whole"' in report and "555" not in report


def test_the_budget_summary_counts_planned_against_completed(people, study):
    ed = people["ed"]
    cycle = option("portfolio_cycle", "2022")
    done = studies.create_study(
        ed,
        None,
        data(
            people["dnv"],
            "Done",
            sectors=[option("sector", "Residential"), option("sector", "Commercial")],
            funding_cycles=[cycle],
            status=option("study_status", "Completed"),
            budget=Decimal("100000"),
        ),
    )
    studies.create_study(
        ed, None, data(people["dnv"], "Dropped", funding_cycles=[cycle], status=option("study_status", "Cancelled"), budget=Decimal(5))
    )
    studies.create_study(ed, None, data(people["dnv"], "Other cycle", funding_cycles=[option("portfolio_cycle", "2026")]))
    result = studies.summary(cycle)
    t = result["total"]
    assert (t["studies"], t["budget"], t["completed"], t["completed_budget"], t["cancelled"]) == (2, Decimal(950000), 1, Decimal(100000), 1)
    sectors = dict(result["tables"]["sector"]["rows"])
    assert sectors["Residential"]["studies"] == 2 and sectors["Commercial"]["completed"] == 1
    lead = dict(result["tables"]["lead"]["rows"])
    assert sum(r["studies"] for r in lead.values()) == 2
    assert done.pk


def test_the_budget_summary_page_and_file(study, client):
    cycle = option("portfolio_cycle", "2022")
    assert client.get("/documents/studies/budgets/").status_code == 200
    r = client.get("/documents/studies/budgets/", {"cycle": cycle.pk, "format": "csv"})
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[-1] == ["2022", "Total", "", "1", "850000.00", "0", "0"]
    assert client.get("/documents/studies/budgets/", {"cycle": "x"}).status_code == 404


def test_work_orders_and_tasks(people, cpuc, study):
    ed = people["ed"]
    with pytest.raises(AccessDenied):
        studies.add_work_order(people["evaluator"], number="WO42")
    wo = studies.add_work_order(ed, number="WO42", name="Residential impact", firm=people["dnv"])
    with pytest.raises(service.DocumentError, match="already listed"):
        studies.add_work_order(ed, number="wo42")
    task = studies.add_task(ed, wo, "Task 3: HVAC")
    with pytest.raises(service.DocumentError, match="already has"):
        studies.add_task(ed, wo, "task 3: hvac")
    other = studies.add_work_order(ed, number="WO43")
    stray = studies.add_task(ed, other, "Task 1")
    # A document is filed under a work order and one of its tasks, on EM&V documents only.
    with pytest.raises(service.DocumentError, match="not one of"):
        service.create_document(ed, cpuc, "emv", details("emv", "Report", work_order=wo, task=stray), [upload()])
    with pytest.raises(service.DocumentError, match="Only EM&V"):
        service.create_document(ed, cpuc, "guidance", details("guidance", "Guidance document", work_order=wo), [upload()])
    doc = service.create_document(ed, cpuc, "emv", details("emv", "Report", work_order=wo, task=task), [upload()], publish=True)
    hits = service.search(None, service.Search(work_order=wo))
    assert [d.pk for d in hits] == [doc.pk]
    studies.set_cancelled(ed, wo, True, reason="Contract ended")
    wo.refresh_from_db()
    assert wo.cancelled


def test_only_the_study_manager_and_ed_file_documents_under_a_study(people, pge, cpuc, study):
    with pytest.raises(AccessDenied, match="study manager"):
        service.create_document(people["pge_writer"], pge, "emv", details("emv", "Report", study=study), [upload()])
    doc = service.create_document(people["evaluator"], people["dnv"], "emv", details("emv", "Report", study=study), [upload()])
    assert doc.study == study
    with pytest.raises(service.DocumentError, match="Only EM&V documents and program files"):
        service.create_document(people["ed"], cpuc, "guidance", details("guidance", "Guidance document", study=study), [upload()])


def test_the_activity_report(people, study):
    ed = people["ed"]
    soon = timezone.localdate() + timedelta(days=10)
    keep = dict(sectors=list(study.sectors.all()), funding_cycles=list(study.funding_cycles.all()), status=study.status)
    studies.change_study(
        ed, study, data(people["dnv"], study.title, next_activity="Draft report", next_activity_due=soon, **keep), reason="plan"
    )
    assert studies.upcoming() == [study]
    assert studies.recently_updated()[0] == study


def test_study_pages_and_access(people, pge, study, client):
    assert client.get("/documents/studies/").status_code == 200
    assert client.get(f"/documents/studies/{study.number}/").status_code == 200
    assert client.get("/documents/studies/999/").status_code == 404
    assert client.get("/documents/work-orders/").status_code == 200
    assert client.get("/documents/studies/new/").status_code == 302  # sign in first
    sign_in(client, people["evaluator"])
    assert client.get("/documents/studies/new/").status_code == 404  # not a PA
    assert client.get(f"/documents/studies/{study.number}/change/").status_code == 200
    sign_in(client, people["pge_writer"])
    assert client.get(f"/documents/studies/{study.number}/change/").status_code == 404
    page = client.get("/documents/studies/new/")
    assert page.status_code == 200
    form = page.context["form"]
    assert list(form.fields["manager"].queryset) == [pge]
    r = client.post(
        "/documents/studies/new/",
        {"title": "PG&E market study", "manager": pge.pk, "lead": option("study_lead", "IOU").pk, "budget": "1200"},
    )
    assert r.status_code == 302
    s = Study.objects.get(title="PG&E market study")
    assert s.manager == pge and s.budget == Decimal("1200")
