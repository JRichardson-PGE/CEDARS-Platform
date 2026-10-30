import pytest

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import User


@pytest.fixture
def sandbox(settings):
    settings.ENVIRONMENT = "sandbox"


@pytest.fixture
def make_user(db):
    def make(email, name="", password="correct horse battery staple"):
        return User.objects.create_user(email=email, name=name, password=password)

    return make


@pytest.fixture
def cpuc_admin(make_user):
    """The bootstrap: the CPUC organisation with a system administrator who is also its organisation admin and the first steward."""
    u = make_user("admin@cpuc.ca.gov", "Ada Admin")
    org = service.create_organisation(None, code="CPUC", name="California Public Utilities Commission", kind="cpuc")
    for code in (perms.ORG_ADMIN, perms.SYSTEM_ADMIN, perms.STEWARD):
        service.grant(None, u, org, code, reason="bootstrap")
    return u


@pytest.fixture
def cpuc(cpuc_admin):
    return service.cpuc()


@pytest.fixture
def pge(cpuc_admin):
    return service.create_organisation(cpuc_admin, code="PGE", name="Pacific Gas and Electric", kind="pa", pa_type="IOU")


@pytest.fixture
def pge_admin(make_user, cpuc_admin, pge):
    u = make_user("admin@pge.com", "Pat Admin")
    service.grant(cpuc_admin, u, pge, perms.ORG_ADMIN, reason="first admin")
    return u
