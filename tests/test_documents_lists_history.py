"""Documents and EM&V (UI review 3): the document value lists are an index with a page for each list, a study's history reads in plain
words, and canceling a work order is folded away."""

from datetime import date
from decimal import Decimal

import pytest

from cedars.modules.documents import studies
from cedars.modules.documents.models import AttributeList, WorkOrder
from cedars.services.registry import service as registry
from tests.conftest import page_only
from tests.test_documents import make_people, option

pytestmark = pytest.mark.django_db


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


def test_the_value_lists_are_an_index_with_a_page_for_each(people, signed_in, client):
    html = client.get("/documents/lists/").content.decode()
    sector = AttributeList.objects.get(code="sector")
    assert (
        f'<a href="/documents/lists/sector/">{sector.name}</a>' in html and 'id="list-sector"' in html
    )  # old #list- links land on its row
    assert "Correct the label" not in html and "<form" not in page_only(html).split("<main", 1)[1].split("</main>")[0]
    page = client.get("/documents/lists/sector/").content.decode()
    assert f"<h1>{sector.name}</h1>" in page and "Commercial" in page and "Correct the label" not in page
    assert 'href="/documents/lists/">Document value lists</a>' in page  # the breadcrumb back
    assert client.get("/documents/lists/no-such-list/").status_code == 404
    ed = signed_in(people["ed"])
    assert "Correct the label" in ed.get("/documents/lists/sector/").content.decode()
    r = ed.post("/documents/lists/sector/add", {"label": "Water-Energy Nexus", "reason": "a new sector"})
    assert r.status_code == 302 and r["Location"] == "/documents/lists/sector/"  # back to the list's own page


def test_a_studys_history_reads_in_plain_words(people, pge, client):
    hvac = registry.create_program(None, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1))
    d = studies.StudyData(
        title="Residential HVAC impact evaluation",
        manager=people["dnv"],
        lead=option("study_lead", "CPUC"),
        status=option("study_status", "Design"),
        budget=Decimal("850000"),
    )
    study = studies.create_study(people["ed"], None, d)
    studies.change_study(
        people["ed"],
        study,
        studies.StudyData(
            **(vars(d) | {"status": option("study_status", "Data/Analysis"), "budget": Decimal("900000"), "calmac_id": "CPU0123"})
        ),
        reason="Fieldwork started",
    )
    row = studies.set_coverage(people["ed"], study, hvac, 2022, 2024)
    studies.set_coverage(people["ed"], study, hvac, 2022, 2025, whole=False, note="Heat pumps only")
    row.refresh_from_db()
    studies.remove_coverage(people["ed"], row, reason="Out of scope")
    lines = [line for h in studies.history(study) for line in h["lines"]]
    assert lines == [
        "PGE_Res_HVAC removed from the programs evaluated (program years 2022 to 2025).",
        "PGE_Res_HVAC now covers program years 2022 to 2025, part of the program (Heat pumps only) (was program years 2022 to 2024).",
        "PGE_Res_HVAC added to the programs evaluated: program years 2022 to 2024, the whole program.",
        "Budget changed from $850,000 to $900,000.",
        "CALMAC ID set to CPU0123.",
        "Status changed from Design to Data/Analysis.",
        "Added to the register, with status Design and a budget of $850,000.",
    ]
    html = client.get(study.get_absolute_url()).content.decode()
    assert "Status changed from Design to Data/Analysis." in html and "Fieldwork started" in html
    assert "Firstyear" not in html and "Lastyear" not in html


def test_canceling_a_work_order_is_folded_away(people, signed_in, client):
    wo = WorkOrder.objects.create(number="ED_O_123", name="Residential impact evaluations")
    ed = signed_in(people["ed"])
    html = page_only(ed.get(f"/documents/work-orders/{wo.pk}/").content.decode())
    assert '<details class="section" id="wo-status">' in html and "<summary>Cancel the work order</summary>" in html
    assert html.index('id="docs-h"') < html.index('<details class="section" id="wo-status">')  # at the end of the page, after its documents
    client.logout()
    assert '<details class="section" id="wo-status">' not in page_only(client.get(f"/documents/work-orders/{wo.pk}/").content.decode())
