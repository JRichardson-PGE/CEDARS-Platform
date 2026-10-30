import json
from pathlib import Path

import pytest
from django.test import Client

from cedars.modules.calculations import dispatch, output_templates, service
from cedars.modules.calculations.models import OutputTemplate
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import service as dictionary

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.79.json"


@pytest.fixture
def catalog():
    return dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))


@pytest.fixture
def web(analyst):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    return c


def test_the_shipped_templates_are_offered_to_everyone(analyst, catalog):  # noqa: F811
    names = [t.name for t in output_templates.visible(analyst)]
    assert names == ["Key results", "Participant costs"]
    key = OutputTemplate.objects.get(name="Key results")
    assert key.shared and len(key.columns) == 25
    ok, bad = output_templates.check(key.columns)
    assert ok == key.columns and not bad  # every shipped column is in the published catalog


def test_columns_are_checked_against_the_catalog_under_their_canonical_names(catalog):
    ok, bad = output_templates.check(
        ["E3ClimateZone", "POC_ACC", "trcratio", "ElecValue_Energy", "TRCRatio", "NotAColumn", "AdminCostsOverhead"]
    )
    assert ok == ["AC_Climate_Zone", "Used_ACCVersion", "TRCRatio", "ElecValue_Energy"]  # CET / old names, case, repeats
    assert bad == ["NotAColumn", "AdminCostsOverhead"]  # a ProgramCost input is not a measure-level column
    groups = [g for g, _ in output_templates.catalog().groups]
    assert groups[0] == output_templates.INPUT_GROUP and "Benefit-cost ratios" in groups and "Program metadata" in groups


def test_saving_changing_and_deleting_a_template(analyst, make_user, cpuc_admin, catalog):  # noqa: F811
    t = output_templates.save(analyst, "Mine", ["CEInputID", "E3ClimateZone", "TRCRatio"])
    assert t.owner == analyst and t.columns == ["CEInputID", "AC_Climate_Zone", "TRCRatio"]
    with pytest.raises(AccessDenied, match="already a template"):
        output_templates.save(analyst, "mine", ["CEInputID"])
    with pytest.raises(AccessDenied, match="Not measure-level columns: Nope"):
        output_templates.save(analyst, "Other", ["CEInputID", "Nope"])
    with pytest.raises(AccessDenied, match="at least one column"):
        output_templates.save(analyst, "Other", [])

    other = make_user("other@pge.com")
    assert t not in output_templates.visible(other)
    with pytest.raises(AccessDenied, match="someone else's"):
        output_templates.save(other, "Mine", ["CEInputID"], template=t)

    key = OutputTemplate.objects.get(name="Key results")
    with pytest.raises(AccessDenied, match="save your copy"):
        output_templates.save(analyst, "Key results", ["CEInputID"], template=key)
    with pytest.raises(AccessDenied, match="Only a CPUC system administrator"):
        output_templates.save(analyst, "For all", ["CEInputID"], shared=True)
    shared = output_templates.save(cpuc_admin, "For all", ["CEInputID", "TotalSystemBenefit"], shared=True)
    assert shared.shared and shared in output_templates.visible(other)
    output_templates.save(cpuc_admin, "Key results", key.columns[:5], template=key)

    output_templates.save(analyst, "Mine, renamed", ["TRCRatio", "CEInputID"], template=t)
    output_templates.delete(analyst, t)
    actions = list(AuditEvent.objects.filter(action__startswith="output_template.").values_list("action", flat=True).order_by("pk"))
    assert [a.removeprefix("output_template.") for a in actions] == ["added", "added", "changed", "changed", "deleted"]
    changed = AuditEvent.objects.filter(action="output_template.changed").order_by("pk").last()
    assert changed.before["columns"] == ["CEInputID", "AC_Climate_Zone", "TRCRatio"] and changed.after["name"] == "Mine, renamed"


def test_a_run_carries_its_template_and_new_run_starts_with_the_default(
    analyst,  # noqa: F811
    pge,
    release,  # noqa: F811
    worker,  # noqa: F811
    tmp_path,
    web,
    catalog,
):
    key = OutputTemplate.objects.get(name="Key results")
    output_templates.set_default(analyst, key)
    assert output_templates.default_for(analyst) == key
    page = web.get("/calculations/new/").content.decode()
    assert f'<option value="{key.pk}" selected>Key results</option>' in page

    run = service.create_run(analyst, pge, request(release, settings=output_templates.settings_for(key)), job_zip(tmp_path))
    assert run.settings["output_template"] == key.columns and run.settings["output_template_name"] == "Key results"
    assert run.spec["settings"]["output_template"] == key.columns
    try:
        from recet import jobspec
    except ImportError:
        pass
    else:
        assert jobspec.validate(run.spec) == []  # the engine takes the template as a run setting
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    page = web.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "Key results (25 columns)" in page and "CEInputID, PrgID" not in page

    output_templates.set_default(analyst, None)
    assert output_templates.default_for(analyst) is None
    assert AuditEvent.objects.filter(action="output_template.default").count() == 2


def test_the_templates_pages(analyst, web, catalog):  # noqa: F811
    page = web.get("/calculations/templates/")
    assert page.status_code == 200 and "Key results" in page.content.decode()

    r = web.post(
        "/calculations/templates/new/",
        {"name": "Ratios", "columns": "CEInputID\nE3ClimateZone", "add": ["TRCRatio", "PACRatio", "CEInputID"]},
    )
    t = OutputTemplate.objects.get(name="Ratios")
    assert r.status_code == 302 and r["Location"] == f"/calculations/templates/{t.pk}/"
    assert t.columns == ["CEInputID", "AC_Climate_Zone", "TRCRatio", "PACRatio"]  # the text's order, then the ticked ones

    page = web.get(f"/calculations/templates/{t.pk}/").content.decode()
    assert "4 columns in the template" in page and "(in the template)" in page

    r = web.post(f"/calculations/templates/{t.pk}/", {"name": "Ratios", "columns": "CEInputID\nNope"})
    assert r.status_code == 200 and "Not measure-level columns: Nope" in r.content.decode()

    key = OutputTemplate.objects.get(name="Key results")
    r = web.get(f"/calculations/templates/{key.pk}/")  # a template offered to everyone opens as a copy
    assert r.status_code == 302 and r["Location"] == f"/calculations/templates/new/?from={key.pk}"
    page = web.get(r["Location"]).content.decode()
    assert "Copy of “Key results”" in page and "TotalSystemBenefit" in page

    r = web.post("/calculations/templates/", {"template": str(t.pk)})
    assert r.status_code == 302 and output_templates.default_for(analyst) == t
    r = web.post(f"/calculations/templates/{t.pk}/", {"action": "delete"})
    assert not OutputTemplate.objects.filter(name="Ratios").exists() and output_templates.default_for(analyst) is None


def test_without_a_catalog_the_page_says_so(web):
    page = web.get("/calculations/templates/new/").content.decode()
    assert "No field catalog is published yet" in page
