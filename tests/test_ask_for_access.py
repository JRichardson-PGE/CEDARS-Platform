"""Asking an organization for access (UI review 3, U6): the organization first, then its roles in plain words with single permissions
behind a disclosure, every permission named and described in words, no statewide pseudo-PA, and an organization with no administrator
has its requests decided by the CPUC."""

import pytest

from cedars.core import queue
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import AccessRequest, RoleTemplate
from cedars.services.accounts.service import AccessDenied

from .conftest import page_only


def _queue(user, kind="access"):
    return {s.kind.key: s.items for s in queue.sections(user)}.get(kind, [])


@pytest.fixture
def filer_role(cpuc_admin):
    return service.create_role_template(
        cpuc_admin,
        name="Claims filer",
        description="Files the organization's quarterly claims.",
        permissions=["claims.submit", "claims.read"],
        kinds=["pa"],
        reason="test",
    )


@pytest.fixture
def mce(cpuc_admin):
    return service.create_organisation(cpuc_admin, code="MCE", name="Marin Clean Energy", kind="pa", pa_type="CCA")


def test_every_permission_has_a_name_and_a_sentence_in_words():
    for code in perms.all_permissions():
        words = perms.describe(code)
        assert words.endswith(".") and code not in words and "_" not in words, code
    assert perms.labels(["claims.submit", "claims.read", "claims.read"]) == "Claims: read, Claims: submit"


@pytest.mark.django_db
def test_the_first_step_lists_the_organizations_people_can_join(signed_in, make_user, pge, cpuc_admin):
    service.create_organisation(cpuc_admin, code="SW", name="Statewide", kind="pa", pa_type="IOU")
    html = page_only(signed_in(make_user("new@example.org")).get("/account/request-access/").content.decode())
    select = html.split('name="organisation"', 1)[1].split("</select>", 1)[0]
    assert '<option value="PGE">' in select
    assert 'value="SW"' not in select and 'value="CPUC"' not in select
    assert "Next: choose your role" in html
    assert '<li aria-current="step" class="now"><span class="n" aria-hidden="true">3</span> Ask an organization for access' in html


@pytest.mark.django_db
def test_a_member_asking_another_organization_sees_no_set_up_steps(signed_in, pge, pge_admin):
    html = page_only(signed_in(pge_admin).get("/account/request-access/").content.decode())
    assert "set-up-steps" not in html


@pytest.mark.django_db
def test_roles_come_first_in_plain_words_and_single_permissions_fold_away(signed_in, make_user, pge, pge_admin, filer_role):
    html = page_only(signed_in(make_user("new@example.org")).get("/account/request-access/?organisation=PGE").content.decode())
    assert "Your request goes to PGE&#x27;s administrators, who decide it." in html or "Your request goes to PGE's administrators" in html
    roles = html.split('<fieldset class="request-roles"', 1)[1].split("</fieldset>", 1)[0]
    assert (
        "Claims filer</label>" in roles and "Files the organization&#x27;s quarterly claims. Gives: Claims: read, Claims: submit." in roles
    )
    singles = html.split('<details class="request-singles"', 1)[1].split("</details>", 1)[0]
    assert singles.startswith(">")  # closed until someone opens it
    assert "Claims: submit</label>" in singles and perms.describe("claims.submit") in singles
    assert "claims.submit<" not in html.replace('value="claims.submit"', "")


@pytest.mark.django_db
def test_a_role_asks_for_its_permissions(signed_in, make_user, pge, pge_admin, filer_role):
    u = make_user("new@example.org")
    c = signed_in(u)
    r = c.post("/account/request-access/", {"organisation": "PGE", "message": "I file our claims"})
    assert r.status_code == 200 and "Choose a role or at least one permission." in r.content.decode()
    r = c.post("/account/request-access/", {"organisation": "PGE", "roles": [str(filer_role.pk)], "permissions": ["programs.read"]})
    assert r.status_code == 302
    assert AccessRequest.objects.get(user=u).permissions == ["claims.read", "claims.submit", "programs.read"]
    (item,) = _queue(pge_admin)
    assert item.note == "Asks for: Programs: read, Claims: read, Claims: submit"


@pytest.mark.django_db
def test_without_roles_the_permissions_are_shown_at_once(signed_in, make_user, pge, pge_admin):
    RoleTemplate.objects.update(active=False)  # the starter templates retired: none offered
    html = page_only(signed_in(make_user("new@example.org")).get("/account/request-access/?organisation=PGE").content.decode())
    assert "request-roles" not in html and "<details" not in html.split("<main", 1)[1].split("page-title", 1)[1].split("</form>", 1)[0]
    assert "Permissions you need in PGE" in html


@pytest.mark.django_db
def test_the_cpuc_decides_for_an_organization_with_no_administrator(signed_in, make_user, cpuc_admin, mce, pge, pge_admin):
    u = make_user("new@example.org")
    c = signed_in(u)
    html = page_only(c.get("/account/request-access/?organisation=MCE").content.decode())
    assert "MCE has no administrator yet, so the CPUC decides your request." in html
    r = c.post("/account/request-access/", {"organisation": "MCE", "permissions": ["claims.read"]}, follow=True)
    assert "Your request was sent to the CPUC (MCE has no administrator yet)." in r.content.decode()
    req = AccessRequest.objects.get(user=u)

    (item,) = _queue(cpuc_admin)
    assert item.what.endswith("asks to join MCE") and "the CPUC decides" in item.note and "Claims: read" in item.note
    assert _queue(pge_admin) == []  # another organization's administrator is not asked
    assert list(service.request_deciders(mce)) == [cpuc_admin]

    assert "Until then the CPUC decides its access requests." in signed_in(cpuc_admin).get("/admin/organizations/MCE/").content.decode()
    page = signed_in(cpuc_admin).get("/admin/organizations/MCE/requests/").content.decode()
    assert f'action="/admin/requests/{req.pk}/decide"' in page and "MCE has no administrator yet, so the CPUC decides" in page
    r = signed_in(cpuc_admin).post(f"/admin/requests/{req.pk}/decide", {"decision": "approve", "permissions": ["claims.read"]})
    assert r.status_code == 302
    assert service.effective_permissions(u, mce) == {"claims.read"}


@pytest.mark.django_db
def test_once_it_has_an_administrator_the_cpuc_no_longer_decides(make_user, cpuc_admin, mce):
    admin = make_user("admin@mce.example.org")
    service.grant(cpuc_admin, admin, mce, perms.ORG_ADMIN)
    req = service.request_access(make_user("new@example.org"), mce, ["claims.read"])
    assert _queue(cpuc_admin) == [] and len(_queue(admin)) == 1
    with pytest.raises(AccessDenied, match="Only MCE's administrators"):
        service.decide_request(cpuc_admin, req, True)
    with pytest.raises(AccessDenied, match="Only MCE's administrators"):
        service.grant(cpuc_admin, make_user("other@example.org"), mce, "claims.read")


@pytest.mark.django_db
def test_the_statewide_pseudo_pa_takes_no_requests(make_user, cpuc_admin):
    sw = service.create_organisation(cpuc_admin, code="SW", name="Statewide", kind="pa", pa_type="IOU")
    with pytest.raises(AccessDenied, match="does not take access requests"):
        service.request_access(make_user("new@example.org"), sw, ["claims.read"])
