"""Help never leads to an error page (UI review 3, U4): every link in every user guide, followed as each kind of account, opens a page
(or, signed out, the sign-in page that brings the reader back to it). A guide names an administrators' page as a link only for a
reader it opens for; anyone else reads its name and who it is for (cedars/core/page_access.py, the {% page_link %} tag)."""

import io
import json
import re
from pathlib import Path

import pytest
from django.core.management import call_command

from cedars.core import help as site_help
from cedars.core import page_access
from cedars.services.accounts import service
from cedars.services.dictionary import service as dictionary

from .conftest import page_only, sign_in

HREF = re.compile(r'<a\b[^>]*\bhref="(/[^"#]*)')
ACCOUNTS = [
    "signed-out",
    "cpuc-system-admin",
    "cpuc-approver",
    "iou-filer",
    "ren-filer",
    "cca-filer",
    "org-admin",
    "analyst",
    "no-organization",
]


def _main(html: str) -> str:
    """The guide itself: the page's main part, without the header and footer every page shares (each has its own checks)."""
    return page_only(html).split("<main", 1)[1].split("</main>", 1)[0]


@pytest.fixture
def accounts(db, cpuc_admin, pge, pge_admin, make_user):
    """The kinds of people the tours are walked as (tests/a11y/test_axe.py ROLES), the filers made as the demo site makes them."""
    from cedars.services.accounts.models import User

    service.create_organisation(None, code="BAYREN", name="Bay Area Regional Energy Network", kind="pa", pa_type="REN")
    service.create_organisation(None, code="SOCALREN", name="Southern California Regional Energy Network", kind="pa", pa_type="REN")
    service.create_organisation(None, code="MCE", name="Marin Clean Energy", kind="pa", pa_type="CCA")
    dictionary.load_catalog(  # the data dictionary is a page only once a catalog is loaded
        None, json.loads((Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.94.json").read_text()), platform=True
    )
    made = {"signed-out": None, "cpuc-system-admin": cpuc_admin, "org-admin": pge_admin}
    for key, email, how in (
        ("iou-filer", "filer@pge.example.org", ["--org", "PGE"]),
        ("ren-filer", "filer@ren.example.org", ["--pa-type", "REN"]),
        ("cca-filer", "filer@cca.example.org", ["--pa-type", "CCA"]),
    ):
        call_command("demo_account", email, *how, "--password", "correct horse battery staple", stdout=io.StringIO())
        made[key] = User.objects.get(email=email)
    made["analyst"] = make_user("analyst@pge.example.org", "Ana Lyst")
    service.grant(pge_admin, made["analyst"], pge, "claims.submit")
    made["cpuc-approver"] = make_user("approver@cpuc.example.org", "Avery Prover")
    service.grant(cpuc_admin, made["cpuc-approver"], service.cpuc(), "claims.approve")
    made["no-organization"] = make_user("community@example.org")
    return made


@pytest.mark.parametrize("who", ACCOUNTS)
def test_no_guide_link_ends_at_an_error_page(client, accounts, who):
    user = accounts[who]
    if user is not None:
        sign_in(client, user)
    ends: dict[str, str] = {}  # each address once: guides share many links
    dead = []
    for guide in site_help.GUIDES:
        response = client.get(f"/help/{guide.slug}/")
        assert response.status_code == 200, guide.slug
        for href in sorted(set(HREF.findall(_main(response.content.decode())))):
            if href.startswith(("/static/", "/media/")):
                continue
            if href not in ends:
                opened = client.get(href, follow=True)
                final = opened.redirect_chain[-1][0] if opened.redirect_chain else href
                if opened.status_code != 200:
                    ends[href] = f"answered {opened.status_code}"
                elif user is None and "/accounts/login/" in final:
                    ends[href] = ""  # signing in brings them back to it
                elif user is not None and "/accounts/login/" in final:
                    ends[href] = "sent a signed-in account to sign in"
                else:
                    ends[href] = ""
            if ends[href]:
                dead.append(f"{guide.slug}: {href} {ends[href]}")
    assert not dead, f"{who}: Help guide links that end at an error page:\n" + "\n".join(dead)


def test_an_administrators_page_is_named_with_who_it_is_for(client, accounts):
    """A filer reads "Role templates (for CPUC system administrators)"; a system administrator gets the link."""
    sign_in(client, accounts["iou-filer"])
    guide = _main(client.get("/help/administering/").content.decode())
    assert '<span class="page-name">Role templates</span> <span class="who-opens">(for CPUC system administrators)</span>' in guide
    assert 'href="/admin/role-templates/"' not in guide
    # an organization administrator's own pages stay links for one
    sign_in(client, accounts["org-admin"])
    guide = _main(client.get("/help/administering/").content.decode())
    assert 'href="/admin/organizations/"' in guide and 'href="/admin/role-templates/"' not in guide
    sign_in(client, accounts["cpuc-system-admin"])
    guide = _main(client.get("/help/administering/").content.decode())
    assert 'href="/admin/role-templates/"' in guide and "who-opens" not in guide


def test_a_page_with_a_check_of_its_own_follows_it(client, accounts):
    """File a document is a link for a filer whose organization files documents, and named for an analyst who cannot file them."""
    sign_in(client, accounts["iou-filer"])
    assert 'href="/documents/new/"' in _main(client.get("/help/documents/").content.decode())
    sign_in(client, accounts["analyst"])
    guide = _main(client.get("/help/documents/").content.decode())
    assert 'href="/documents/new/"' not in guide
    assert 'File a document</span> <span class="who-opens">(for people who file documents for the organization they act for)' in guide


def test_signed_out_readers_get_links_that_sign_them_in_but_not_administrators_pages(client, accounts):
    guide = _main(client.get("/help/documents/").content.decode())
    assert 'href="/documents/subscriptions/"' in guide  # any signed-in account's page: signing in brings them to it
    assert 'href="/documents/new/"' not in guide
    guide = _main(client.get("/help/administering/").content.decode())
    assert 'href="/admin/health/"' not in guide and "Jobs and health</span>" in guide


def test_every_page_a_guide_names_with_a_check_says_who_it_is_for(accounts):
    for name in ("documents:new", "documents:aging", "impact_profiles:custom_new", "impact_profiles:settings", "refdata:etrm_upload"):
        assert page_access.rule(name) is not None, name
    for name in ("administration:role_templates", "administration:audit", "administration:stewardship", "snapshots:scan_settings"):
        assert page_access.who_opens(name), name
    assert page_access.who_opens("core:help") == ""


def test_page_link_names_only_pages_a_guide_may_link():
    """Every {% page_link %} in a template names a page that exists (a typo would fail the whole guide)."""
    from django.urls import reverse

    root = Path(__file__).parent.parent / "cedars"
    used = {m for p in root.rglob("templates/**/*.html") for m in re.findall(r"\{% page_link '([^']+)'", p.read_text(encoding="utf-8"))}
    assert used, "no page links: the test is looking in the wrong place"
    for name in used:
        reverse(name)
