"""WCAG 2.2 AA on every page, in both colour schemes, at full width and at 320 px (reflow, 1.4.10).

Run with `pytest -m a11y` (CI does). axe finds what can be found automatically; keyboard and screen-reader checks are by hand
(docs/ACCESSIBILITY.md). Each page is checked with realistic content: SCENE builds organisations, members, an access request, a pending
PII grant and an open recertification, so the tables and forms are not empty."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.dictionary import service as dictionary
from cedars.services.refdata import service as refdata
from cedars.services.registry import service as registry
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.submissions import service as submissions

from .pages import PAGES

pytestmark = [pytest.mark.a11y, pytest.mark.django_db(transaction=True)]

TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


@pytest.fixture
def scene(cpuc_admin, cpuc, pge, pge_admin, make_user):
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
    dictionary.load_catalog(
        None, json.loads((Path(__file__).parent.parent.parent / "data" / "dictionary" / "recet-0.1.75.json").read_text())
    )
    allocation = registry.import_allocation_csv(cpuc_admin, factors, label="2028 TUAL draft", purpose="filing")  # with a problem to show
    rules = RuleSet.objects.create(
        code="claims", label="Quarterly claims", effective_from=date(2026, 3, 12), status="published", source="test"
    )
    Rule.objects.create(
        rule_set=rules,
        rule_id="S1a2b3c",
        severity="reject",
        table="Measure",
        field="NumUnits",
        message="NumUnits should be a number",
        fix="Enter a plain number in NumUnits.",
        kind="spec",
        definition={"check": "numeric"},
        modules=["claims"],
    )
    Rule.objects.create(
        rule_set=rules,
        rule_id="W4d5e6f",
        severity="warning",
        table="Measure",
        field="UnitMeaCost1stBaseline",
        kind="rule",
        message="When UnitIncentiveToOthers equals 0, UnitMeaCost1stBaseline should be different from 0",
        fix="Change it.",
        difference="Budget filings do not apply it.",
        modules=["claims"],
    )
    # a filing past its due date with a reopen request waiting for a CPUC claims approver
    service.grant(pge_admin, analyst, pge, "claims.confirm")
    approver = make_user("approver@cpuc.ca.gov", "Avery Prover")
    service.grant(cpuc_admin, approver, cpuc, "claims.approve")
    quarter = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    deadline = submissions.add_deadline(
        cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=timezone.now() + timedelta(days=10)
    )
    v = submissions.start(analyst, deadline, pge)
    submissions.attach_files(analyst, v, [{"name": "Measure.csv", "key": "s3://bucket/x", "sha256": "abc", "bytes": 10}])
    submissions.record_validation(analyst, v, None, {"records": 10, "rejected": 0, "warnings": 2})
    submissions.record_calculation(None, v, run="run-1", input_hash="h1")
    submissions.confirm(analyst, v, "h1")
    submissions.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "close-out for the scene")
    submissions.request_reopen(analyst, v.submission, "Two measures were counted twice")
    manifest = {
        "hourly/electric_2026.parquet": {"sha256": "ab" * 32, "bytes": 912_000_000},
        "README.md": {"sha256": "cd" * 32, "bytes": 2048},
    }
    pack = refdata.register_pack(
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v2 draft",
        acc_year=2026,
        manifest=manifest,
        storage_uri="s3://refdata/acc/v2/",
    )
    refdata.record_tie_out(cpuc_admin, pack, check="source_workbook", passed=False, precision=1e-9, largest_difference=0.02)
    published = refdata.register_pack(
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v1b",
        acc_year=2026,
        manifest=manifest,
        storage_uri="s3://refdata/acc/v1b/",
    )
    for check in ("source_workbook", "reference_measures"):
        refdata.record_tie_out(cpuc_admin, published, check=check, passed=True, precision=1e-9, largest_difference=0)
    refdata.publish_pack(cpuc_admin, published)
    return {
        "pack": pack,
        "published_pack": published,
        "rules": rules,
        "cpuc_admin": cpuc_admin,
        "approver": approver,
        "pge_admin": pge_admin,
        "community": community,
        "password": community,
        "pge": pge,
        "cpuc": cpuc,
        "allocation": allocation,
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
    response = page.goto(
        live_server.url
        + path.format(
            pge=scene["pge"].code,
            cpuc=scene["cpuc"].code,
            allocation=scene["allocation"].pk,
            rules=scene["rules"].pk,
            pack=scene["pack"].pk,
            published_pack=scene["published_pack"].pk,
        )
    )
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
