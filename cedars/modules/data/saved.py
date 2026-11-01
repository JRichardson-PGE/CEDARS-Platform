"""Saved, shared and published views (BR-07.4), and each person's default columns on the records page.

A saved view is a page and its query string: a dashboard page's GET parameters, or the records page's (filters and the column choice).
It keeps no release, so it opens on the data published now ("Given a saved query, when the user returns, then it reruns on current
data"). Its owner names it and may share it with named people (any active account, told by notification) and with the active members
of one organization they belong to. A CPUC system administrator may publish a view for everyone, signed in or not, with a reason; an
owner may ask for that. A published view never changes: it is unpublished first. Every change below is an audit event.

The data module's own records change only through these functions."""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications

from . import query as dq
from .models import RecordColumnDefault, SavedView

RECORDS = "records"
MAX_QUERY = 4000  # characters; every view the pages make is far shorter (column choices are encoded short, records.encode)
MAX_NAME = 80
MAX_DESCRIPTION = 500
# Never part of a saved view: the record page number, a pinned release (a view reruns on current data), the form's token, the tour.
DROPPED = ("page", "release", "csrfmiddlewaretoken", "tour")
FIELDS = ("name", "description", "page", "query", "organisation", "published_at", "published_by", "publish_reason")


class ViewRefused(ValueError):
    """A refused change to a saved view; str(e) is the plain-language reason."""


# --- pages and queries


def pages() -> dict[str, str]:
    """Every page a view can be saved from: the dashboard's pages, then the records page."""
    from . import dashboard

    return {slug: f"Dashboard: {p.title}" for slug, p in dashboard.PAGES.items()} | {RECORDS: "Record downloads"}


def path(page: str) -> str:
    return "/data/records/" if page == RECORDS else f"/data/{page}/"


def url(view: SavedView) -> str:
    """Where the view opens: its page with its query."""
    return path(view.page) + (f"?{view.query}" if view.query else "")


def clean_query(page: str, query: str) -> str:
    """The query string to keep for a page: parameters in their order, blanks and the dropped ones left out. Refused when the page
    is unknown or cannot show it (the page's own parser decides)."""
    from . import dashboard, records

    if page not in pages():
        raise ViewRefused("That page cannot be saved as a view.")
    params = {k: v for k, v in parse_qs(query or "", keep_blank_values=False).items() if k not in DROPPED}
    try:
        if page == RECORDS:
            records.from_params(params)
        else:
            dashboard.parse(dashboard.PAGES[page], params)
    except dq.QueryError as e:
        raise ViewRefused(f"That view cannot be saved: {e}.") from e
    out = urlencode(params, doseq=True)
    if len(out) > MAX_QUERY:
        raise ViewRefused(f"That view's choices are too long to save (more than {MAX_QUERY:,} characters). Choose fewer filters.")
    return out


PARAM_LABELS = {
    "series": "Series",
    "dataset": "Data",
    "filing": "Filing (forecast)",
    "year_from": "From year",
    "year_to": "To year",
    "by": "View by",
    "metric": "Metric",
    "period": "Goals",
    "gross": "Savings basis",
    "lifecycle": "Period",
    "columns": "Columns start from",
    "colgroup": "Column group added",
    "col": "Column added",
    "drop": "Column left out",
}
VALUE_WORDS = {
    "columns": {"key": "the key columns", "all": "every column", "none": "no columns"},
    "gross": {"1": "Gross", "true": "Gross", "on": "Gross"},
    "lifecycle": {"1": "Lifecycle", "true": "Lifecycle", "on": "Lifecycle"},
    "period": {"cycle": "Cycle goals"},
    "dataset": {"claims": "Claimed", "forecast": "Forecast", "monthly": "Monthly reported"},
    "series": {"claims": "Claimed", "forecast": "Forecast"},
}


def describe(view_or_query) -> list[tuple[str, str]]:
    """The view's choices in words, (label, value) in the query's order; the page's defaults apply to everything not listed."""
    query = view_or_query.query if isinstance(view_or_query, SavedView) else view_or_query
    out = []
    for key, values in parse_qs(query or "").items():
        if key in dq.EXCLUSIONS:
            label = dq.EXCLUSIONS[key].label
            words = {"include": "Included", "exclude": "Left out"}
        elif key in dq.DIMENSIONS:
            label, words = dq.DIMENSIONS[key].label, {}
        else:
            label, words = PARAM_LABELS.get(key, key), VALUE_WORDS.get(key, {})
        for v in values:
            out.append((label, words.get(v, v)))
    return out


# --- who sees what


def _orgs(user) -> list:
    return list(access.organisations_of(user)) if user is not None and user.is_authenticated else []


def visible(user):
    """The views a person may open: published ones for everyone; signed in, also their own, those shared with them by name, and those
    shared with an organization they are an active member of."""
    q = Q(published_at__isnull=False)
    if user is not None and user.is_authenticated:
        q |= Q(owner=user) | Q(shared_with=user) | Q(organisation__in=_orgs(user))
    return SavedView.objects.filter(q).distinct()


def can_see(user, view: SavedView) -> bool:
    return visible(user).filter(pk=view.pk).exists() or bool(user and user.is_authenticated and access.is_system_admin(user))


def for_page(user, page: str):
    return visible(user).filter(page=page).select_related("owner")


def shared_with_me(user):
    if user is None or not user.is_authenticated:
        return SavedView.objects.none()
    return (
        SavedView.objects.filter(Q(shared_with=user) | Q(organisation__in=_orgs(user)))
        .exclude(owner=user)
        .distinct()
        .select_related("owner", "organisation")
    )


def waiting_for_publication():
    return SavedView.objects.filter(publish_requested_at__isnull=False, published_at__isnull=True).select_related("owner")


# --- changes, each audited


def _snapshot(view: SavedView) -> dict:
    out = audit.snapshot(view, FIELDS)
    out["shared_with"] = sorted(view.shared_with.values_list("pk", flat=True)) if view.pk else []
    return out


def _owner_only(user, view: SavedView, what: str) -> None:
    if user is None or view.owner_id != user.pk:
        raise AccessDenied(f"Only the person who saved this view can {what}.")


def _not_published(view: SavedView, what: str) -> None:
    if view.published:
        raise ViewRefused(f"This view is published, so it cannot be {what}. Ask a CPUC system administrator to unpublish it first.")


def _name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ViewRefused("Give the view a name.")
    if len(name) > MAX_NAME:
        raise ViewRefused(f"A view's name is at most {MAX_NAME} characters.")
    return name


def _description(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_DESCRIPTION:
        raise ViewRefused(f"A view's description is at most {MAX_DESCRIPTION} characters.")
    return text


def _named(user, name: str, exclude: int | None = None) -> bool:
    return SavedView.objects.filter(owner=user, name__iexact=name).exclude(pk=exclude).exists()


def save(user, *, name: str, page: str, query: str, description: str = "") -> SavedView:
    """A new view of the person's own (a community account may save views too)."""
    if user is None or not user.is_authenticated:
        raise AccessDenied("Sign in to save a view.")
    name, description, query = _name(name), _description(description), clean_query(page, query)
    if SavedView.objects.filter(owner=user).count() >= settings.DATA_SAVED_VIEWS_PER_USER:
        most = settings.DATA_SAVED_VIEWS_PER_USER
        raise ViewRefused(f"You already have {most:,} saved views, the most one person keeps. Delete one first.")
    if _named(user, name):
        raise ViewRefused(f"You already have a view named {name!r}. Choose another name, or replace that view.")
    try:
        with transaction.atomic():
            view = SavedView.objects.create(owner=user, name=name, page=page, query=query, description=description)
    except IntegrityError as e:  # the same name saved twice at once
        raise ViewRefused(f"You already have a view named {name!r}.") from e
    audit.record("data_view.saved", obj=view, after=_snapshot(view))
    return view


def replace(user, view: SavedView, *, query: str) -> SavedView:
    """Save the page's current choices over one of the person's own views of the same page."""
    _owner_only(user, view, "replace it")
    _not_published(view, "replaced")
    before = _snapshot(view)
    view.query = clean_query(view.page, query)
    view.save(update_fields=["query", "updated_at"])
    audit.record("data_view.changed", obj=view, before=before, after=_snapshot(view), reason="replaced with the page's current choices")
    return view


def rename(user, view: SavedView, *, name: str, description: str = "") -> SavedView:
    _owner_only(user, view, "rename it")
    _not_published(view, "renamed")
    name, description = _name(name), _description(description)
    if _named(user, name, exclude=view.pk):
        raise ViewRefused(f"You already have a view named {name!r}.")
    before = _snapshot(view)
    view.name, view.description = name, description
    view.save(update_fields=["name", "description", "updated_at"])
    audit.record("data_view.changed", obj=view, before=before, after=_snapshot(view))
    return view


def delete(user, view: SavedView) -> None:
    _owner_only(user, view, "delete it")
    _not_published(view, "deleted")
    before = _snapshot(view)
    audit.record("data_view.deleted", obj=view, before=before)
    view.delete()


def copy(user, view: SavedView, *, name: str = "") -> SavedView:
    """A copy of a view the person can open (shared with them or published), as a view of their own."""
    if not can_see(user, view):
        raise AccessDenied("That view is not shared with you.")
    base = _name(name or view.name)
    candidate, n = base, 2
    while not name and _named(user, candidate):  # "Emissions view (2)" when the person already has one by that name
        suffix = f" ({n})"
        candidate, n = base[: MAX_NAME - len(suffix)] + suffix, n + 1
    return save(user, name=candidate, page=view.page, query=view.query, description=view.description)


def share_with(user, view: SavedView, email: str) -> SavedView:
    """Share the view with one more person, by their account's email address; they are told."""
    from cedars.services.accounts.models import User

    _owner_only(user, view, "share it")
    person = User.objects.filter(email__iexact=(email or "").strip(), is_active=True).first()
    if person is None:
        raise ViewRefused("No active CEDARS account has that email address. They can register, then you can share it.")
    if person.pk == user.pk:
        raise ViewRefused("The view is already yours.")
    if view.shared_with.filter(pk=person.pk).exists():
        raise ViewRefused(f"It is already shared with {person}.")
    if view.shared_with.count() >= settings.DATA_SAVED_VIEW_SHARES:
        most = settings.DATA_SAVED_VIEW_SHARES
        raise ViewRefused(f"A view can be shared with at most {most:,} people by name. Share it with your organization instead.")
    before = _snapshot(view)
    view.shared_with.add(person)
    audit.record("data_view.shared", obj=view, before=before, after=_snapshot(view), object_repr=f"{view.name} (with {person.email})")
    notifications.notify(
        person,
        f"{user} shared a view with you: {view.name}",
        f"It opens {pages().get(view.page, view.page)} with the choices they saved, on the data published now.",
        f"/data/views/{view.pk}/",
    )
    return view


def unshare(user, view: SavedView, person) -> SavedView:
    _owner_only(user, view, "change who sees it")
    before = _snapshot(view)
    view.shared_with.remove(person)
    audit.record("data_view.unshared", obj=view, before=before, after=_snapshot(view), object_repr=f"{view.name} (with {person.email})")
    return view


def set_organisation(user, view: SavedView, organisation) -> SavedView:
    """Share the view with every active member of one of the owner's organizations, or (None) stop sharing it with one."""
    _owner_only(user, view, "change who sees it")
    if organisation is not None and organisation.pk not in {o.pk for o in _orgs(user)}:
        raise AccessDenied("You can share a view only with an organization you are a member of.")
    if view.organisation_id == (organisation.pk if organisation else None):
        return view
    before = _snapshot(view)
    view.organisation = organisation
    view.save(update_fields=["organisation", "updated_at"])
    action = "data_view.shared" if organisation else "data_view.unshared"
    audit.record(action, obj=view, before=before, after=_snapshot(view))
    return view


def _require_system_admin(user, what: str) -> None:
    if user is None or not user.is_authenticated or not access.is_system_admin(user):
        raise AccessDenied(f"Only a CPUC system administrator can {what}.")


def request_publication(user, view: SavedView, note: str = "") -> SavedView:
    """The owner asks the CPUC to publish the view; the system administrators are told."""
    _owner_only(user, view, "ask for it to be published")
    if view.published:
        raise ViewRefused("This view is already published.")
    before = _snapshot(view)
    view.publish_requested_at, view.publish_request_note = timezone.now(), _description(note)
    view.save(update_fields=["publish_requested_at", "publish_request_note", "updated_at"])
    audit.record("data_view.publication_requested", obj=view, before=before, after=_snapshot(view), reason=view.publish_request_note)
    cpuc = access.cpuc()
    from cedars.services.accounts import permissions as perms

    for admin in access.holders(cpuc, perms.SYSTEM_ADMIN) if cpuc else []:
        notifications.notify(
            admin,
            f"A saved view is waiting to be published: {view.name}",
            view.publish_request_note or "Its owner asked for it to be published on CEDARS for everyone.",
            f"/data/views/{view.pk}/",
            key=f"data_view.publication_requested:{view.pk}:{view.publish_requested_at:%s}",
        )
    return view


def publish(user, view: SavedView, reason: str) -> SavedView:
    """Publish the view for everyone, signed in or not (BR-07.4). The view no longer changes until it is unpublished."""
    _require_system_admin(user, "publish a view")
    if view.published:
        raise ViewRefused("This view is already published.")
    if not (reason or "").strip():
        raise ViewRefused("Say why the view is published; the reason is recorded.")
    before = _snapshot(view)
    view.published_at, view.published_by, view.publish_reason = timezone.now(), user, reason.strip()
    view.publish_requested_at, view.publish_request_note = None, ""
    view.save()
    audit.record("data_view.published", obj=view, before=before, after=_snapshot(view), reason=view.publish_reason)
    if view.owner_id != user.pk:
        notifications.notify(
            view.owner, f"Your view is published: {view.name}", "Everyone can now open it from Saved views.", f"/data/views/{view.pk}/"
        )
    return view


def decline_publication(user, view: SavedView, reason: str) -> SavedView:
    _require_system_admin(user, "decline a view's publication")
    if view.publish_requested_at is None:
        raise ViewRefused("Nobody asked for this view to be published.")
    if not (reason or "").strip():
        raise ViewRefused("Say why it is not published; the owner is told.")
    before = _snapshot(view)
    view.publish_requested_at, view.publish_request_note = None, ""
    view.save(update_fields=["publish_requested_at", "publish_request_note", "updated_at"])
    audit.record("data_view.publication_declined", obj=view, before=before, after=_snapshot(view), reason=reason.strip())
    notifications.notify(view.owner, f"Your view was not published: {view.name}", reason.strip(), f"/data/views/{view.pk}/")
    return view


def unpublish(user, view: SavedView, reason: str) -> SavedView:
    _require_system_admin(user, "unpublish a view")
    if not view.published:
        raise ViewRefused("This view is not published.")
    if not (reason or "").strip():
        raise ViewRefused("Say why the view is unpublished; the reason is recorded.")
    before = _snapshot(view)
    view.published_at, view.published_by, view.publish_reason = None, None, ""
    view.save()
    audit.record("data_view.unpublished", obj=view, before=before, after=_snapshot(view), reason=reason.strip())
    if view.owner_id != user.pk:
        notifications.notify(view.owner, f"Your view is no longer published: {view.name}", reason.strip(), f"/data/views/{view.pk}/")
    return view


HISTORY_WORDS = {
    "data_view.saved": "saved",
    "data_view.changed": "changed",
    "data_view.shared": "shared",
    "data_view.unshared": "sharing stopped",
    "data_view.publication_requested": "asked to publish",
    "data_view.publication_declined": "publication declined",
    "data_view.published": "published",
    "data_view.unpublished": "unpublished",
}


def history(view: SavedView) -> list[tuple]:
    """(when, what in words, who, reason), oldest first."""
    return [
        (e.at, HISTORY_WORDS.get(e.action, e.action), e.actor or e.actor_label, e.reason)
        for e in audit.changes(view).select_related("actor")
    ]


# --- each person's default columns on the records page


def default_columns(user) -> list[str] | None:
    if user is None or not user.is_authenticated:
        return None
    row = RecordColumnDefault.objects.filter(user=user).first()
    return list(row.columns) if row else None


def set_default_columns(user, columns: list[str]) -> RecordColumnDefault:
    if user is None or not user.is_authenticated:
        raise AccessDenied("Sign in to keep default columns.")
    columns = [c for c in dict.fromkeys(str(c).strip() for c in columns) if c]
    if not columns:
        raise ViewRefused("Choose at least one column before making them your default.")
    row = RecordColumnDefault.objects.filter(user=user).first()
    before = {"columns": row.columns} if row else None
    row, _ = RecordColumnDefault.objects.update_or_create(user=user, defaults={"columns": columns})
    audit.record(
        "data_view.default_columns_set", obj=row, before=before, after={"columns": columns}, object_repr=f"{user.email}'s default columns"
    )
    return row


def clear_default_columns(user) -> None:
    row = RecordColumnDefault.objects.filter(user=user).first() if user is not None and user.is_authenticated else None
    if row is None:
        return
    audit.record(
        "data_view.default_columns_cleared", obj=row, before={"columns": row.columns}, object_repr=f"{user.email}'s default columns"
    )
    row.delete()
