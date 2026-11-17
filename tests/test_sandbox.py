from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.utils import timezone

from cedars.services.accounts.models import Organisation
from cedars.services.dictionary.models import CatalogVersion
from cedars.services.notifications import service as notifications
from cedars.services.retention import service as retention
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, Submission

pytestmark = pytest.mark.django_db


def test_seed_sandbox_is_repeatable():
    call_command("seed_sandbox", "--year", "2028")
    call_command("seed_sandbox", "--year", "2028")
    assert Organisation.objects.filter(kind="pa").count() == 6 and Organisation.objects.filter(kind="cpuc").count() == 1
    assert Deadline.objects.count() == 4 + 12 + 1
    q4 = Deadline.objects.get(module="claims", period__number=4)
    assert timezone.localtime(q4.due_at).date().isoformat() == "2029-02-01"
    assert CatalogVersion.objects.count() == 1


def test_seed_sandbox_refuses_production(settings):
    settings.ENVIRONMENT = "prod"
    with pytest.raises(CommandError, match="not in prod"):
        call_command("seed_sandbox")


def test_sandbox_filings_expire_and_emails_say_sandbox(settings, cpuc_admin, pge, pge_admin):
    from cedars.services.registry import service as registry
    from cedars.services.submissions import apps

    settings.ENVIRONMENT = "sandbox"
    apps.SubmissionsConfig.ready(None)  # registers the sandbox purger, as starting the sandbox does
    period = registry.ensure_periods(2028)[1]
    d = submissions.add_deadline(cpuc_admin, module="claims", kind="claim", period=period, due_at=timezone.now() + timedelta(days=5))
    Submission.objects.create(deadline=d, organisation=pge)
    run = retention.purge(now=timezone.now() + timedelta(days=31))
    assert run.counts["sandbox_data"]["deleted"] == 1 and not Submission.objects.exists()
    notifications.notify(pge_admin, "Your run finished")
    assert mail.outbox[0].subject.startswith("[SANDBOX] CEDARS:")
    retention._purgers.pop("sandbox_data")
