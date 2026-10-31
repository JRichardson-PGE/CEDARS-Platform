import pytest

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import User


@pytest.fixture(autouse=True)
def local_storage(settings, tmp_path):
    """Every test stores files in its own temporary folder (cedars.core.storage)."""
    from cedars.core import storage

    settings.CEDARS_STORAGE = {"backend": "local", "root": str(tmp_path / "storage"), "buckets": {}}
    settings.CALCULATIONS_RESULTS_CACHE = str(tmp_path / "results_cache")
    settings.DATA_CACHE = str(tmp_path / "data_cache")
    storage.reset()
    yield
    storage.reset()


@pytest.fixture
def sandbox(settings):
    settings.ENVIRONMENT = "sandbox"


def create_user(email, name="", password="correct horse battery staple"):
    return User.objects.create_user(email=email, name=name, password=password)


def create_cpuc_admin():
    """The bootstrap: the CPUC organisation with a system administrator who is also its organisation admin and the first steward."""
    u = create_user("admin@cpuc.ca.gov", "Ada Admin")
    org = service.create_organisation(None, code="CPUC", name="California Public Utilities Commission", kind="cpuc")
    for code in (perms.ORG_ADMIN, perms.SYSTEM_ADMIN, perms.STEWARD):
        service.grant(None, u, org, code, reason="bootstrap")
    return u


def create_pge(cpuc_admin):
    return service.create_organisation(cpuc_admin, code="PGE", name="Pacific Gas and Electric", kind="pa", pa_type="IOU")


def create_pge_admin(cpuc_admin, pge):
    u = create_user("admin@pge.com", "Pat Admin")
    service.grant(cpuc_admin, u, pge, perms.ORG_ADMIN, reason="first admin")
    return u


@pytest.fixture
def make_user(db):
    return create_user


@pytest.fixture
def cpuc_admin(make_user):
    return create_cpuc_admin()


@pytest.fixture
def cpuc(cpuc_admin):
    return service.cpuc()


@pytest.fixture
def pge(cpuc_admin):
    return create_pge(cpuc_admin)


@pytest.fixture
def pge_admin(make_user, cpuc_admin, pge):
    return create_pge_admin(cpuc_admin, pge)


def sign_in(client, user, method="mfa"):
    """Sign a test client in as user, with a session established by method: password, mfa or socialaccount (federated)."""
    client.force_login(user)
    session = client.session
    records = [{"method": "password", "at": 0}]
    if method != "password":
        records.append({"method": method, "at": 0})
    session["account_authentication_methods"] = records
    session["account_authentication_methods_user"] = str(user.pk)
    session.save()
    return client


@pytest.fixture
def signed_in(client):
    return lambda user, method="mfa": sign_in(client, user, method)
