"""The front page (Jake, 2026-10-31; how the portfolio is doing, 2026-11-11): what CEDARS is, the published claims year by year, and
Help with its guides, support and the guided tour."""

import re

import pytest
from django.core.cache import cache
from django.utils.html import escape

from cedars.core import help as site_help
from cedars.modules.data import front

from .test_dashboard import both  # noqa: F401 - fixture
from .test_snapshots import filer  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def values(tsb=None, kwh=None, kw=None, therms=None, spent=None) -> dict:
    return {
        "tsb": tsb,
        "kwh_first_year_net": kwh,
        "kw_first_year_net": kw,
        "therms_first_year_net": therms,
        "expenditure": spent,
    }


ROWS = [  # TSB published from 2022 only, as in the legacy claims
    (2020, "IOU", "PGE", values(None, 400e6, 80e3, 10e6, 300e6)),
    (2021, "IOU", "PGE", values(None, 420e6, 82e3, 11e6, 320e6)),
    (2022, "IOU", "PGE", values(1.0e9, 450e6, 85e3, 12e6, 350e6)),
    (2022, "REN", "BAYREN", values(20e6, 5e6, 1e3, 1e6, 10e6)),
    (2023, "IOU", "PGE", values(1.1e9, 460e6, 86e3, 12e6, 400e6)),
    (2023, "IOU", "SCE", values(0.5e9, 300e6, 50e3, 0, 200e6)),
    (2023, "REN", "BAYREN", values(30e6, 6e6, 1e3, 1e6, 12e6)),
    (2023, "CCA", "MCE", values(5e6, 1e6, 100, 0, 2e6)),
]


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(front, "rows", lambda: front.Rows(list(ROWS)))


def test_the_tiles_say_the_year_and_its_change(legacy):
    t = front.trends({})
    assert t.year == 2023 and t.years == [2020, 2021, 2022, 2023] and t.pa_types == ["IOU", "REN", "CCA"]
    tiles = {x["key"]: x for x in t.tiles}
    assert list(tiles) == ["tsb", "kwh", "kw", "therms", "expenditure"]
    assert (tiles["tsb"]["number"], tiles["tsb"]["unit"]) == ("$1.64", "billion") and tiles["tsb"]["current"]
    assert tiles["tsb"]["change"]["text"] == "up 60% from 2022"  # 1.635B against 1.02B
    assert tiles["tsb"]["since"] == 2022  # no TSB published before 2022: not a zero
    assert (tiles["kwh"]["number"], tiles["kwh"]["unit"]) == ("767", "GWh")
    assert tiles["therms"]["change"]["text"] == "about the same as 2022"
    assert tiles["kw"]["href"] == "?metric=kw" and tiles["tsb"]["href"] == "?"


def test_the_charts(legacy):
    t = front.trends({})
    assert set(t.charts) == {"by_year", "indexed", "by_pa", "per_dollar"}  # four years: no shorter chart for phones
    by_year = t.charts["by_year"]
    assert by_year["years"] == [2022, 2023]  # from 2022, when TSB took effect: the years before it are not drawn (Jake, 2026-11-12)
    assert ">2020<" not in by_year["svg"] and ">2021<" not in by_year["svg"]
    assert front.trends({"metric": "kwh"}).charts["by_year"]["years"] == [2020, 2021, 2022, 2023]  # savings: every year
    tsb_tile = {x["key"]: x for x in t.tiles}["tsb"]
    assert tsb_tile["spark"]["line"].startswith("4.0,")  # the sparkline starts at its first published year
    assert 'style="' not in "".join(c["svg"] for c in t.charts.values())  # the CSP blocks inline styles
    assert len(by_year["table"]) == 2 and by_year["tips"]
    assert by_year["table"][1] == {"year": 2023, "value": "$1.64B", "change": "up 60% from 2022"}
    by_pa = t.charts["by_pa"]
    assert [r["code"] for r in by_pa["table"]] == ["PGE", "SCE", "BAYREN", "MCE"] and (by_pa["start"], by_pa["end"]) == (2022, 2023)
    assert by_pa["table"][1]["start"] == "none published" and by_pa["table"][0]["href"] == "/data/total-system-benefit/?pa=PGE"
    assert t.charts["indexed"]["base"] == 2022 and t.charts["indexed"]["table"][1] == {"year": 2023, "a": "160", "b": "171"}
    # TSB over spending, where both are published
    assert t.charts["per_dollar"]["table"] == [{"year": 2022, "value": "$2.83"}, {"year": 2023, "value": "$2.66"}]
    assert t.footnotes and "Energy Savings Assistance" in t.footnotes[0]


def test_a_pa_type_and_a_year(legacy):
    t = front.trends({"type": "REN", "year": "2022", "metric": "kwh"})
    assert (t.pa_type, t.year, t.metric.key) == ("REN", 2022, "kwh") and t.who == "the RENs"
    assert {x["key"]: x["number"] for x in t.tiles}["kwh"] == "5.00"
    assert t.link(metric="tsb") == "?type=REN&year=2022"
    assert t.metric_href == "/data/overview/?metric=kwh&pa_type=REN"
    assert [x["label"] for x in t.type_links] == ["All", "IOU", "REN", "CCA"]
    # nonsense is ignored, never an error
    t = front.trends({"type": "XYZ", "year": "1999", "metric": "nope"})
    assert (t.pa_type, t.year, t.metric.key) == ("", 2023, "tsb")


def test_a_long_history_gets_a_short_chart_for_phones(monkeypatch):
    rows = [(y, "IOU", "PGE", values(1e9 + y, 1e6, 1e3, 1e6, 1e8)) for y in range(2016, 2026)]
    monkeypatch.setattr(front, "rows", lambda: front.Rows(rows))
    t = front.trends({})
    assert "by_year_recent" in t.charts and len(t.charts["by_year_recent"]["tips"]) == front.PHONE_YEARS


def test_numbers_in_words():
    tsb, kwh = front.BY_KEY["tsb"], front.BY_KEY["kwh"]
    assert front.money(3.754e9) == "$3.75B" and front.money(313e6) == "$313M" and front.money(-2.5e6) == "-$2.50M"
    assert front.money(48_250) == "$48.2K" and front.money(480) == "$480"
    assert front.amount(kwh, 3_137e6) == "3,137 GWh" and front.amount(tsb, None) == "none published"
    assert front.tick(tsb, 6e9) == "$6B" and front.tick(tsb, 250e6) == "$250M" and front.tick(kwh, 1.5e9) == "1,500"
    assert front.split_amount(tsb, 869e6) == ("$869", "million")
    assert front.change(104, 100, 2024)["text"] == "up 4% from 2024" and front.change(98, 100, 2024)["dir"] == "down"
    assert front.change(1, 0, 2024) is None and front.change(None, 1, 2024) is None
    assert front.span([2028]) == "2028" and front.span([2029, 2028]) == "2028-2029" and front.span([]) == ""


def test_before_anything_is_published(db):
    t = front.trends({})
    assert t.years == [] and t.tiles == [] and t.charts == {} and t.notes


def test_published_claims(both):  # noqa: F811
    t = front.trends({})
    assert t.years == [2028] and t.year == 2028
    tsb = {x["key"]: x for x in t.tiles}["tsb"]
    assert tsb["number"] == "$180" and tsb["change"] is None  # one year: nothing to compare with


def test_published_savings_are_read_in_their_own_units(both):  # noqa: F811
    """The query's values are already scaled for display (kWh to MWh, therms to MMTherms): the front page reads the fields' sums, so
    1,234 GWh shows as 1,234 GWh and not 1.23; a field no file carries (kW here) is "none published", not 0."""
    [(year, _, pa, values)] = front._read().rows
    assert (year, pa) == (2028, "PGE")
    assert values == {"tsb": 180, "kwh_first_year_net": 3500, "kw_first_year_net": None, "therms_first_year_net": 10, "expenditure": 110}
    tiles = {x["key"]: x for x in front.trends({}).tiles}
    assert (tiles["kwh"]["number"], tiles["kwh"]["unit"]) == ("0.00", "GWh")  # 3,500 kWh
    assert (tiles["kw"]["number"], tiles["kw"]["unit"]) == ("None", "published")
    kwh = front.BY_KEY["kwh"]
    assert front.amount(kwh, 1_234e6) == "1,234 GWh" and front.amount(front.BY_KEY["kw"], 512e3) == "512 MW"


@pytest.fixture
def reads(both, monkeypatch):  # noqa: F811
    """Counts how often the claims are read, and a clock the test moves."""
    made = []
    real = front._read
    monkeypatch.setattr(front, "_read", lambda: made.append(1) or real())
    now = [1_000_000.0]
    monkeypatch.setattr(front.time, "time", lambda: now[0])
    return made, now


def test_kept_rows_are_shown_and_reread_after_the_response(reads, settings, monkeypatch):
    """Jake, 2026-11-06 (the front page was sometimes very slow): a visitor never waits for the numbers once they have been read; a
    copy older than FRESH_SECONDS is shown while they are read again in a thread, one at a time."""
    made, now = reads
    first = front.rows()
    assert len(made) == 1
    now[0] += front.FRESH_SECONDS - 1
    front.rows()
    assert len(made) == 1  # fresh: kept

    settings.FRONT_PAGE_REFRESH_IN_BACKGROUND = True
    started = []

    class Thread:
        def __init__(self, target, args, **kw):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(front.threading, "Thread", Thread)
    now[0] += 2
    assert front.rows().rows == first.rows and len(made) == 1  # shown at once, not read in the request
    front.rows()
    assert len(started) == 1  # a second visit does not start a second read
    target, args = started[0]
    target(*args)  # the thread's work
    assert len(made) == 2
    front.rows()
    assert len(started) == 1  # fresh again


def test_a_new_release_is_read_in_the_request(reads, monkeypatch):
    made, _ = reads
    front.rows()
    monkeypatch.setattr(front, "_fingerprint", lambda: "another release")
    front.rows()
    assert len(made) == 2  # nobody sees the previous release's numbers


def test_a_failed_reread_keeps_the_rows(reads, monkeypatch):
    made, now = reads
    kept = front.rows()
    now[0] += front.FRESH_SECONDS + 1
    monkeypatch.setattr(front, "_read", lambda: 1 / 0)
    assert front.rows().rows == kept.rows
    assert not front.cache.get(front._cache_key() + ":refreshing")  # the lock is freed, so a later visit tries again


def test_warm_front_page(both, capsys):  # noqa: F811
    from django.core.management import call_command

    call_command("warm_front_page")
    assert capsys.readouterr().out.startswith("Front page: ")
    assert front.cache.get(front._cache_key())["fingerprint"] == front._fingerprint()
    call_command("warm_front_page", "--all")  # kept for older scripts


def test_the_front_page(legacy, client):
    page = client.get("/").content.decode()
    assert '<h1 id="intro-title">California Energy Data and Reporting System</h1>' in page
    assert "{#" not in page and "#}" not in page  # a template comment over two lines prints as text
    assert "How the portfolio is doing" in page and 'data-tour="trends"' in page
    assert page.count('<figure class="trend-panel') == 4 and page.count("<summary>Show as a table</summary>") == 4
    assert 'aria-current="true"' in page and 'href="?metric=kwh"' in page
    assert "data/front.js" in page
    assert 'href="/data/total-system-benefit/"' in page
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
    # with no support address set, the card says nothing about it: only the feedback and Getting support links
    support = page.split("<h3>Feedback and support</h3>")[1].split("</div>")[0]
    assert "support address" not in support and "mailto:" not in support and 'href="/help/contact/"' in support


def test_the_support_address_shows_once_it_is_set(client, settings):
    settings.CEDARS_SUPPORT_EMAIL = "support@cedars.example.org"
    page = client.get("/").content.decode()
    support = page.split("<h3>Feedback and support</h3>")[1].split("</div>")[0]
    assert 'href="mailto:support@cedars.example.org' in support and "support@cedars.example.org</a>" in support
    contact = client.get("/help/contact/").content.decode()
    assert "support@cedars.example.org" in contact and "CEDARS_SUPPORT_EMAIL" not in contact


def test_the_front_page_with_nothing_published(client):
    r = client.get("/")
    assert r.status_code == 200 and "Nothing has been published" in r.content.decode()


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


def test_the_glossary(client, cpuc_admin, pge):
    """The glossary guide (Jake, 2026-11-02): acronyms, every PA organization's codes with its name and type, and terms, each with a
    link."""
    assert "glossary" in site_help.BY_SLUG
    page = client.get("/help/glossary/").content.decode()
    for anchor in ("acronyms", "pa-codes", "terms", "tsb", "iou", "ren", "cca", "sw", "cet", "deer", "pa", "operating-budget"):
        assert f'id="{anchor}"' in page
    assert '<tr><th scope="row">PGE</th><td>Pacific Gas and Electric</td><td>IOU</td></tr>' in page
    # every in-page link lands on an entry
    ids = set(re.findall(r'id="([^"]+)"', page))
    assert {t for t in re.findall(r'href="#([^"]+)"', page)} <= ids
    assert 'href="/help/glossary/"' in client.get("/").content.decode()
