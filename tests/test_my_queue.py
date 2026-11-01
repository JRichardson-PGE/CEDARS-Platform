"""Each module's work in the queue (cedars/core/queue.py, BR-09.5): fund shifts, documents, program changes and access requests."""

from datetime import date, timedelta

import pytest

from cedars.core import queue
from cedars.modules.budgets import shifts
from cedars.modules.documents import service as documents
from cedars.services.accounts import service as access
from cedars.services.registry import service as registry

from .test_budgets import filer, filing, programs, release  # noqa: F401 (fixtures)
from .test_documents import details, jcm, people, upload  # noqa: F401 (fixtures)
from .test_fund_shifts import L, cycle, sce_confirmer, statewide  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def items(user, kind):
    return {s.kind.key: s.items for s in queue.sections(user)}[kind]


def test_a_cofunder_acknowledges_from_the_queue(filer, pge, cycle, statewide, sce_confirmer, programs):  # noqa: F811
    s = shifts.create(filer, pge, cycle, title="Into SW X", reason="r")
    shifts.set_lines(filer, s, L((statewide, 2028, 500), ("PGE_Com_Ltg", 2028, -300)))
    s = shifts.submit(filer, s)
    (item,) = items(sce_confirmer, "fund_shift")
    assert item.organisation == "SCE" and item.url == f"/budgets/shifts/{s.pk}/acknowledge/" and "Into SW X" in item.what
    assert items(filer, "fund_shift") == []
    shifts.acknowledge(sce_confirmer, s, programs, L(("SCE_Ind", 2028, -200)))
    assert items(sce_confirmer, "fund_shift") == []


def test_documents_to_publish_and_responses_owed(people, pge, jcm):  # noqa: F811
    (item,) = items(people["pge_writer"], "document_draft")  # documents.confirm in PG&E, the owner
    assert "Joint Cooperation Memo" in item.what and item.organisation == "PGE"
    assert items(people["pge_drafter"], "document_draft") == []  # submit only: may not publish
    documents.publish_version(people["pge_writer"], jcm.versions.get())
    assert items(people["pge_writer"], "document_draft") == []


def test_program_changes_and_access_requests(cpuc_admin, cpuc, pge, pge_admin, make_user):
    approver = make_user("programs@cpuc.ca.gov", "Pro Grams")
    access.grant(cpuc_admin, approver, cpuc, "programs.approve")
    p = registry.create_program(cpuc_admin, prg_id="PGE_Q1", administrator=pge, valid_from=date(2024, 1, 1), name="Queue test")
    registry.request_change(pge_admin, p, effective=date.today() + timedelta(days=30), reason="a new segment", segment="market_support")
    (item,) = items(approver, "program_change")
    assert item.what.startswith("PGE_Q1") and "a new segment" in item.note
    assert items(pge_admin, "program_change") == []

    newcomer = make_user("new@example.com", "New Comer")
    access.request_access(newcomer, pge, ["claims.read"], "I work on PG&E's claims")
    (item,) = items(pge_admin, "access")
    assert "New Comer asks to join PGE" == item.what and item.url == "/admin/organizations/PGE/"
    assert items(newcomer, "access") == []
