import csv
import io
import json
from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from cedars.modules.documents import psr_import, studies
from cedars.modules.documents.models import Study, WorkOrder
from cedars.services.audit.models import AuditEvent
from tests.test_documents import make_people, option

pytestmark = pytest.mark.django_db


def project(pid, **fields):
    """A project as /api/projects gives it (the capture of 2026-10-30: 34 keys, the lists as objects)."""
    record = {
        "projectID": pid,
        "WONum": "N/A",
        "studyName": f"Study {pid}",
        "projectDesc": None,
        "notes": None,
        "projectBudget": None,
        "WOLead": None,
        "studyManager": None,
        "EDProgramManager": None,
        "sub_sector": None,
        "studyType": None,
        "nextActivityDesc": None,
        "nextActivityDueDate": None,
        "planPostedToPDA": None,
        "EDConsultants": None,
        "PGEContact": None,
        "SCEContact": None,
        "SDGEContact": None,
        "SCGContact": None,
        "projectCoordinationGroup": None,
        "PCGCoordinator": None,
        "PCGEDRep": None,
        "PCGOtherED": None,
        "researchQuestions": None,
        "potentialEMVMethods": None,
        "updatedAt": None,
        "objective": None,
        "completionDate": None,
        "calmacID": None,
        "studyID": f"2025_Residential_{pid}",
        "studyLeads": {"studyLeadID": 1, "name": "ED"},
        "fundingCycles": [{"fundingCycleID": 13, "name": "2025"}],
        "projectStatus": {},
        "sectors": [{"sectorID": 1, "name": "Residential"}],
    }
    record.update(fields)
    return record


PROJECTS = [
    project(
        733,
        WONum="23NC0465",
        studyName="Residential HVAC impact evaluation",
        projectDesc="x" * 1200,
        projectBudget=850000,
        studyManager="DNV GL",
        EDProgramManager="Ed Staff",
        WOLead="A Lead",
        sub_sector="Residential",
        studyType="Impact",
        nextActivityDesc="Draft report",
        nextActivityDueDate="2026-04-17T07:00:00.000Z",  # a Pacific midnight
        completionDate="2028-06-30T00:00:00.000Z",  # the date as entered
        calmacID="N/A",
        planPostedToPDA="Yes",
        PGEContact="A Contact",
        updatedAt="2026-03-06T23:36:34.000Z",
        projectStatus={"projectStatusID": 5, "projectStatus": "Data/Analysis"},
        fundingCycles=[{"fundingCycleID": 13, "name": "2025"}, {"fundingCycleID": 14, "name": "2026"}],
        sectors=[{"sectorID": 1, "name": "Residential"}, {"sectorID": 7, "name": "Cross-Cutting"}],
    ),
    project(
        123,
        WONum="1028",
        studyName="PG&amp;E lighting process evaluation",
        studyID="2013-2015__123",
        studyManager="PG&amp;E",
        EDProgramManager="[redacted]",
        studyType="Market Assessment",
        sub_sector="Cross-Cutting (Group B)",
        calmacID="PGE0450",
        studyLeads={"studyLeadID": 2, "name": "IOU"},
        fundingCycles=[{"fundingCycleID": 2, "name": "2013-2015"}],
        projectStatus={"projectStatusID": 7, "projectStatus": "Completed"},
        sectors=[],
        projectBudget=0,
    ),
    project(
        740,
        WONum="17PS5017",
        studyManager="A Person",
        studyType="Process",
        studyLeads={"studyLeadID": 3, "name": "REN"},
        fundingCycles=[{"fundingCycleID": 15, "name": "2027"}],
    ),
]


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


@pytest.fixture
def capture(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(PROJECTS))
    return path


def run(capture, *extra):
    out = io.StringIO()
    call_command("documents_import_psr", "--projects", str(capture), *extra, stdout=out)
    return out.getvalue()


def test_a_dry_run_maps_everything_and_stores_nothing(people, capture, cpuc_admin):
    studies.add_work_order(cpuc_admin, number="23NC0465", name="Group B sectors")
    out = run(capture, "--dry-run")
    assert out.startswith("Dry run: nothing stored.")
    assert "3 studies imported" in out
    assert "1 projects linked to a work order; 1 work orders added" in out and "1 other work order numbers kept" in out
    assert "1 projects whose study manager matched no organization" in out
    assert "'Market Assessment' x1" in out and "'Cross-Cutting (Group B)' x1" in out
    assert "description x1" in out
    assert not Study.objects.exists() and WorkOrder.objects.count() == 1


def test_the_psr_is_migrated_with_its_numbers_lists_and_contacts(people, capture, cpuc_admin, cpuc, pge, client, signed_in):
    wo = studies.add_work_order(cpuc_admin, number="23NC0465", name="Group B sectors")
    out = run(capture)
    assert "3 studies imported" in out

    s = Study.objects.get(number=733)
    assert s.study_id == "2025_Residential_733" and s.title == "Residential HVAC impact evaluation"
    assert (s.lead.label, s.manager, s.status.label, s.study_type.label, s.group.label) == (
        "CPUC",
        people["dnv"],
        "Data/Analysis",
        "Impact Evaluation",
        "Residential",
    )
    assert sorted(o.label for o in s.sectors.all()) == ["Cross Cutting", "Residential"]
    assert sorted(o.label for o in s.funding_cycles.all()) == ["2025", "2026"]
    assert s.work_order == wo and s.budget == Decimal("850000") and s.calmac_id == ""
    assert (s.next_activity_due, s.completion_date) == (date(2026, 4, 17), date(2028, 6, 30))
    assert len(s.description) == 1200  # longer than the form takes: kept whole
    assert s.ed_program_manager == people["ed"]
    assert s.contacts == "Work order lead: A Lead\nPG&E contact: A Contact"
    assert s.psr == {
        "project": 733,
        "study_id": "2025_Residential_733",
        "fields": {"Study manager": "DNV GL", "Plan posted to the PDA": "Yes"},
    }
    assert s.updated_at.date() == date(2026, 3, 6) and s.created_at == s.updated_at

    iou = Study.objects.get(number=123)
    assert iou.study_id == "2013-2015__123" and iou.title == "PG&E lighting process evaluation"
    assert (iou.lead.label, iou.manager, iou.study_type, iou.group, iou.work_order) == ("IOU", pge, None, None, None)
    assert iou.calmac_id == "PGE0450" and iou.budget == 0 and not iou.sectors.exists()
    assert iou.contacts == "ED program manager: [redacted]"
    assert iou.psr["fields"] == {
        "Study manager": "PG&E",
        "Study type": "Market Assessment",
        "Group": "Cross-Cutting (Group B)",
        "Work order number": "1028",
    }

    ren = Study.objects.get(number=740)
    assert (ren.lead.label, ren.manager, ren.study_type.label) == ("REN", cpuc, "Process Evaluation")  # an unknown manager: the CPUC's
    assert ren.work_order.number == "17PS5017" and ren.psr["fields"] == {"Study manager": "A Person"}
    added = AuditEvent.objects.filter(action="documents.study_added")
    assert added.count() == 3 and {e.actor_label for e in added} == {"PSR migration"}
    assert AuditEvent.objects.filter(action="documents.work_order_imported").count() == 1

    # the old link, the public page (no PSR values, no contacts) and CPUC ED's page
    r = client.get("/documents/psr/project-status-view-edit/733")
    assert (r.status_code, r["Location"]) == (301, s.get_absolute_url())
    page = client.get(s.get_absolute_url()).content.decode()
    assert "The PSR, project 733" in page and "As the PSR had it" not in page and "DNV GL" not in page and "A Lead" not in page
    assert "imported" in page  # the history names no person
    page = signed_in(people["ed"]).get(s.get_absolute_url()).content.decode()
    assert "As the PSR had it" in page and "DNV GL" in page and "Work order lead: A Lead" in page

    again = run(capture)
    assert "0 studies imported, 3 already in the register" in again and Study.objects.count() == 3


def test_a_map_file_settles_what_the_lists_do_not_take(people, capture, tmp_path, pge):
    listed = tmp_path / "listed.csv"
    out = run(capture, "--write-map", str(listed))
    assert "values written" in out and not Study.objects.exists()
    with open(listed, newline="") as f:
        rows = {(r["field"], r["psr_value"]): r["register_value"] for r in csv.DictReader(f)}
    assert rows[("manager", "PG&E")] == "PGE" and rows[("manager", "A Person")] == ""
    assert rows[("study_type", "Impact")] == "Impact Evaluation" and rows[("study_type", "Market Assessment")] == ""
    assert rows[("sectors", "Cross-Cutting")] == "Cross Cutting" and rows[("lead", "ED")] == "CPUC"

    mapping = tmp_path / "map.csv"
    mapping.write_text(
        "field,psr_value,register_value\n"
        "study_type,Market Assessment,Market Study\n"
        "group,Cross-Cutting (Group B),Group B\n"
        "manager,A Person,BAYREN\n"
    )
    run(capture, "--map", str(mapping))
    iou = Study.objects.get(number=123)
    assert (iou.study_type.label, iou.group.label) == ("Market Study", "Group B")
    assert Study.objects.get(number=740).manager == people["bayren"]

    bad = tmp_path / "bad.csv"
    bad.write_text("field,psr_value,register_value\ncolour,red,blue\n")
    with pytest.raises(CommandError, match="names a field 'colour'"):
        run(capture, "--map", str(bad))


def test_a_number_taken_here_is_left_out(people, capture, cpuc_admin, cpuc):
    studies.create_study(
        cpuc_admin, cpuc, studies.StudyData(title="Added here", manager=cpuc, lead=option("study_lead", "CPUC")), number=740
    )
    out = run(capture)
    assert "2 studies imported" in out and "1 projects left out, their number is taken by a study added here: 740" in out
    assert Study.objects.get(number=740).title == "Added here"


def test_psr_dates():
    assert psr_import._psr_day("2027-12-31T00:00:00.000Z") == date(2027, 12, 31)
    assert psr_import._psr_day("2025-11-30T08:00:00.000Z") == date(2025, 11, 30)
    assert psr_import._psr_day(None) is None
    assert psr_import._labels({"projectStatusID": 4, "projectStatus": "Design", "enabled": True, "sortOrder": 4}) == ["Design"]
    assert psr_import._labels([{"sectorID": 1, "name": "Residential", "createdOn": "2019-01-01"}]) == ["Residential"]
