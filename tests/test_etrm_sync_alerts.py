"""BR-11.3: the nightly eTRM sync retries a failure and alerts the CPUC's administrators when the last attempt fails too."""

from io import StringIO
from pathlib import Path

import pytest
from django.core import mail
from django.core.management import CommandError, call_command

from cedars.modules.impact_profiles import etrm
from cedars.modules.impact_profiles.models import EtrmSyncRun, MeasurePackage
from cedars.services.accounts import service as access
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification

FIXTURE = Path(__file__).parent / "data" / "etrm_fixture.json"
pytestmark = pytest.mark.django_db


class Flaky:
    """A source that fails its first `failures` reads."""

    label = "flaky eTRM"

    def __init__(self, failures):
        self.failures, self.reads = failures, 0

    def packages(self):
        self.reads += 1
        if self.reads <= self.failures:
            raise etrm.SourceError("the eTRM database could not be read: connection timed out")
        return etrm.FileSource(FIXTURE).packages()


@pytest.fixture
def profiles_admin(cpuc_admin, make_user, cpuc):
    u = make_user("profiles@cpuc.ca.gov", "Pat Profiles")
    access.grant(cpuc_admin, u, cpuc, "impact_profiles.admin")
    return u


def test_a_failed_sync_is_tried_again_and_nobody_is_alerted_when_a_retry_succeeds(cpuc_admin):
    waits = []
    run = etrm.nightly(Flaky(1), waits=[10, 30], sleep=waits.append)
    assert run.status == EtrmSyncRun.Status.SUCCEEDED and run.attempt == 2
    assert waits == [600]
    assert list(EtrmSyncRun.objects.order_by("pk").values_list("attempt", "status")) == [(1, "failed"), (2, "succeeded")]
    assert MeasurePackage.objects.count() == 2
    assert not Notification.objects.exists()


def test_when_every_attempt_fails_each_administrator_is_alerted_once(cpuc_admin, profiles_admin, make_user):
    make_user("someone@pge.com", "Not An Admin")
    waits = []
    run = etrm.nightly(Flaky(5), waits=[10, 30], sleep=waits.append)
    assert run.status == EtrmSyncRun.Status.FAILED and run.attempt == 3 and run.alerted_at is not None
    assert waits == [600, 1800]
    told = Notification.objects.filter(title="eTRM sync failed")
    assert set(told.values_list("user", flat=True)) == {cpuc_admin.pk, profiles_admin.pk}
    n = told.first()
    assert "after 3 attempts" in n.body and "connection timed out" in n.body and "No sync has succeeded yet" in n.body
    assert n.link == "/impact-profiles/etrm/"
    assert {m.to[0] for m in mail.outbox} == {cpuc_admin.email, profiles_admin.email}
    assert AuditEvent.objects.filter(action="etrm_sync.alerted").count() == 1
    assert AuditEvent.objects.filter(action="etrm_sync.failed").count() == 3
    assert etrm.alert_failure(run, 3) == 0  # once per run
    assert told.count() == 2


def test_an_unexpected_error_part_way_ends_the_run_failed_and_a_retry_completes_it(cpuc_admin, monkeypatch):
    real = etrm._store
    calls = []

    def store(run, rec, perms):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("deadlock detected")
        return real(run, rec, perms)

    monkeypatch.setattr(etrm, "_store", store)
    run = etrm.nightly(etrm.FileSource(FIXTURE), waits=[1], sleep=lambda s: None)
    first = EtrmSyncRun.objects.order_by("pk").first()
    assert first.status == EtrmSyncRun.Status.FAILED and "deadlock detected" in first.error and first.finished_at
    assert run.status == EtrmSyncRun.Status.SUCCEEDED and (run.imported, run.unchanged) == (1, 1)  # the first package was kept


def test_the_command_retries_alerts_and_fails(cpuc_admin, settings, tmp_path, monkeypatch):
    settings.ETRM_SYNC_RETRY_MINUTES = [0.01]
    monkeypatch.setattr(etrm.time, "sleep", lambda s: None)
    with pytest.raises(CommandError, match="attempt 2"):
        call_command("sync_etrm", "--fixture", str(tmp_path / "missing.json"), stdout=StringIO())
    assert EtrmSyncRun.objects.count() == 2
    assert Notification.objects.filter(user=cpuc_admin, title="eTRM sync failed").exists()
    with pytest.raises(CommandError, match="attempt 1"):
        call_command("sync_etrm", "--fixture", str(tmp_path / "missing.json"), "--no-retry", stdout=StringIO())
    assert EtrmSyncRun.objects.count() == 3
    out = StringIO()
    call_command("sync_etrm", "--fixture", str(FIXTURE), stdout=out)
    assert out.getvalue().startswith("Succeeded")


def test_the_alert_names_the_last_success(cpuc_admin):
    etrm.sync(None, etrm.FileSource(FIXTURE))
    run = etrm.nightly(Flaky(9), waits=[], sleep=lambda s: None)
    body = Notification.objects.get(user=cpuc_admin).body
    assert run.attempt == 1 and "failed." in body and "Last successful sync:" in body


def test_the_history_page_shows_attempts_and_alerts(signed_in, cpuc_admin):
    etrm.nightly(Flaky(9), waits=[5], sleep=lambda s: None)
    page = signed_in(cpuc_admin).get("/impact-profiles/etrm/").content.decode()
    assert "(attempt 2)" in page and "Administrators were alerted" in page and "after\n10 and 30 minutes" in page
