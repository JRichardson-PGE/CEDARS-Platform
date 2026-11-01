"""Saved, shared and published views (BR-07.4) and each person's default columns on the records page."""

import pytest

from cedars.modules.data import saved
from cedars.modules.data.models import RecordColumnDefault, SavedView
from cedars.services.accounts import service as access
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification

from .conftest import page_only, sign_in
from .test_data_records import sce_filer, two_pas  # noqa: F401 - fixtures
from .test_snapshots import filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db
COLUMNS = "dataset=claims&columns=none&col=CEInputID&col=TotalSystemBenefit"


@pytest.fixture
def owner(make_user):
    return make_user("owner@example.org", "Olive Owner")


@pytest.fixture
def other(make_user):
    return make_user("other@example.org", "Otto Other")


def actions(view):
    return [e.action for e in AuditEvent.objects.filter(object_type="data.savedview", object_id=str(view.pk)).order_by("pk")]


def test_a_dashboard_view_is_saved_without_its_release_and_opens_on_current_data(client, owner):
    page = client.get("/data/overview/?by=sector").content.decode()
    assert "Sign in to save this view" in page and 'action="/data/views/save"' not in page

    sign_in(client, owner)
    page = client.get("/data/overview/?by=sector").content.decode()
    assert 'action="/data/views/save"' in page and 'name="query" value="by=sector"' in page
    r = client.post("/data/views/save", {"page": "overview", "query": "by=sector&release=1&page=3&tour=1", "name": "  By   sector "})
    view = SavedView.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/data/views/{view.pk}/"
    assert (view.name, view.page, view.query, view.owner) == ("By sector", "overview", "by=sector", owner)
    assert saved.url(view) == "/data/overview/?by=sector"
    detail = client.get(f"/data/views/{view.pk}/").content.decode()
    assert 'href="/data/overview/?by=sector">Open this view' in detail and "View by: <strong>sector</strong>" in detail
    assert "Only you" in detail and actions(view) == ["data_view.saved"]
    # the page lists it, and so does Saved views
    assert "By sector</a>" in client.get("/data/overview/").content.decode()
    assert "By sector</a>" in client.get("/data/views/").content.decode()


def test_a_view_the_page_cannot_show_or_a_repeated_name_is_refused(client, owner):
    sign_in(client, owner)
    client.post("/data/views/save", {"page": "overview", "query": "by=nonsense", "name": "Bad"})
    client.post("/data/views/save", {"page": "nowhere", "query": "", "name": "Nowhere"})
    assert not SavedView.objects.exists()
    saved.save(owner, name="Mine", page="overview", query="")
    with pytest.raises(saved.ViewRefused, match="already have a view named"):
        saved.save(owner, name="MINE", page="goals", query="")
    with pytest.raises(saved.ViewRefused, match="Give the view a name"):
        saved.save(owner, name=" ", page="goals", query="")


def test_the_number_of_views_and_shares_is_limited(settings, owner, other, make_user):
    settings.DATA_SAVED_VIEWS_PER_USER, settings.DATA_SAVED_VIEW_SHARES = 1, 1
    view = saved.save(owner, name="One", page="overview", query="")
    with pytest.raises(saved.ViewRefused, match="the most one person keeps"):
        saved.save(owner, name="Two", page="overview", query="")
    saved.share_with(owner, view, "other@example.org")
    make_user("third@example.org")
    with pytest.raises(saved.ViewRefused, match="at most 1 people"):
        saved.share_with(owner, view, "third@example.org")
    from cedars.services.releases import limits

    assert {"saved_views", "saved_view_shares"} <= {lim.key for lim in limits.LIMITS}


def test_sharing_with_a_person_tells_them_and_unsharing_hides_it_again(client, owner, other):
    view = saved.save(owner, name="Emissions", page="environmental-impact", query="metric=co2")
    sign_in(client, other)
    assert client.get(f"/data/views/{view.pk}/").status_code == 404  # not shared: not found
    assert client.post(f"/data/views/{view.pk}/", {"action": "copy"}).status_code == 404

    sign_in(client, owner)
    client.post(f"/data/views/{view.pk}/", {"action": "share", "email": "OTHER@example.org"})
    assert list(view.shared_with.all()) == [other]
    n = Notification.objects.get(user=other)
    assert "Olive Owner shared a view with you: Emissions" == n.title and n.link == f"/data/views/{view.pk}/"
    client.post(f"/data/views/{view.pk}/", {"action": "share", "email": "nobody@example.org"})
    assert view.shared_with.count() == 1

    sign_in(client, other)
    page = client.get("/data/views/").content.decode()
    assert "Shared with you" in page and "Emissions</a>" in page and "Olive Owner" in page
    detail = client.get(f"/data/views/{view.pk}/").content.decode()
    assert "Save a copy to your views" in detail and 'value="rename"' not in detail and 'value="delete"' not in detail
    assert "Emissions</a>" in client.get("/data/environmental-impact/").content.decode()
    # only the owner changes it
    client.post(f"/data/views/{view.pk}/", {"action": "rename", "name": "Mine now"})
    client.post(f"/data/views/{view.pk}/", {"action": "delete"})
    view.refresh_from_db()
    assert view.name == "Emissions"

    saved.unshare(owner, view, other)
    assert client.get(f"/data/views/{view.pk}/").status_code == 404
    assert actions(view) == ["data_view.saved", "data_view.shared", "data_view.unshared"]


def test_sharing_with_an_organization_reaches_its_active_members_only(client, cpuc_admin, pge, pge_admin, make_user, other):
    member = make_user("member@pge.example.org")
    access.ensure_membership(pge_admin, member, pge)
    view = saved.save(pge_admin, name="PG&E programs", page="programs", query="pa=PGE")
    with pytest.raises(access.AccessDenied):
        saved.set_organisation(other, view, pge)  # not the owner
    with pytest.raises(access.AccessDenied):
        saved.set_organisation(cpuc_admin, view, access.cpuc())
    saved.set_organisation(pge_admin, view, pge)
    assert saved.can_see(member, view) and not saved.can_see(other, view)
    sign_in(client, member)
    assert "PG&amp;E programs</a>" in client.get("/data/views/").content.decode()
    saved.set_organisation(pge_admin, view, None)
    assert not saved.can_see(member, view)


def test_publication_is_asked_for_decided_by_a_system_administrator_and_freezes_the_view(client, cpuc_admin, owner, other):
    view = saved.save(owner, name="Statewide TSB", page="total-system-benefit", query="by=pa")
    with pytest.raises(access.AccessDenied):
        saved.publish(owner, view, "I like it")
    sign_in(client, owner)
    client.post(f"/data/views/{view.pk}/", {"action": "request", "note": "Useful to everyone"})
    view.refresh_from_db()
    assert view.publish_requested_at and Notification.objects.filter(user=cpuc_admin, title__contains="waiting to be published").exists()

    sign_in(client, cpuc_admin)
    assert "Waiting to be published" in client.get("/data/views/").content.decode()
    client.post(f"/data/views/{view.pk}/", {"action": "publish", "reason": ""})  # a reason is required
    view.refresh_from_db()
    assert not view.published
    client.post(f"/data/views/{view.pk}/", {"action": "publish", "reason": "Asked for by the working group"})
    view.refresh_from_db()
    assert view.published and view.published_by == cpuc_admin and view.publish_requested_at is None
    assert Notification.objects.filter(user=owner, title="Your view is published: Statewide TSB").exists()

    # everyone sees it, signed out too, without who saved it
    client.logout()
    page = client.get("/data/views/").content.decode()
    assert "Statewide TSB</a>" in page and "Olive Owner" not in page_only(page)
    detail = page_only(client.get(f"/data/views/{view.pk}/").content.decode())
    assert "Open this view" in detail and "Olive Owner" not in detail and "owner@example.org" not in detail
    assert "Statewide TSB</a>" in client.get("/data/total-system-benefit/").content.decode()
    # a published view does not change
    for change in (
        lambda: saved.rename(owner, view, name="Changed"),
        lambda: saved.replace(owner, view, query="by=sector"),
        lambda: saved.delete(owner, view),
    ):
        with pytest.raises(saved.ViewRefused, match="published"):
            change()
    # other people can copy it
    copy = saved.copy(other, view)
    assert (copy.owner, copy.query, copy.page, copy.published) == (other, "by=pa", "total-system-benefit", False)
    assert saved.copy(other, view).name == "Statewide TSB (2)"

    saved.unpublish(cpuc_admin, view, "superseded")
    assert not saved.can_see(other, view)
    assert actions(view) == [
        "data_view.saved",
        "data_view.publication_requested",
        "data_view.published",
        "data_view.unpublished",
    ]
    assert AuditEvent.objects.get(object_id=str(view.pk), action="data_view.unpublished").reason == "superseded"


def test_a_declined_request_tells_the_owner(cpuc_admin, owner):
    view = saved.request_publication(owner, saved.save(owner, name="Mine", page="overview", query=""))
    saved.decline_publication(cpuc_admin, view, "Too close to the Overview's default")
    view.refresh_from_db()
    assert view.publish_requested_at is None and not view.published
    assert Notification.objects.get(user=owner).body == "Too close to the Overview's default"


def test_the_owner_renames_replaces_and_deletes_a_view(client, owner):
    view = saved.save(owner, name="Old", page="overview", query="by=pa")
    sign_in(client, owner)
    client.post(f"/data/views/{view.pk}/", {"action": "rename", "name": "New", "description": "PAs side by side"})
    client.post("/data/views/save", {"page": "overview", "query": "by=sector", "replace": view.pk})
    view.refresh_from_db()
    assert (view.name, view.description, view.query) == ("New", "PAs side by side", "by=sector")
    page = client.get(f"/data/views/{view.pk}/").content.decode()
    assert "History" in page and "changed by Olive Owner" in page
    client.post(f"/data/views/{view.pk}/", {"action": "delete"})
    assert not SavedView.objects.exists()
    assert actions(view) == ["data_view.saved", "data_view.changed", "data_view.changed", "data_view.deleted"]


@pytest.mark.usefixtures("two_pas")
def test_records_views_keep_their_columns_and_a_person_keeps_default_columns(client, owner, other):
    sign_in(client, owner)
    page = client.get(f"/data/records/?{COLUMNS}").content.decode()
    assert "Make these 2 columns my default" in page and "Start from the key columns again" not in page
    client.post("/data/records/default-columns", {"query": COLUMNS})
    assert RecordColumnDefault.objects.get(user=owner).columns == ["CEInputID", "TotalSystemBenefit"]

    # a link that names no columns now starts from them, for this person only
    page = client.get("/data/records/?dataset=claims").content.decode()
    assert "These are your default columns." in page and '<th scope="col" class="mono">TotalSystemBenefit</th>' in page
    assert '<th scope="col" class="mono">PrgID</th>' not in page
    assert "Start from the key columns again" in page
    keyed = client.get("/data/records/?dataset=claims&columns=key").content.decode()  # a preset still wins
    assert '<th scope="col" class="mono">PrgID</th>' in keyed and "These are your default columns." not in keyed
    # a records view saved from it names its columns, so it opens the same for someone without those defaults
    client.post(
        "/data/views/save", {"page": "records", "query": "dataset=claims&columns=none&col=CEInputID&col=TotalSystemBenefit", "name": "Two"}
    )
    view = SavedView.objects.get(name="Two")
    assert (
        "col=TotalSystemBenefit" in view.query
        and "Column added: <strong>CEInputID</strong>" in client.get(f"/data/views/{view.pk}/").content.decode()
    )
    assert 'name="query" value="dataset=claims&amp;metric=tsb&amp;columns=none&amp;col=CEInputID&amp;col=TotalSystemBenefit"' in page

    sign_in(client, other)
    assert '<th scope="col" class="mono">PrgID</th>' in client.get("/data/records/?dataset=claims").content.decode()

    sign_in(client, owner)
    client.post("/data/records/default-columns", {"query": "dataset=claims", "clear": "1"})
    assert not RecordColumnDefault.objects.exists()
    assert [e.action for e in AuditEvent.objects.filter(action__startswith="data_view.default_columns").order_by("pk")] == [
        "data_view.default_columns_set",
        "data_view.default_columns_cleared",
    ]


def test_signed_out_people_see_only_published_views(client, owner):
    saved.save(owner, name="Private", page="overview", query="")
    page = client.get("/data/views/").content.decode()
    assert "Private" not in page_only(page) and "Your views" not in page_only(page) and "not published any views yet" in page
    assert client.post("/data/views/save", {"page": "overview", "query": "", "name": "x"}).status_code == 302
    assert SavedView.objects.count() == 1
