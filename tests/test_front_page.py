"""The front page (Jake, 2026-10-31): what CEDARS is, the program cycle at a glance from the published data, and Help with its guides,
support and the guided tour."""

import re
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import escape

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


KEYS = ["spending", "tsb", "energy", "demand", "gas", "trc", "pac"]


def test_the_cycle_at_a_glance(both, goal_set):  # noqa: F811
    s = front.summary("2028-2031")
    assert str(s.cycle) == "2028-2031" and s.claimed_years == [2028] and s.claimed_span == "2028"
    c = cards(s)
    assert list(c) == KEYS  # seven compact cards (Jake, 2026-11-03)
    assert [k for k, card in c.items() if card.bar is not None] == ["spending", "tsb", "energy", "demand", "gas"]  # not the ratios
    assert [c[k].title for k in ("energy", "demand")] == ["Electricity saved", "Peak demand avoided"]
    # what each card counts, under its title (Jake): TSB voluntary programs, the savings codes and standards, the ratios RA
    assert [c[k].scope for k in KEYS] == ["", "Voluntary programs only"] + [front.CS_SCOPE] * 3 + [front.RA_SCOPE] * 2

    spend = c["spending"]  # no operating budget recorded: the latest filings' forecast is the budget
    assert spend.figures == [
        {"label": "Claimed 2028", "value": front.money(110), "detail": f"of {front.money(220)} budget in the latest filings"}
    ]
    assert spend.percent == 50 and spend.bar == 50 and spend.percent_text == "50%" and spend.percent_label == "of budget"
    assert any("no operating budget is recorded" in n for n in s.footnotes)

    tsb = c["tsb"]  # voluntary programs: no codes and standards, ESA or pension; the PGE cycle goal of the set in force
    assert tsb.figures == [{"label": "Claimed 2028", "value": front.money(180), "detail": f"Goal {front.money(2000)}, 2028-2031"}]
    assert tsb.percent == pytest.approx(9) and tsb.percent_label == "of IOU goals"
    assert any("counts toward the IOU territories' goals" in n for n in s.footnotes)

    # codes and standards only (PGE_CS: 9,000 kWh) against PG&E's C&S cycle goal of 0.05 GWh, shown as 50 MWh
    energy = c["energy"]
    assert energy.figures == [{"label": "Claimed 2028", "value": "9", "unit": "MWh", "detail": "of 50 MWh goal"}]
    assert energy.percent == pytest.approx(18) and energy.percent_label == "of C&S goals"
    assert [c[k].figures[0]["unit"] for k in ("demand", "gas")] == ["MW", "MMTherms"]
    assert c["demand"].percent == 0 and c["demand"].figures[0]["detail"] == "of 1.0 MW goal"
    assert c["energy"].link == "/data/goals/?year_from=2028&year_to=2031&metric=cs_gwh&period=cycle"
    assert c["gas"].link.endswith("metric=cs_mmtherms&period=cycle")
    assert any(
        n.startswith("Electricity saved, peak demand avoided and gas saved are codes and standards programs only") for n in s.footnotes
    )

    # the resource acquisition segment only; PGE_Com_Lt is flagged Exclude_From_CE: left out, as on the cost-effectiveness page
    trc = c["trc"]  # the ratio large in the gauge's place, against 1 in words; benefits and costs under it (Jake: no gauge)
    assert trc.bar is None and trc.headline == "1.00" and trc.percent_label == "At 1: benefits equal costs"
    assert trc.figures[0]["unit"] == "benefits" and trc.figures[0]["detail"].endswith("costs; 1.00 forecast ratio")
    assert c["pac"].bar is None and c["pac"].figures[0]["detail"] == "$0 costs; ratio none yet"  # no PAC costs: blank, never 0
    assert c["trc"].link == "/data/cost-effectiveness/?year_from=2028&year_to=2031&segment=Resource+Acquisition"
    assert c["pac"].link.endswith("segment=Resource+Acquisition&metric=pac")


def test_no_goal_loaded_measures_tsb_against_the_forecast(both):  # noqa: F811
    s = front.summary("2028-2031")
    tsb = cards(s)["tsb"]
    assert [f["label"] for f in tsb.figures] == ["Claimed 2028"] and tsb.figures[0]["detail"].endswith(" forecast")
    assert tsb.percent_label == "of forecast" and tsb.percent is not None
    assert any("No CPUC goal is loaded for this cycle yet" in n for n in s.footnotes)


def test_a_ratio_against_its_threshold():
    assert front.threshold(0.84) == "Below 1: costs exceed benefits" and front.threshold(1.12) == "Above 1: benefits exceed costs"
    assert front.threshold(0.999) == "At 1: benefits equal costs"  # as shown, to two places


def test_a_gauge_past_100_is_full_and_says_how_far():
    card = front.Card("x", "X", percent=112.4)
    assert card.bar == 100 and card.percent_text == "112%"
    assert front.Card("x", "X", percent=-3).bar == 0 and front.Card("x", "X").bar is None


def test_before_anything_is_published(db):
    FilingCycle.objects.create(first_year=2028, last_year=2031)
    s = front.summary("2028-2031")
    assert all(card.empty for card in s.cards) and [card.key for card in s.cards] == KEYS
    assert s.claimed_span == "" and s.footnotes == []


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
    assert [c[k].figures[0]["label"] for k in ("energy", "demand", "gas", "trc")] == ["Forecast, 2028-2031"] * 4
    assert all(c[k].bar is None for k in ("spending", "energy"))  # nothing claimed: nothing to fill a gauge with
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


@pytest.fixture
def builds(both, monkeypatch):  # noqa: F811
    """Counts how often the cards are made, and a clock the test moves."""
    made = []
    real = front._build
    monkeypatch.setattr(front, "_build", lambda *a: made.append(1) or real(*a))
    now = [1_000_000.0]
    monkeypatch.setattr(front.time, "time", lambda: now[0])
    return made, now


def test_kept_cards_are_shown_and_remade_after_the_response(builds, settings, monkeypatch):
    """Jake, 2026-11-06 (the front page was sometimes very slow): a visitor never waits for the cards once they have been made; a copy
    older than FRESH_SECONDS is shown while new ones are made in a thread, one at a time."""
    made, now = builds
    first = front.summary()
    assert len(made) == 1
    now[0] += front.FRESH_SECONDS - 1
    assert front.summary() is not None and len(made) == 1  # fresh: kept

    settings.FRONT_PAGE_REFRESH_IN_BACKGROUND = True
    started = []

    class Thread:
        def __init__(self, target, args, **kw):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(front.threading, "Thread", Thread)
    now[0] += 2
    stale = front.summary()
    assert [c.key for c in stale.cards] == [c.key for c in first.cards] and len(made) == 1  # shown at once, not made in the request
    front.summary()
    assert len(started) == 1  # a second visit does not start a second rebuild
    target, args = started[0]
    target(*args)  # the thread's work
    assert len(made) == 2
    front.summary()
    assert len(started) == 1  # fresh again


def test_a_new_release_or_goal_set_is_made_in_the_request(builds, monkeypatch):
    made, _ = builds
    front.summary()
    monkeypatch.setattr(front, "_fingerprint", lambda: "another release")
    front.summary()
    assert len(made) == 2  # nobody sees the previous release's numbers


def test_a_failed_rebuild_keeps_the_cards(builds, monkeypatch):
    made, now = builds
    kept = front.summary()
    now[0] += front.FRESH_SECONDS + 1
    monkeypatch.setattr(front, "_build", lambda *a: 1 / 0)
    assert [c.key for c in front.summary().cards] == [c.key for c in kept.cards]
    assert not front.cache.get(front._cache_key(kept.cycle) + ":refreshing")  # the lock is freed, so a later visit tries again


def test_warm_front_page(both, capsys):  # noqa: F811
    from django.core.management import call_command

    call_command("warm_front_page")
    assert capsys.readouterr().out.startswith("2028-2031: ")
    assert front.cache.get(front._cache_key(front.pick_cycle()[0]))["fingerprint"] == front._fingerprint()


def test_warm_front_page_with_no_cycle(db, capsys):
    from django.core.management import call_command

    call_command("warm_front_page", "--all")
    assert "nothing to make" in capsys.readouterr().out


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
    assert "{#" not in page and "#}" not in page  # a template comment over two lines prints as text
    assert "The 2028-2031 program cycle so far" in page and "Claims cover 2028 so far" in page
    for key in KEYS:
        assert f'data-card="{key}"' in page
    # the gauge's words are text in the card; the drawing is hidden and filled by an attribute (no inline style under the CSP)
    assert '<span class="gauge-pct">9%</span> <span class="gauge-of">of IOU goals</span>' in page
    assert 'class="gauge-fill"' in page and 'stroke-dasharray="9.0 100"' in page and 'aria-hidden="true"' in page
    assert (
        page.count('class="gauge-fill"') == 5
        and page.count('class="gauge gauge-number"') == 1
        and 'style="' not in page.split('class="front-cards"')[1].split("</section>")[0]
    )
    demand = '<a class="card-title" href="/data/goals/?year_from=2028&amp;year_to=2031&amp;metric=cs_mw&amp;period=cycle">'
    assert demand + 'Peak demand avoided <span class="card-scope">Codes and standards only</span></a>' in page
    assert 'href="/data/goals/?year_from=2028&amp;year_to=2031&amp;period=cycle"' in page
    assert "Sign in" in page and "Create an account" in page
    # Help, the guides and the tour
    assert 'href="/help/tour/" data-tour-start' in page and 'id="tour"' in page and "core/tour.js" in page
    # The first three guides, then All of Help; the help index lists the rest
    guides = page.split("<h3>User guides</h3>")[1].split("</div>")[0]
    for g in site_help.GUIDES[:3]:
        assert f'href="/help/{g.slug}/"' in guides
    for g in site_help.GUIDES[3:]:
        assert f'href="/help/{g.slug}/"' not in guides
    assert '<a href="/help/">All of Help</a>' in guides
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
        assert r.status_code == 200 and f"<h1>{escape(g.title)}</h1>" in r.content.decode()
    assert client.get("/help/nope/").status_code == 404
    assert 'href="/?tour=1"' in index


def test_the_tour_for_each_visitor(client, signed_in, pge_admin):
    out = [s.target for s in site_help.tour_steps(False)]
    signed = [s.target for s in site_help.tour_steps(site_help.Visitor(signed_in=True, acting_for=True))]
    assert out[0] == "intro" and out[-1] == "help" and "sign-in" in out and "nav-programs" in out and "nav-filings" not in out
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


def test_the_glossary(client):
    """The glossary guide (Jake, 2026-11-02): acronyms, every PA code CEDARS knows with its name and type, and terms, each with a link."""
    from django.utils.html import escape

    from cedars.services.accounts import service as accounts

    assert "glossary" in site_help.BY_SLUG
    page = client.get("/help/glossary/").content.decode()
    for anchor in ("acronyms", "pa-codes", "terms", "tsb", "iou", "ren", "cca", "sw", "cet", "deer", "pa", "operating-budget"):
        assert f'id="{anchor}"' in page
    for code in accounts.KNOWN_PA_TYPES:
        assert f'<tr><th scope="row">{code}</th><td>{escape(accounts.known_pa_name(code))}' in page
    assert "East Bay Community Energy (before 2024; now Ava Community Energy, AVA)" in page
    # every in-page link lands on an entry
    ids = set(re.findall(r'id="([^"]+)"', page))
    assert {t for t in re.findall(r'href="#([^"]+)"', page)} <= ids
    assert 'href="/help/glossary/"' in client.get("/").content.decode()
