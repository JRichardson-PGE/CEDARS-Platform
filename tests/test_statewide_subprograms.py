"""Statewide programs as the source of truth (Jake, 2026-11-06): each PA's ID of a statewide program (PGE_SW_X for SW_X) follows it.
The statewide program's record is filled once from its lead PA's ID, every PA's ID then takes the statewide program's attributes, nobody
changes them but through the statewide program, all of them show the same documents, and the catalog opens the statewide program's page.
Every program page has an Add a document button."""

from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from cedars.modules.documents.models import AttributeOption, Document
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program, ProgramVersion

from .conftest import create_user, page_only, sign_in

pytestmark = pytest.mark.django_db
START = date(2020, 1, 1)


@pytest.fixture
def sce(cpuc_admin):
    return access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")


@pytest.fixture
def sce_admin(cpuc_admin, sce):
    user = create_user("admin@sce.example.org", "Sam Admin")
    access.grant(cpuc_admin, user, sce, perms.ORG_ADMIN, reason="first admin")
    return user


@pytest.fixture
def scene(cpuc_admin, pge, sce):
    """SW_X as CEDARS has it (mostly blank), led by PG&E; PG&E's and SCE's IDs of it with their own attributes; PG&E's own costs ID
    of it (not an ID of it); another statewide program with no lead."""
    sw = registry.create_program(cpuc_admin, prg_id="SW_X", administrator=None, statewide=True, valid_from=START, name="COM-SW-X")
    registry.set_lead(cpuc_admin, sw, pge, "PG&E leads SW_X")
    pge_x = registry.create_program(
        cpuc_admin,
        prg_id="PGE_SW_X",
        administrator=pge,
        valid_from=START,
        name="Statewide X",
        status="active",
        segment="resource_acquisition",
        primary_sector="Commercial",
        implementer="Third-Party Solicited",
        statewide_program="SW_X - Statewide X",
    )
    sce_x = registry.create_program(cpuc_admin, prg_id="SCE_SW_X", administrator=sce, valid_from=START, name="SCE X", status="new")
    costs = registry.create_program(cpuc_admin, prg_id="PGE_SW_X_PA", administrator=pge, valid_from=START, name="Statewide X - PGE Costs")
    registry.create_program(cpuc_admin, prg_id="SW_Y", administrator=None, statewide=True, valid_from=START, name="SW Y")
    registry.create_program(cpuc_admin, prg_id="PGE_SW_Y", administrator=pge, valid_from=START, name="Y")
    return {"sw": sw, "pge_x": pge_x, "sce_x": sce_x, "costs": costs}


def _follow(scene, effective=None):
    return registry.follow_statewide(None, scene["sw"], effective=effective, reason="SW programs are the source of truth", apply=True)


def _v(p):
    return registry.current_version(Program.objects.get(pk=p.pk))


def test_ids_are_matched_by_the_pa_code_in_front(scene):
    assert registry.statewide_part_of(scene["pge_x"]) == scene["sw"]
    assert registry.statewide_part_of(scene["costs"]) is None  # PGE_SW_X_PA is PG&E's own costs, not an ID of SW_X
    assert [p.prg_id for p in registry.allocated_ids(scene["sw"])] == ["PGE_SW_X", "SCE_SW_X"]
    assert registry.lead_id(scene["sw"]) == scene["pge_x"]


def test_a_dry_run_changes_nothing(scene):
    r = registry.follow_statewide(None, scene["sw"], reason="check", apply=False)
    assert r.filled["segment"] == ("", "resource_acquisition") and r.filled["name"] == ("COM-SW-X", "Statewide X")
    assert [x[0] for x in r.linked] == ["PGE_SW_X", "SCE_SW_X"]
    assert dict(r.linked[1][2])["status"] == ("new", "active")
    assert not Program.objects.filter(follows__isnull=False).exists()
    assert scene["sw"].versions.count() == 1


def test_the_statewide_program_is_filled_from_its_lead_and_its_ids_follow_it(scene):
    today = timezone.localdate()
    r = _follow(scene)
    assert r.source == scene["pge_x"] and r.filled_from == today
    sw = _v(scene["sw"])
    assert (sw.name, sw.segment, sw.primary_sector, sw.status) == ("Statewide X", "resource_acquisition", "Commercial", "active")
    assert sw.statewide_program == ""  # its own; not the lead ID's pointer to it
    for key, starts in (("pge_x", START), ("sce_x", today)):  # the lead's ID had the attributes already: no new version
        p = Program.objects.get(pk=scene[key].pk)
        assert p.follows == scene["sw"] and p.follows_from == today
        v = _v(p)
        assert (v.name, v.segment, v.status, v.valid_from) == ("Statewide X", "resource_acquisition", "active", starts)
        assert v.statewide_program == "SW_X - Statewide X"
    sce_first = Program.objects.get(pk=scene["sce_x"].pk).versions.order_by("valid_from").first()
    assert sce_first.name == "SCE X" and sce_first.valid_to == today - timedelta(days=1)  # its own history before it followed is kept
    assert Program.objects.get(pk=scene["costs"].pk).follows is None
    assert AuditEvent.objects.filter(action="program.follows_statewide").count() == 2
    # running it again does nothing more
    again = _follow(scene)
    assert again.linked == [] and set(again.already) == {"PGE_SW_X", "SCE_SW_X"}


def test_a_statewide_program_with_no_lead_is_left_alone(scene):
    sw_y = Program.objects.get(prg_id="SW_Y")
    r = registry.follow_statewide(None, sw_y, reason="r", apply=True)
    assert "no lead" in r.skipped
    assert Program.objects.get(prg_id="PGE_SW_Y").follows is None


def test_nobody_changes_an_id_that_follows(scene, pge_admin, cpuc_admin):
    _follow(scene)
    p = Program.objects.get(pk=scene["pge_x"].pk)
    assert not registry.can_edit(pge_admin, p) and not registry.can_edit(cpuc_admin, p)
    with pytest.raises(AccessDenied, match="follows the statewide program SW_X"):
        registry.change_program(cpuc_admin, p, effective=date(2030, 1, 1), reason="r", name="mine")


def test_the_lead_changes_the_statewide_program_and_the_change_reaches_every_id(scene, pge_admin, sce_admin):
    _follow(scene)
    sw = Program.objects.get(pk=scene["sw"].pk)
    assert registry.can_edit(pge_admin, sw) and not registry.can_edit(sce_admin, sw)
    when = timezone.localdate() + timedelta(days=30)
    registry.change_program(pge_admin, sw, effective=when, reason="new contractor", implementation_contractor="Acme")
    for key in ("pge_x", "sce_x"):
        p = Program.objects.get(pk=scene[key].pk)
        v = registry.version_on(p, when)
        assert v.implementation_contractor == "Acme" and v.valid_from == when and "follows SW_X: new contractor" in v.reason
    assert AuditEvent.objects.filter(action="program.changed", reason__startswith="follows SW_X").count() >= 2


def test_a_new_id_of_a_followed_program_follows_it_from_the_start(scene, cpuc_admin):
    _follow(scene)
    sdge = access.create_organisation(cpuc_admin, code="SDGE", name="San Diego Gas & Electric", kind="pa", pa_type="IOU")
    p = registry.create_program(cpuc_admin, prg_id="SDGE_SW_X", administrator=sdge, valid_from=date(2030, 1, 1), name="whatever")
    assert p.follows == scene["sw"] and p.follows_from == date(2030, 1, 1)
    v = p.versions.get()
    assert (v.name, v.segment) == ("Statewide X", "resource_acquisition")


def test_every_id_shows_the_same_documents_and_the_catalog_opens_the_statewide_page(client, scene, pge):
    _follow(scene)
    kind = AttributeOption.objects.filter(attribute_list__collections__contains=["program"]).first() or AttributeOption.objects.first()
    for title, prg in (("SW plan", scene["sw"]), ("Old PG&E X plan", scene["pge_x"])):
        doc = Document.objects.create(collection="program", document_type=kind, title=title, owner=pge, published_at=timezone.now())
        doc.programs.add(prg)
    for prg_id in ("SW_X", "PGE_SW_X", "SCE_SW_X"):
        html = page_only(client.get(f"/programs/{prg_id}/").content.decode())
        assert "SW plan" in html and "Old PG&amp;E X plan" in html, prg_id
    sce_page = client.get("/programs/SCE_SW_X/").content.decode()
    assert "part of the statewide program" in sce_page
    sw_page = client.get("/programs/SW_X/").content.decode()
    assert "Each PA&#x27;s ID of this program" in sw_page or "Each PA's ID of this program" in sw_page
    catalog = client.get("/programs/").content.decode()
    assert '<a class="id" href="/programs/SW_X/">PGE_SW_X</a>' in catalog
    assert '<a class="id" href="/programs/PGE_SW_X_PA/">PGE_SW_X_PA</a>' in catalog


def test_the_change_page_of_an_id_that_follows_sends_you_to_the_statewide_program(client, scene, cpuc_admin):
    _follow(scene)
    sign_in(client, cpuc_admin)
    r = client.get("/programs/PGE_SW_X/change")
    assert r.status_code == 302 and r["Location"] == "/programs/SW_X/"


def test_every_program_page_has_an_add_a_document_button(client, scene, pge, pge_admin, cpuc_admin):
    _follow(scene)
    html = page_only(client.get("/programs/PGE_SW_X_PA/").content.decode())
    assert "Sign in to add a document" in html
    sign_in(client, pge_admin)
    assert 'id="add-document"' not in page_only(client.get("/programs/PGE_SW_X_PA/").content.decode())  # files no documents
    access.grant(None, pge_admin, pge, "documents.submit", reason="files documents")
    html = client.get("/programs/PGE_SW_X_PA/").content.decode()
    assert 'id="add-document" href="/documents/new/?collection=program&amp;program=PGE_SW_X_PA"' in html
    html = client.get("/programs/SCE_SW_X/").content.decode()  # an ID that follows: the document is about the statewide program
    assert "program=SW_X" in html
    form = client.get("/documents/new/?collection=program&program=SW_X").content.decode()
    assert "Add a document to SW_X" in form and 'value="SW_X"' in form


def test_the_command_says_what_it_would_do_then_does_it(scene):
    out = StringIO()
    call_command("follow_statewide", "--reason", "SW is the source of truth", stdout=out)
    text = out.getvalue()
    assert "filled from PGE_SW_X" in text and "SCE_SW_X follows it" in text and "skipped SW_Y: no lead" in text
    assert "would fill 1 statewide program and would link 2 IDs" in text
    assert not Program.objects.filter(follows__isnull=False).exists()
    call_command("follow_statewide", "--reason", "SW is the source of truth", "--apply", stdout=StringIO())
    assert Program.objects.filter(follows__prg_id="SW_X").count() == 2


@pytest.fixture
def legacy(cpuc_admin, pge, sce):
    """The shape the CEDARS loads leave (the demo, 2026-11-09): the statewide record ends with its last legacy year, each PA's ID has
    its legacy years and then its filed years (2028-2031) after a gap, and one PA's ID starts only with its filing."""
    sdge = access.create_organisation(cpuc_admin, code="SDGE", name="San Diego Gas & Electric", kind="pa", pa_type="IOU")
    sw = registry.create_program(cpuc_admin, prg_id="SW_Z", administrator=None, statewide=True, valid_from=START, name="SW Z")
    registry.set_lead(cpuc_admin, sw, pge, "PG&E leads SW_Z")
    ids = {}
    for org, years in ((pge, (START, date(2028, 1, 1))), (sce, (START, date(2028, 1, 1))), (sdge, (date(2028, 1, 1),))):
        p = registry.create_program(cpuc_admin, prg_id=f"{org.code}_SW_Z", administrator=org, valid_from=years[0], name=f"{org.code} Z")
        if len(years) == 2:
            p.versions.update(valid_to=date(2026, 12, 31))
            ProgramVersion.objects.create(program=p, valid_from=years[1], valid_to=date(2031, 12, 31), name=f"{org.code} Z filed")
        ids[org.code] = p
    ProgramVersion.objects.filter(program__prg_id__in=["SW_Z", "PGE_SW_Z", "SCE_SW_Z"], valid_from=START).update(
        valid_to=date(2026, 12, 31)
    )
    ProgramVersion.objects.filter(program=ids["SDGE"]).update(valid_to=date(2031, 12, 31))
    return {"sw": sw, **ids}


def _spans(p):
    return [(v.valid_from, v.valid_to, v.name) for v in Program.objects.get(pk=p.pk).versions.order_by("valid_from")]


def test_ids_keep_their_own_dates_when_the_records_are_closed_years(legacy, pge_admin):
    day = date(2026, 11, 9)
    cut = day - timedelta(days=1)
    dry = registry.follow_statewide(None, legacy["sw"], effective=day, reason="r", apply=False)
    r = registry.follow_statewide(None, legacy["sw"], effective=day, reason="r", apply=True)
    assert (dry.filled, dry.filled_from, dry.linked) == (r.filled, r.filled_from, r.linked)  # the dry run is the run, rolled back
    # the statewide record now reaches its lead's filed years too
    assert _spans(legacy["sw"]) == [
        (START, cut, "SW Z"),
        (day, date(2026, 12, 31), "PGE Z"),
        (date(2028, 1, 1), date(2031, 12, 31), "PGE Z filed"),
    ]
    assert _spans(legacy["SCE"]) == [
        (START, cut, "SCE Z"),
        (day, date(2026, 12, 31), "PGE Z"),
        (date(2028, 1, 1), date(2031, 12, 31), "PGE Z filed"),
    ]
    assert _spans(legacy["SDGE"]) == [(date(2028, 1, 1), date(2031, 12, 31), "PGE Z filed")]  # starts with its filing still
    assert registry.version_on(Program.objects.get(pk=legacy["SDGE"].pk), date(2029, 1, 1)).statewide_program == "SW_Z - PGE Z filed"
    # a later change to the statewide program reaches the IDs' filed years from its date on
    sw = Program.objects.get(pk=legacy["sw"].pk)
    registry.change_program(pge_admin, sw, effective=date(2029, 1, 1), reason="new contractor", implementation_contractor="Acme")
    for key in ("PGE", "SCE", "SDGE"):
        p = Program.objects.get(pk=legacy[key].pk)
        assert registry.version_on(p, date(2028, 6, 1)).implementation_contractor == ""
        assert registry.version_on(p, date(2030, 6, 1)).implementation_contractor == "Acme"
        assert registry.version_on(p, date(2027, 6, 1)) is None  # the gap stays a gap
    again = registry.follow_statewide(None, Program.objects.get(pk=legacy["sw"].pk), effective=day, reason="r", apply=True)
    assert again.linked == [] and set(again.already) == {"PGE_SW_Z", "SCE_SW_Z", "SDGE_SW_Z"}


def test_a_dry_run_on_legacy_records_leaves_them_as_they_were(legacy):
    before = {k: _spans(p) for k, p in legacy.items()}
    r = registry.follow_statewide(None, legacy["sw"], effective=date(2026, 11, 9), reason="r", apply=False)
    assert [x[0] for x in r.linked] == ["PGE_SW_Z", "SCE_SW_Z", "SDGE_SW_Z"]
    assert {k: _spans(p) for k, p in legacy.items()} == before
    assert not Program.objects.filter(follows__isnull=False).exists()


def test_the_command_goes_on_past_a_statewide_program_it_cannot_link(scene, legacy, monkeypatch):
    real = registry.follow_statewide

    def fails_on_sw_x(actor, sw, **kw):
        if sw.prg_id == "SW_X":
            raise AccessDenied("SW_X has a version a change request started")
        return real(actor, sw, **kw)

    monkeypatch.setattr(registry, "follow_statewide", fails_on_sw_x)
    out, err = StringIO(), StringIO()
    call_command("follow_statewide", "--reason", "r", "--apply", stdout=out, stderr=err)
    assert "failed SW_X: SW_X has a version a change request started; nothing changed for it" in err.getvalue()
    assert "failed 1" in out.getvalue()
    assert Program.objects.filter(follows__prg_id="SW_Z").count() == 3
