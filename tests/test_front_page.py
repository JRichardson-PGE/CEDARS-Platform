"""The front page (Jake, 2026-10-31): what CEDARS is, the program cycle at a glance from the published data, and Help with its guides,
support and the guided tour."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from cedars.core import help as site_help
from cedars.modules.data import front
from cedars.services.registry.models import FilingCycle
from cedars.services.submissions import service as submissions

from .test_dashboard import both, goal_set  # noqa: F401 - fixtures
from .test_data_query import publish
from .test_snapshots import filer  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def cards(summary) -> dict:
    return {c.key: c for c in summary.cards}


def test_the_cycle_at_a_glance(both, goal_set):  # noqa: F811
    s = front.summary("2028-2031")
    assert str(s.cycle) == "2028-2031" and s.claimed_years == [2028] and s.claimed_span == "2028"
    c = cards(s)
    assert list(c) == ["spending", "tsb", "savings", "cost_effectiveness"]

    spend = c["spending"]  # no operating budget recorded: the latest filings' forecast is the budget
    assert [f["value"] for f in spend.figures] == [front.money(110), front.money(220)]
    assert spend.percent == 50 and spend.bar == 50 and "budget in the latest filings" in spend.percent_label

    tsb = c["tsb"]  # voluntary programs: no codes and standards, ESA or pension; the PGE cycle goal of the set in force
    assert tsb.figures[0]["value"] == front.money(180) and tsb.figures[1] == {"label": "Goal, 2028-2031", "value": front.money(2000)}
    assert tsb.percent == pytest.approx(9) and "counts toward the IOU territories' goals" in tsb.lines[0]

    saved = c["savings"]
    assert [f["label"] for f in saved.figures] == ["MWh", "MW", "MMTherms"]
    assert saved.figures[0]["value"] == "4" and saved.figures[0]["detail"] == "of 7 forecast"  # 3,500 kWh and 7,000 kWh

    ce = c["cost_effectiveness"]  # PGE_Com_Lt is flagged Exclude_From_CE: left out, as on the cost-effectiveness page
    trc, pac = ce.figures
    assert trc == {"label": "TRC ratio", "value": "1.00", "detail": "1.00 forecast"}
    assert pac["value"] == front.NONE  # no PAC costs in these records: blank, never 0
    assert "/data/cost-effectiveness/?year_from=2028&year_to=2031" == ce.link


def test_no_goal_loaded_says_so(both):  # noqa: F811
    tsb = cards(front.summary("2028-2031"))["tsb"]
    assert len(tsb.figures) == 1 and tsb.percent is None
    assert "No CPUC goal is loaded for this cycle yet" in tsb.lines[0]


def test_before_anything_is_published(db):
    FilingCycle.objects.create(first_year=2028, last_year=2031)
    c = cards(front.summary("2028-2031"))
    assert c["spending"].empty and c["savings"].empty and c["cost_effectiveness"].empty
    assert front.summary("2028-2031").claimed_span == ""


@pytest.fixture
def forecast_only(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    """A 2028-2031 forecast and no claims yet."""
    cycle = FilingCycle.objects.create(first_year=2028, last_year=2031)
    tual = submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", cycle=cycle, due_at=timezone.now() - timedelta(days=1))
    submissions.set_policy(cpuc_admin, "budgets", late_confirmation=True)
    publish(filer, tual, pge, django_capture_on_commit_callbacks, "t1", scale=2.0)


def test_a_cycle_with_no_claims_says_so_once(forecast_only, client):
    """UI review 2, #23: the intro says no claims are published; the cards leave the claimed figures out and show the forecast."""
    s = front.summary("2028-2031")
    assert s.claimed_span == ""
    c = cards(s)
    assert all(f["value"] != front.NONE for card in s.cards for f in card.figures)
    assert [f["label"] for f in c["savings"].figures] == ["MWh forecast", "MW forecast", "MMTherms forecast"]
    assert [f["label"] for f in c["spending"].figures] == ["Budget in the latest filings, 2028-2031"]
    page = client.get("/?cycle=2028-2031").content.decode()
    assert "No claims are published for this cycle yet." in page and front.NONE not in page


def test_no_cycle_set_up(db):
    s = front.summary()
    assert s.cycle is None and s.cards == [] and "No program cycle is set up yet" in s.notes[0]


def test_the_cycle_choice(both):  # noqa: F811
    FilingCycle.objects.create(first_year=2024, last_year=2027)
    assert str(front.summary("2024-2027").cycle) == "2024-2027"
    assert [str(c) for c in front.summary("2024-2027").cycles] == ["2028-2031", "2024-2027"]
    assert front.summary("nonsense").cycle is not None  # the default cycle


def test_money():
    assert (
        front.money(412_345_678) == "$412.3M"
        and front.money(-2_500_000) == "-$2.5M"
        and front.money(48_250.4) == "$48,250"
        and front.money(None) == front.NONE
    )


def test_the_front_page(both, goal_set, client):  # noqa: F811
    page = client.get("/?cycle=2028-2031").content.decode()
    assert '<h1 id="intro-title">California Energy Data and Reporting System</h1>' in page
    assert "The 2028-2031 program cycle so far" in page and "Claims cover 2028 so far" in page
    for key in ("spending", "tsb", "savings", "cost_effectiveness"):
        assert f'data-card="{key}"' in page
    assert "9% of the goal" in page and 'class="meter-fill" width="9.0"' in page
    assert 'href="/data/goals/?year_from=2028&amp;year_to=2031&amp;period=cycle"' in page
    assert "Sign in" in page and "Create an account" in page
    # Help, the guides and the tour
    assert 'href="/help/tour/" data-tour-start' in page and 'id="tour"' in page and "core/tour.js" in page
    for g in site_help.GUIDES:
        assert f'href="/help/{g.slug}/"' in page
    assert "This site has no support address set yet." in page


def test_the_front_page_no_longer_redirects(client):
    r = client.get("/")
    assert r.status_code == 200 and "No program cycle is set up yet" in r.content.decode()


def test_support_comes_from_settings(client, settings):
    settings.CEDARS_SUPPORT_EMAIL = "support@example.org"
    s = site_help.support()
    assert s["mailto"] == "mailto:support@example.org" and s["feedback"] == "mailto:support@example.org?subject=CEDARS%20feedback"
    assert not s["feedback_is_form"]
    settings.CEDARS_FEEDBACK_URL = "https://feedback.example.org/cedars"
    assert site_help.support()["feedback"] == "https://feedback.example.org/cedars" and site_help.support()["feedback_is_form"]
    page = client.get("/help/contact/").content.decode()
    assert 'href="mailto:support@example.org?subject=CEDARS%20support%20request"' in page
    assert "Send feedback on the feedback form" in page


def test_help_pages(client):
    index = client.get("/help/").content.decode()
    for g in site_help.GUIDES:
        assert f'href="/help/{g.slug}/"' in index
        r = client.get(f"/help/{g.slug}/")
        assert r.status_code == 200 and f"<h1>{g.title}</h1>" in r.content.decode()
    assert client.get("/help/nope/").status_code == 404
    assert 'href="/?tour=1"' in index


def test_the_tour_for_each_visitor(client, signed_in, pge_admin):
    out = [s.target for s in site_help.tour_steps(False)]
    signed = [s.target for s in site_help.tour_steps(True)]
    assert out[0] == "intro" and out[-1] == "help" and "sign-in" in out and "nav-programs" not in out
    assert "nav-filings" in signed and "account" in signed and "notifications" in signed and "sign-in" not in signed
    assert "nav-admin" not in signed  # administration only for administrators
    admin = [s.target for s in site_help.tour_steps(site_help.Visitor(signed_in=True, acting_for=True, org_admin=True))]
    assert "nav-admin" in admin and "acting-for" in admin
    assert len(signed) > len(out)  # the signed-in parts get steps of their own
    written = client.get("/help/tour/").content.decode()
    assert all(f"<h2>{s.title}</h2>" in written for s in site_help.tour_steps(False))
    # every step's part of the page is on the front page, signed out and signed in
    page = client.get("/").content.decode()
    assert all(f'data-tour="{t}"' in page for t in out)
    c = signed_in(pge_admin)
    page = c.get("/").content.decode()
    steps = site_help.tour_steps(site_help.visitor(c.get("/").wsgi_request))
    assert {"nav-admin", "acting-for"} <= {s.target for s in steps}  # PG&E's administrator
    assert all(f'data-tour="{s.target}"' in page for s in steps)


def test_help_is_open_to_a_password_only_session(signed_in, make_user):
    c = signed_in(make_user("new@example.com"), "password")
    assert c.get("/help/").status_code == 200 and c.get("/help/getting-started/").status_code == 200
    assert c.get("/").status_code == 302  # the front page waits for the second factor, as every other page does
