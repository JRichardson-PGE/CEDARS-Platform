"""An organization's pages (UI review 3, U7): Members, Access requests, Recertification, API tokens and Details under one sub-navigation;
Give access (one person picked from the members or anyone with an account, several roles or permissions, one reason, one send); and
recertification grouped by member with "Certify all shown" and "Certify all of this member's"."""

from datetime import timedelta

import pytest
from django.utils import timezone

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service, tokens
from cedars.services.accounts.models import Grant, RoleTemplate
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

from .conftest import page_only

PAGES = ["", "requests/", "recertification/", "tokens/", "details/"]


@pytest.fixture
def analyst(make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org", "Ana Analyst")
    service.grant(pge_admin, u, pge, "claims.read")
    return u


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


def _nav(html):
    return html.split('<nav class="subnav org-pages"', 1)[1].split("</nav>", 1)[0]


def test_each_page_has_the_sub_navigation_and_a_breadcrumb(signed_in, make_user, pge, pge_admin):
    service.request_access(make_user("new@example.org"), pge, ["claims.read"])
    c = signed_in(pge_admin)
    for page in PAGES:
        html = page_only(c.get(f"/admin/organizations/PGE/{page}").content.decode())
        nav = _nav(html)
        for href in PAGES:
            assert f'href="/admin/organizations/PGE/{href}"' in nav, (page, href)
        assert f'href="/admin/organizations/PGE/{page}" aria-current="page"' in nav
        assert 'Access requests <span class="count">(1<span class="sr-only"> waiting</span>)</span>' in nav
        assert '<a href="/admin/organizations/">Organizations</a>' in html.split('aria-label="Breadcrumb"', 1)[1].split("</nav>", 1)[0]
    assert c.get("/admin/organizations/PGE/").content.decode().count("<form") < 10  # the members page no longer carries every form


def test_a_cpuc_administrator_viewing_sees_no_tokens_page(signed_in, cpuc_admin, pge, pge_admin):
    c = signed_in(cpuc_admin)
    assert "/tokens/" not in _nav(c.get("/admin/organizations/PGE/").content.decode())
    assert c.get("/admin/organizations/PGE/tokens/").status_code == 404


def test_an_old_code_finds_the_same_page(signed_in, cpuc_admin, pge):
    service.rename_organisation(cpuc_admin, pge, code="PGE2", name=pge.name, effective=timezone.localdate(), reason="test")
    r = signed_in(cpuc_admin).get("/admin/organizations/PGE/recertification/")
    assert r.status_code == 302 and r["Location"] == "/admin/organizations/PGE2/recertification/"


def test_give_access_picks_a_member_and_gives_roles_and_permissions_at_once(signed_in, pge, pge_admin, analyst, filer_role):
    c = signed_in(pge_admin)
    html = page_only(c.get("/admin/organizations/PGE/").content.decode())
    give = html.split('id="give-h"', 1)[1].split("</form>", 1)[0]
    assert 'id="id_give-person"' in give and 'list="id_give-person-list"' in give
    assert '<option value="analyst@pge.example.org">Ana Analyst</option>' in give  # the members, to pick from
    assert f'name="give-roles" value="{filer_role.pk}"' in give and "gives Claims: read, Claims: submit" in give
    assert 'name="give-permissions" value="budgets.submit"' in give
    r = c.post(
        "/admin/organizations/PGE/grant",
        {"give-person": "Ana Analyst", "give-roles": str(filer_role.pk), "give-permissions": ["budgets.submit"], "give-reason": "2028"},
        follow=True,
    )
    assert "Gave analyst@pge.example.org: Budgets: submit, Claims: submit. Already held: Claims: read." in r.content.decode()
    held = {g.permission: g for g in Grant.objects.filter(membership__user=analyst, status="active")}
    assert set(held) == {"claims.read", "claims.submit", "budgets.submit"}
    assert held["claims.submit"].role_template == filer_role and held["budgets.submit"].role_template is None
    assert held["claims.submit"].grant_reason == held["budgets.submit"].grant_reason == "2028"
    applied = AuditEvent.objects.get(action="role_template.applied")
    assert applied.after["granted"] == ["claims.submit"] and applied.after["already_held"] == ["claims.read"]


def test_give_access_adds_someone_new_by_their_email(signed_in, make_user, pge, pge_admin):
    newcomer = make_user("newcomer@example.org")
    r = signed_in(pge_admin).post(
        "/admin/organizations/PGE/grant", {"give-person": "NEWCOMER@example.org", "give-permissions": ["claims.read"]}
    )
    assert r.status_code == 302 and service.effective_permissions(newcomer, pge) == {"claims.read"}


def test_give_access_explains_what_is_missing(signed_in, pge, pge_admin, analyst):
    c = signed_in(pge_admin)
    r = c.post("/admin/organizations/PGE/grant", {"give-person": "analyst@pge.example.org"}, follow=True)
    assert "Choose a role or at least one permission." in r.content.decode()
    r = c.post("/admin/organizations/PGE/grant", {"give-person": "Ana Analys", "give-permissions": ["claims.read"]}, follow=True)
    html = r.content.decode()
    assert "No member is “Ana Analys”. Did you mean analyst@pge.example.org (Ana Analyst)?" in html
    assert "To add someone new, type the email address of their account" in html


def test_give_access_gives_all_or_nothing(make_user, pge, pge_admin, analyst):
    with pytest.raises(AccessDenied, match="exists only in the CPUC organization"):
        service.give_access(pge_admin, analyst, pge, permissions=["budgets.submit", perms.SYSTEM_ADMIN])
    assert not Grant.objects.filter(membership__user=analyst, permission="budgets.submit").exists()
    with pytest.raises(AccessDenied, match="already has every permission chosen"):
        service.give_access(pge_admin, analyst, pge, permissions=["claims.read"])


def test_a_retired_role_is_not_given(pge, pge_admin, analyst, filer_role):
    RoleTemplate.objects.filter(pk=filer_role.pk).update(active=False)
    with pytest.raises(AccessDenied, match="is retired"):
        service.give_access(pge_admin, analyst, pge, templates=[filer_role])


def test_the_first_administrator_is_assigned_on_the_members_page(signed_in, make_user, cpuc_admin, cpuc):
    sce = service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    first = make_user("first@sce.example.org")
    c = signed_in(cpuc_admin)
    html = page_only(c.get("/admin/organizations/SCE/").content.decode())
    assert "Assign the first administrator" in html and 'id="give-h"' not in html
    c.post("/admin/organizations/SCE/grant", {"give-person": first.email, "give-permissions": perms.ORG_ADMIN, "give-reason": "named"})
    assert service.is_org_admin(first, sce)


def _recertification(pge, pge_admin, make_user):
    ana = make_user("ana@pge.example.org", "Ana")
    ben = make_user("ben@pge.example.org", "Ben")
    for u, codes in ((ana, ["claims.read", "claims.submit", "budgets.read"]), (ben, ["claims.read", "budgets.read"])):
        for code in codes:
            service.grant(pge_admin, u, pge, code)
    campaign = service.open_recertification(pge_admin, pge, timezone.now() + timedelta(days=30))
    return campaign, ana, ben


def test_recertification_groups_grants_by_member(signed_in, make_user, pge, pge_admin):
    campaign, ana, ben = _recertification(pge, pge_admin, make_user)
    html = page_only(signed_in(pge_admin).get("/admin/organizations/PGE/recertification/").content.decode())
    assert html.count('<form method="post" action="/admin/organizations/PGE/recertification/certify" id="certify"') == 1
    assert "Certify all shown <span" in html and html.count('name="shown"') == campaign.items.count()
    assert "Certify all 3 of Ana's" in html and "Certify all 2 of Ben's" in html
    assert html.index("ana@pge.example.org") < html.index("ben@pge.example.org")
    assert 'form="certify" name="item"' in html  # each row's Certify joins the one form


def test_certify_one_a_members_or_all_shown(signed_in, make_user, pge, pge_admin):
    campaign, ana, ben = _recertification(pge, pge_admin, make_user)
    c = signed_in(pge_admin)
    url = "/admin/organizations/PGE/recertification/certify"
    one = campaign.items.get(grant__membership__user=ben, grant__permission="claims.read")
    r = c.post(url, {"item": one.pk}, follow=True)
    assert "Certified Claims: read for ben@pge.example.org." in r.content.decode()
    ana_m = ana.memberships.get(organisation=pge)
    r = c.post(url, {"member": ana_m.pk}, follow=True)
    assert "Certified 3 grants for ana@pge.example.org." in r.content.decode()
    assert not campaign.items.filter(grant__membership=ana_m, decision="").exists()
    # an exception removed first, then the rest certified at once
    left = campaign.items.filter(decision="")
    shown = [str(i.pk) for i in left] + [str(one.pk)]  # one already reviewed: left as it is
    removed = left.get(grant__membership__user=ben)
    c.post(f"/admin/recertification/{removed.pk}", {"decision": "remove", "reason": "left the team"})
    r = c.post(url, {"all": "1", "shown": shown}, follow=True)
    assert "Certified Organization administrator for admin@pge.example.org." in r.content.decode()  # all that was left
    assert not campaign.items.filter(decision="").exists()
    r = c.post(url, {"all": "1", "shown": shown}, follow=True)
    assert "Nothing left to certify" in r.content.decode()
    removed.refresh_from_db()
    assert removed.decision == "removed"


def test_only_its_administrators_certify(signed_in, make_user, cpuc_admin, pge, pge_admin):
    campaign, ana, ben = _recertification(pge, pge_admin, make_user)
    item = campaign.items.first()
    signed_in(cpuc_admin).post("/admin/organizations/PGE/recertification/certify", {"item": item.pk})
    item.refresh_from_db()
    assert item.decision == ""


def test_the_tokens_and_details_pages(signed_in, cpuc_admin, pge, pge_admin, analyst):
    token, _ = tokens.create_token(analyst, pge, name="claims system", scopes=["calculations"], days=30)
    c = signed_in(pge_admin)
    assert f"cedars_{token.prefix}" in c.get("/admin/organizations/PGE/tokens/").content.decode()
    assert f"cedars_{token.prefix}" not in c.get("/admin/organizations/PGE/").content.decode()
    details = c.get("/admin/organizations/PGE/details/").content.decode()
    assert '<h2 id="details-h">Details</h2>' in details and '<h2 id="codes-h">Codes</h2>' in details
