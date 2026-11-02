import pytest
from django.core.management import CommandError, call_command

from cedars.services.accounts import service
from cedars.services.accounts.models import Organisation, User

pytestmark = pytest.mark.django_db

EMAIL = "filer@pge.example.org"


def test_demo_account_is_a_pa_submitter_that_signs_in(client, settings):
    settings.CEDARS_REQUIRE_MFA = False
    call_command("seed_sandbox", "--year", "2028")
    call_command("demo_account", EMAIL, "--name", "Phil Er", "--password", "a demo password 1")
    user = User.objects.get(email=EMAIL)
    pge = Organisation.objects.get(code="PGE")
    held = service.effective_permissions(user, pge)
    assert {"claims.submit", "claims.confirm", "budgets.submit", "monthly.submit", "documents.read"} <= held
    assert not any(p.endswith((".approve", ".admin")) for p in held) and "org.admin" not in held
    assert not service.is_system_admin(user)
    r = client.post("/accounts/login/", {"login": EMAIL, "password": "a demo password 1"})
    assert r.status_code == 302 and "_auth_user_id" in client.session


def test_demo_account_is_rerunnable_and_resets_the_password():
    call_command("seed_sandbox", "--year", "2028")
    call_command("demo_account", EMAIL, "--password", "first password 1")
    call_command("demo_account", EMAIL, "--password", "second password 2", "--org-admin")
    user = User.objects.get(email=EMAIL)
    assert user.check_password("second password 2")
    assert service.is_org_admin(user, Organisation.objects.get(code="PGE"))
    assert user.memberships.count() == 1


def test_demo_account_refuses_production_and_unknown_or_non_pa_orgs(settings):
    call_command("seed_sandbox", "--year", "2028")
    with pytest.raises(CommandError, match="no organization XYZ"):
        call_command("demo_account", EMAIL, "--org", "XYZ", "--password", "p")
    with pytest.raises(CommandError, match="not a program administrator"):
        call_command("demo_account", EMAIL, "--org", "CPUC", "--password", "p")
    settings.ENVIRONMENT = "prod"
    with pytest.raises(CommandError, match="not in prod"):
        call_command("demo_account", EMAIL, "--password", "p")
