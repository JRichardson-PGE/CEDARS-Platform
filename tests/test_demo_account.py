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


def test_demo_account_files_for_every_pa_of_a_type():
    call_command("seed_sandbox", "--year", "2028")
    service.create_organisation(None, code="SCR", name="SoCalREN", kind="pa", pa_type="REN")
    service.create_organisation(None, code="TCR", name="3C-REN", kind="pa", pa_type="REN")
    call_command("demo_account", "rens@example.org", "--pa-type", "REN", "--password", "a demo password 1")
    user = User.objects.get(email="rens@example.org")
    assert sorted(user.memberships.values_list("organisation__code", flat=True)) == ["BAY", "SCR", "TCR"]
    for code in ("BAY", "SCR", "TCR"):
        assert "claims.submit" in service.effective_permissions(user, Organisation.objects.get(code=code))
    call_command("demo_account", "two@example.org", "--org", "PGE", "--org", "MCE", "--password", "p")
    assert sorted(User.objects.get(email="two@example.org").memberships.values_list("organisation__code", flat=True)) == ["MCE", "PGE"]
    with pytest.raises(CommandError, match="no active program administrator of type XYZ"):
        call_command("demo_account", EMAIL, "--pa-type", "XYZ", "--password", "p")


def test_demo_account_cpuc_is_an_administrator_that_signs_in(client, settings):
    settings.CEDARS_REQUIRE_MFA = False
    call_command("seed_sandbox", "--year", "2028")
    call_command("demo_account", "admin@cpuc.example.org", "--cpuc", "--name", "Ada Min", "--password", "a demo password 1")
    user = User.objects.get(email="admin@cpuc.example.org")
    cpuc = service.cpuc()
    held = service.effective_permissions(user, cpuc)
    assert {"claims.approve", "budgets.admin", "documents.read", "org.admin", "system.admin", "pii.steward"} <= held
    assert service.is_system_admin(user) and service.is_steward(user) and service.is_cpuc_staff(user)
    assert list(user.memberships.values_list("organisation__code", flat=True)) == ["CPUC"]
    r = client.post("/accounts/login/", {"login": "admin@cpuc.example.org", "password": "a demo password 1"})
    assert r.status_code == 302 and "_auth_user_id" in client.session
    call_command("demo_account", "admin@cpuc.example.org", "--cpuc", "--password", "a second password 2")
    user.refresh_from_db()
    assert user.check_password("a second password 2") and user.memberships.count() == 1
    with pytest.raises(CommandError, match="leave out --org"):
        call_command("demo_account", "admin@cpuc.example.org", "--cpuc", "--org", "PGE", "--password", "p")


def test_demo_account_all_pas_admin_and_filer():
    call_command("seed_sandbox", "--year", "2028")
    service.create_organisation(None, code="SCR", name="SoCalREN", kind="pa", pa_type="REN")
    gone = service.create_organisation(None, code="OLD", name="Retired PA", kind="pa", pa_type="CCA")
    gone.active = False
    gone.save()
    every = sorted(Organisation.objects.filter(kind="pa", active=True).values_list("code", flat=True))
    assert "SCR" in every and "OLD" not in every and "CPUC" not in every

    call_command("demo_account", "admin-all@example.org", "--all-pas", "--org-admin", "--password", "a demo password 1")
    admin = User.objects.get(email="admin-all@example.org")
    assert sorted(admin.memberships.values_list("organisation__code", flat=True)) == every
    for code in every:
        organisation = Organisation.objects.get(code=code)
        assert service.is_org_admin(admin, organisation)
        assert {"claims.submit", "budgets.confirm"} <= service.effective_permissions(admin, organisation)
    assert not service.is_system_admin(admin)

    call_command("demo_account", "filer-all@example.org", "--all-pas", "--password", "a demo password 1")
    filer = User.objects.get(email="filer-all@example.org")
    assert sorted(filer.memberships.values_list("organisation__code", flat=True)) == every
    for code in every:
        held = service.effective_permissions(filer, Organisation.objects.get(code=code))
        assert "claims.submit" in held and "org.admin" not in held
        assert not any(p.endswith((".approve", ".admin")) for p in held)

    # A PA added later joins on the next run.
    service.create_organisation(None, code="TCR", name="3C-REN", kind="pa", pa_type="REN")
    call_command("demo_account", "filer-all@example.org", "--all-pas", "--password", "a demo password 1")
    assert "TCR" in filer.memberships.values_list("organisation__code", flat=True)

    with pytest.raises(CommandError, match="leave out --org and --pa-type"):
        call_command("demo_account", EMAIL, "--all-pas", "--org", "PGE", "--password", "p")
    with pytest.raises(CommandError, match="leave out --org, --pa-type, --all-pas"):
        call_command("demo_account", EMAIL, "--all-pas", "--cpuc", "--password", "p")
