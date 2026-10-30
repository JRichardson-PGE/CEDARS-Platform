"""WCAG 2.2 AA on every page, in both colour schemes, at full width and at 320 px (reflow, 1.4.10).

Run with `pytest -m a11y` (CI does). axe finds what can be found automatically; keyboard and screen-reader checks are by hand
(docs/ACCESSIBILITY.md). Each page is checked with realistic content: SCENE builds organisations, members, an access request, a pending
PII grant and an open recertification, so the tables and forms are not empty."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.registry import service as registry

from .pages import PAGES

pytestmark = [pytest.mark.a11y, pytest.mark.django_db(transaction=True)]

TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


@pytest.fixture
def impact_profiles(cpuc_admin, pge, pge_admin, make_user, settings, tmp_path):
    """The Impact profiles pages' content: a synced package, an accepted and a rejected upload, generated and custom profiles, a mapping
    and a library write."""
    import numpy as np
    from django.core.files.base import ContentFile

    from cedars.modules.impact_profiles import custom, etrm, generation, jobs, library, loadshapes
    from cedars.modules.impact_profiles.models import ImpactProfile, LoadShape, MeasurePackage

    from ..test_impact_profiles import FIXTURE, leds_series, load_shape_csv, profile_file

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.IMPACT_PROFILES_INLINE_JOBS = False
    dev = make_user("developer@pge.com", "Dee Veloper")
    service.grant(pge_admin, dev, pge, "impact_profiles.submit")
    etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE))
    leds = MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")
    good = loadshapes.create_upload(dev, pge, leds, ContentFile(load_shape_csv(leds_series()), name="shapes.csv"))
    loadshapes.create_upload(dev, pge, leds, ContentFile(load_shape_csv([({"TechID": "Msr-X"}, np.full(8760, 1 / 8000))]), name="bad.csv"))
    jobs.run_pending()
    generation.generate(dev, leds, good)
    target = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    custom.create_mapping(
        dev, pge, leds, alias_key="OfL*New*rDXGF*Msr-LED*Std-Fluor", target_sector="Com", target=target, reason="NC reuses Ex"
    )
    mine = custom.create_custom(
        dev, pge, profile_key="PGE Office NMEC", target_sector="Commercial", territory="PGE", data=profile_file(), file_name="p.csv"
    )
    library.export(cpuc_admin)
    return {
        "developer": dev,
        "package": leds.measure_version_id,
        "upload": good.pk,
        "rejected": good.pk + 1,
        "profile": target.pk,
        "custom_profile": mine.pk,
        "load_shape": LoadShape.objects.first().pk,
    }


@pytest.fixture
def scene(cpuc_admin, cpuc, pge, pge_admin, make_user, impact_profiles):
    service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    analyst = make_user("analyst@pge.com", "Ana Lyst")
    service.grant(pge_admin, analyst, pge, "claims.submit", expires_at=timezone.now() + timedelta(days=90))
    service.grant(pge_admin, analyst, pge, perms.PII, reason="files claims with contacts")
    community = make_user("community@example.com")
    service.request_access(community, pge, ["claims.read", "budgets.read"], "I work on PG&E's filings")
    service.open_recertification(pge_admin, pge, timezone.now() + timedelta(days=30))
    factors = (
        (Path(__file__).parent.parent / "data" / "statewide_factors_20260918.csv")
        .read_bytes()
        .replace(b"0.4440,0.1550", b"0.4000,0.1550", 1)
    )
    allocation = registry.import_allocation_csv(cpuc_admin, factors, label="2028 TUAL draft", purpose="filing")  # with a problem to show
    return {
        "cpuc_admin": cpuc_admin,
        "pge_admin": pge_admin,
        "community": community,
        "password": community,
        "pge": pge,
        "cpuc": cpuc,
        "allocation": allocation,
        **impact_profiles,
    }


def _session_cookie(user, method, live_server):
    s = SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    records = [{"method": "password", "at": 0}] + ([{"method": method, "at": 0}] if method != "password" else [])
    s["account_authentication_methods"] = records
    s["account_authentication_methods_user"] = str(user.pk)
    s.create()
    return {"name": settings.SESSION_COOKIE_NAME, "value": s.session_key, "url": live_server.url}


def _open(browser, live_server, scene, path, who, **context_args):
    context = browser.new_context(**context_args)
    if who is not None:
        context.add_cookies([_session_cookie(scene[who], "password" if who == "password" else "mfa", live_server)])
    page = context.new_page()
    ids = {k: (v.code if hasattr(v, "code") else getattr(v, "pk", v)) for k, v in scene.items()}
    response = page.goto(live_server.url + path.format(**ids))
    assert response.status == 200, f"{path} answered {response.status} at {page.url}"
    return context, page


def _violations(page, axe_source):
    page.add_script_tag(content=axe_source)
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # check what is folded away too
    result = page.evaluate("async (tags) => await axe.run(document, {runOnly: {type: 'tag', values: tags}})", TAGS)
    return [
        {"id": v["id"], "impact": v["impact"], "help": v["help"], "targets": [n["target"] for n in v["nodes"]][:5]}
        for v in result["violations"]
    ]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_axe_clean(browser, axe_source, live_server, scene, name, path, who, scheme):
    # axe is injected inline, so this context bypasses the page's CSP; nothing else does
    context, page = _open(browser, live_server, scene, path, who, color_scheme=scheme, bypass_csp=True)
    found = _violations(page, axe_source)
    context.close()
    assert not found, f"{name} ({scheme}): " + json.dumps(found, indent=1)


@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_reflows_at_320px(browser, live_server, scene, name, path, who):
    context, page = _open(browser, live_server, scene, path, who, viewport={"width": 320, "height": 640})
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    context.close()
    assert overflow <= 0, f"{name} scrolls sideways by {overflow} px at 320 px wide"
