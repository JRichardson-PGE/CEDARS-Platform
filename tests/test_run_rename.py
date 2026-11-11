"""A run's owner renames it or adds a note; nothing else about the run changes."""

import pytest

from cedars.modules.calculations import service
from cedars.modules.calculations.models import Run
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

from .test_calculations import analyst, job_zip, release, request  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def test_the_owner_renames_a_run_and_adds_a_note(tmp_path, analyst, pge, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    before = (run.input_hash, run.settings, run.inputs)
    service.rename_run(analyst, run, name="  2028   PG&E  filing ", note="Second try, with the new NTGs.")
    run = Run.objects.get(pk=run.pk)
    assert (run.name, run.note) == ("2028 PG&E filing", "Second try, with the new NTGs.")
    assert (run.input_hash, run.settings, run.inputs) == before
    e = AuditEvent.objects.get(action="calculation_run.renamed")
    assert e.after == {"name": "2028 PG&E filing", "note": "Second try, with the new NTGs."} and e.organisation == pge
    with pytest.raises(AccessDenied, match="Nothing changed"):
        service.rename_run(analyst, run, name="2028 PG&E filing", note="Second try, with the new NTGs.")
    with pytest.raises(AccessDenied, match="Give the run a name"):
        service.rename_run(analyst, run, name=" ", note="")


def test_only_the_owner_renames(tmp_path, analyst, pge, pge_admin, make_user, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    colleague = make_user("colleague@pge.example.org")
    access.grant(pge_admin, colleague, pge, "calculations.submit")
    with pytest.raises(AccessDenied, match="Only the person who started"):
        service.rename_run(colleague, run, name="mine now", note="")


def test_the_run_page(tmp_path, signed_in, analyst, pge, pge_admin, make_user, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    c = signed_in(analyst)
    assert "Rename or add a note" in c.get(f"/calculations/runs/{run.pk}/").content.decode()
    r = c.post(f"/calculations/runs/{run.pk}/rename", {"name": "Renamed", "note": "Line one\nline two"})
    assert r.status_code == 302
    page = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "<h1>Renamed</h1>" in page and "Line one<br>line two" in page and "Rename or change the note" in page
    reader = make_user("reader@pge.example.org")
    access.grant(pge_admin, reader, pge, "calculations.read")
    other = signed_in(reader)
    page = other.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "Line one" in page and "Rename or change the note" not in page
    other.post(f"/calculations/runs/{run.pk}/rename", {"name": "Hijacked", "note": ""})
    assert Run.objects.get(pk=run.pk).name == "Renamed"
