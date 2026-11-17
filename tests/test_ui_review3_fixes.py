"""UI review 3's small fixes (2026-11-11): a run under a second, the run page's tiles and checksums, one plain name for a filing,
and a cost-test set's page. (X1, printed template comments, is tests/test_template_comments.py; X6, the first recovery codes, is in
tests/test_uat2_wording.py.)"""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from cedars.modules.calculations import results
from cedars.modules.calculations import service as service_calc
from cedars.modules.calculations.models import Run
from cedars.modules.calculations.views import duration
from cedars.services.accounts import service as access
from cedars.services.registry import service as registry
from cedars.services.submissions import queue, service
from cedars.services.submissions.models import SubmissionVersion

from .test_calculations import analyst, job_zip, release, request  # noqa: F401 - fixtures
from .test_cost_test_sets import registered

pytestmark = pytest.mark.django_db


def test_a_run_of_under_a_second_says_so():
    assert duration(0.4) == "under a second" and duration(0) == "under a second"
    assert duration(12) == "under a minute" and duration(180) == "about 3 min" and duration(None) == ""


def test_the_money_tiles_carry_a_dollar_sign():
    tiles = {t["label"]: t["value"] for t in results.headline_totals({"TotalSystemBenefit": 881099.4, "TRC_ratio": 1.234}, 3)}
    assert tiles == {"Measures": "3", "Total system benefit": "$881,099", "TRC": "1.23"}
    assert results.headline_totals({"TotalExpenditures": -1500}, None)[0]["value"] == "-$1,500"


def test_no_empty_checksum_chips(tmp_path, signed_in, analyst, pge, release):  # noqa: F811
    run = service_calc.create_run(analyst, pge, request(release), job_zip(tmp_path))
    receipt = {
        "engine": {"release": "0.1.93"},
        "packs": {"files": {"acc/2024": {"sha256": "", "version": ""}, "acc/2026": {"sha256": "abc", "version": "3"}}},
        "totals": {"TotalSystemBenefit": 881099.4},
    }
    Run.objects.filter(pk=run.pk).update(status=Run.Status.SUCCEEDED, receipt=receipt)
    html = signed_in(analyst).get(f"/calculations/runs/{run.pk}/").content.decode()
    assert '<code class="small"></code>' not in html and "<code></code>" not in html
    assert "No checksum or version recorded" in html and "abc</code> version 3" in html
    assert "<dd>$881,099</dd>" in html


@pytest.fixture
def claimer(make_user, pge, pge_admin):
    u = make_user("claimer@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


def _quarter(year, n):
    return next(p for p in registry.ensure_periods(year) if p.kind == "quarter" and p.number == n)


def test_a_filing_has_one_plain_name_with_the_pacific_due_date(cpuc_admin, pge):
    # due 23:59 Pacific on 1 November: 06:59 UTC on 2 November
    due = datetime(2026, 11, 2, 6, 59, tzinfo=UTC)
    d = service.add_deadline(cpuc_admin, module="claims", kind="Q1 claim", period=_quarter(2027, 1), due_at=due)
    assert d.plain_name("PGE") == "PGE 2027 Q1 claim, due 2026-11-01" and str(d) == "2027 Q1 claim, due 2026-11-01"
    assert d.plain_name(due=False) == "2027 Q1 claim"
    other = service.add_deadline(cpuc_admin, module="claims", kind="quarterly claim", period=_quarter(2027, 2), due_at=due)
    assert other.plain_name("PGE", due=False) == "PGE 2027 Q2 quarterly claim"


def test_the_reopen_page_and_queue_name_a_filing_plainly(signed_in, claimer, pge, cpuc_admin, cpuc, make_user):
    d = service.add_deadline(
        cpuc_admin, module="claims", kind="Q1 claim", period=_quarter(2028, 1), due_at=timezone.now() + timedelta(days=5)
    )
    v = service.start(claimer, d, pge)
    service.attach_files(claimer, v, [{"name": "Measure.csv", "key": "k", "sha256": "abc", "bytes": 10}])
    service.record_validation(claimer, v, None, {"records": 1, "rejected": 0, "warnings": 0})
    service.record_calculation(None, v, run="run-1", input_hash="h1")
    v = service.confirm(claimer, SubmissionVersion.objects.get(pk=v.pk), "h1")
    service.move_deadline(cpuc_admin, d, timezone.now() - timedelta(minutes=1), "passes")
    d.refresh_from_db()
    v = SubmissionVersion.objects.get(pk=v.pk)
    name = f"PGE 2028 Q1 claim, due {timezone.localtime(d.due_at):%Y-%m-%d}"
    assert v.submission.plain_name == name and str(v) == f"{name}, version 1"
    req = service.request_reopen(claimer, v.submission, "double counted")
    approver = make_user("approver@cpuc.example.org")
    access.grant(cpuc_admin, approver, cpuc, "claims.approve")
    assert [i.what for i in queue.reopen_requests(approver)] == [f"Claims: {name}"]
    a = signed_in(approver)
    page = a.get("/filings/reopen-requests/").content.decode()
    assert f'<h3><a href="/claims/versions/{v.pk}/">{name}</a></h3>' in page  # named once, with a link to the filing
    assert "claims Q1 claim" not in page  # never the internal name
    r = a.post(f"/filings/reopen-requests/{req.pk}", {"decision": "approve"}, follow=True)
    html = r.content.decode()
    assert f"Approved: {name}." in html
    assert f'">{name}</a></th>' in html


def test_a_cost_test_sets_page_says_whether_it_is_in_force(signed_in, cpuc_admin):
    p = registered(cpuc_admin, years=(None, None), sid="EE-2026")
    html = signed_in(cpuc_admin).get(f"/reference-data/{p.pk}/").content.decode()
    crumbs = html.split('aria-label="Breadcrumb"', 1)[1].split("</nav>", 1)[0]
    assert "Cost-test sets</a>" in crumbs and "Data packs" not in crumbs
    assert "<dt>In force</dt><dd>Yes, the set in force today</dd>" in html and "not dated" not in html
