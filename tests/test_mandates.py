"""The register of CPUC-mandated data and calculation changes (gap 30, BR-12.7): kept by CPUC system administrators, public once
published, every change audited with a reason and shown as the entry's history, never deleted, linked to the published records that
carry it, and only public CPUC documents linked."""

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.db import InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import service as dictionary
from cedars.services.releases import mandates
from cedars.services.releases.mandates import MandateError
from cedars.services.releases.models import Mandate
from cedars.services.rules.models import RuleSet

from .conftest import page_only

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.89.json"
ENTRY = {
    "reference": "Res. E-5350",
    "authority": "resolution",
    "title": "Specification change log",
    "requirement": "Keep a public register of mandated data and calculation changes.",
    "effective_on": date(2026, 1, 1),
    "document_url": "https://docs.cpuc.ca.gov/PublishedDocs/Published/G000/M000/K000/000000.PDF",
}


@pytest.fixture
def entry(cpuc_admin):
    return mandates.create_draft(cpuc_admin, **ENTRY)


def test_only_system_administrators_keep_the_register(pge_admin, cpuc_admin):
    with pytest.raises(AccessDenied):
        mandates.create_draft(pge_admin, **ENTRY)
    m = mandates.create_draft(cpuc_admin, **ENTRY)
    with pytest.raises(AccessDenied):
        mandates.update(pge_admin, m, title="X")
    with pytest.raises(AccessDenied):
        mandates.publish(pge_admin, m)


def test_an_entry_names_a_public_document_and_a_consistent_status(cpuc_admin):
    with pytest.raises(MandateError, match="ca.gov"):
        mandates.create_draft(cpuc_admin, **{**ENTRY, "document_url": "https://example.com/d.pdf"})
    with pytest.raises(MandateError, match="ca.gov"):
        mandates.create_draft(cpuc_admin, **{**ENTRY, "document_url": "http://docs.cpuc.ca.gov/d.pdf"})
    with pytest.raises(MandateError, match="decision or resolution"):
        mandates.create_draft(cpuc_admin, **{**ENTRY, "reference": " "})
    m = mandates.create_draft(cpuc_admin, **ENTRY)
    with pytest.raises(MandateError, match="date it was implemented"):
        mandates.update(cpuc_admin, m, status="implemented")
    with pytest.raises(MandateError, match="Implemented status"):
        mandates.update(cpuc_admin, m, implemented_on=date(2026, 2, 1))
    with pytest.raises(MandateError, match="why it is superseded"):
        mandates.update(cpuc_admin, m, status="superseded")
    mandates.update(cpuc_admin, m, status="implemented", implemented_on=date(2026, 2, 1))
    mandates.update(cpuc_admin, m, status="in_progress")
    m.refresh_from_db()
    assert m.implemented_on is None  # off Implemented, the date goes


def test_publishing_changes_and_history(cpuc_admin, entry):
    mandates.publish(cpuc_admin, entry)
    entry.refresh_from_db()
    assert entry.published_at and list(mandates.published()) == [entry]
    with pytest.raises(MandateError, match="Say why"):
        mandates.update(cpuc_admin, entry, status="in_progress")
    mandates.update(cpuc_admin, entry, status="in_progress", status_note="Built, not yet released.", reason="Work started")
    events = AuditEvent.objects.filter(object_type="releases.mandate", object_id=str(entry.pk))
    assert set(events.values_list("action", flat=True)) == {"mandate.drafted", "mandate.published", "mandate.changed"}
    changed = events.get(action="mandate.changed")
    assert changed.reason == "Work started" and changed.before["status"] == "not_started" and changed.after["status"] == "in_progress"
    h = mandates.history(entry)
    assert [x["what"] for x in h] == ["Published on the register", "Changed"]
    assert ("Implementation status", "Not started", "In progress") in h[1]["rows"] and h[1]["reason"] == "Work started"
    with pytest.raises(MandateError, match="never deleted"):
        mandates.discard_draft(cpuc_admin, entry)


def test_a_published_entry_is_never_deleted(cpuc_admin, entry):
    mandates.publish(cpuc_admin, entry)
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        Mandate.objects.filter(pk=entry.pk).delete()
    draft = mandates.create_draft(cpuc_admin, **{**ENTRY, "reference": "D.99-01-001"})
    mandates.discard_draft(cpuc_admin, draft)
    assert not Mandate.objects.filter(pk=draft.pk).exists()


def test_links_to_published_records(cpuc_admin, entry):
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    draft_rules = RuleSet.objects.create(code="claims", label="Claims 2027", effective_from=date(2027, 1, 1))
    with pytest.raises(MandateError, match="no published rule set"):
        mandates.add_link(cpuc_admin, entry, "rule_set", str(draft_rules.pk))
    rules = RuleSet.objects.create(code="claims", label="Claims 2026", effective_from=date(2026, 3, 12))
    RuleSet.objects.filter(pk=rules.pk).update(status="published")
    link = mandates.add_link(cpuc_admin, entry, "rule_set", f"https://cedars.example.org/rules/{rules.pk}/?severity=reject")
    assert link.key == str(rules.pk) and link.label.startswith("Rule set: Claims 2026")
    assert mandates.add_link(cpuc_admin, entry, "field", "E3ClimateZone").key == "AC_Climate_Zone"  # the canonical name
    with pytest.raises(MandateError, match="already links"):
        mandates.add_link(cpuc_admin, entry, "field", "AC_Climate_Zone")
    with pytest.raises(MandateError, match="no field"):
        mandates.add_link(cpuc_admin, entry, "field", "NotAField")
    with pytest.raises(MandateError, match="number"):
        mandates.add_link(cpuc_admin, entry, "catalog", "latest")
    version = dictionary.current()
    mandates.add_link(cpuc_admin, entry, "catalog", str(version.pk))
    assert mandates.link_target(link) == f"/rules/{rules.pk}/"
    mandates.publish(cpuc_admin, entry)
    assert mandates.for_record("catalog", version.pk) == [entry]
    assert mandates.requiring_field("e3climatezone") == [entry]
    with pytest.raises(MandateError, match="Say why"):
        mandates.remove_link(cpuc_admin, link)
    mandates.remove_link(cpuc_admin, link, reason="Linked the wrong set")
    assert [x["what"] for x in mandates.history(entry)][-1] == "Link removed"
    mandates.update(cpuc_admin, entry, status="withdrawn", status_note="Entered in error.", reason="Wrong resolution")
    assert mandates.requiring_field("AC_Climate_Zone") == []


def test_the_register_csv(cpuc_admin, entry):
    mandates.publish(cpuc_admin, entry)
    mandates.create_draft(cpuc_admin, **{**ENTRY, "reference": "D.99-01-001"})  # a draft is not on the register
    rows = list(csv.DictReader(io.StringIO(mandates.register_csv(mandates.published()))))
    assert [r["reference"] for r in rows] == ["Res. E-5350"] and rows[0]["status"] == "Not started"


def test_the_pages(client, signed_in, cpuc_admin, pge_admin, entry):
    assert client.get(f"/mandates/{entry.pk}/").status_code == 404  # a draft is not public
    assert "Res. E-5350" not in page_only(client.get("/mandates/").content.decode())
    mandates.publish(cpuc_admin, entry)
    r = client.get("/mandates/")
    assert r.status_code == 200 and "Res. E-5350" in r.content.decode() and "Keep the register" not in page_only(r.content.decode())
    assert client.get("/mandates/?status=implemented").content.decode().count("Res. E-5350") == 0
    r = client.get("/mandates/?format=csv")
    assert r["Content-Type"].startswith("text/csv") and b"Res. E-5350" in r.content
    r = client.get(f"/mandates/{entry.pk}/")
    assert r.status_code == 200 and "Published on the register" in r.content.decode()
    assert cpuc_admin.email not in r.content.decode()  # the history never says who
    assert client.get("/mandates/manage/").status_code == 302
    signed_in(pge_admin)
    assert client.get("/mandates/manage/").status_code == 403
    signed_in(cpuc_admin)
    data = {**ENTRY, "reference": "D.26-06-001", "authority": "decision", "area": "calculation", "status": "not_started"}
    data["effective_on"] = (timezone.localdate() + timedelta(days=60)).isoformat()
    r = client.post("/mandates/manage/", data)
    new = Mandate.objects.get(reference="D.26-06-001")
    assert r.status_code == 302 and r["Location"] == f"/mandates/manage/{new.pk}/"
    assert client.get(f"/mandates/manage/{new.pk}/").status_code == 200
    r = client.post(f"/mandates/manage/{new.pk}/", {"action": "publish"})
    assert r.status_code == 302 and Mandate.objects.get(pk=new.pk).published_at
    edit = {**data, "action": "save", "status": "in_progress", "reason": ""}
    r = client.post(f"/mandates/manage/{new.pk}/", edit)
    assert r.status_code == 200 and "Say why" in r.content.decode()
    r = client.post(f"/mandates/manage/{new.pk}/", {**edit, "reason": "Started"})
    assert r.status_code == 302 and Mandate.objects.get(pk=new.pk).status == "in_progress"
    r = client.post(f"/mandates/manage/{new.pk}/", {"action": "link", "kind": "release_note", "key": "999"})
    assert r.status_code == 200 and "Say why the link is added" in r.content.decode()
    r = client.post(f"/mandates/manage/{new.pk}/", {"action": "link", "kind": "release_note", "key": "999", "reason": "Announced"})
    assert r.status_code == 200 and "no published release note" in r.content.decode()
