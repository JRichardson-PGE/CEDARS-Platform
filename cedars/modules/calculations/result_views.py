"""Saved Results views (as ReCET's own Results page has them): a person names the columns they ticked and picks them again later on
Results or Compare, for any run. Columns are kept as Results column keys; a key a run does not have is skipped when the view is shown.
Every change is an audit event."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from cedars.services import audit
from cedars.services.accounts.service import AccessDenied

from .models import ResultView

MAX_NAME = 60
MAX_COLUMNS = 300
MAX_VIEWS = 100


def visible(user):
    return ResultView.objects.filter(owner=user)


def get(user, pk) -> ResultView | None:
    try:
        return visible(user).filter(pk=int(pk)).first()
    except (TypeError, ValueError):
        return None


def _snapshot(v: ResultView) -> dict:
    return {"name": v.name, "columns": list(v.columns)}


def _record(action, view, actor, before=None, after=None):
    audit.record(action, obj=view, before=before, after=after, actor=actor)


@transaction.atomic
def save(actor, name: str, columns) -> ResultView:
    """Save the actor's view under `name`; a view of theirs with that name (case ignored) gets these columns instead."""
    name = " ".join(str(name or "").split())
    if not name:
        raise AccessDenied("Give the view a name.")
    if len(name) > MAX_NAME:
        raise AccessDenied(f"A view name has at most {MAX_NAME} characters.")
    cols = list(dict.fromkeys(str(c).strip() for c in (columns or []) if str(c).strip()))
    if not cols:
        raise AccessDenied("Pick at least one column.")
    if len(cols) > MAX_COLUMNS or any(len(c) > 120 for c in cols):
        raise AccessDenied("Too many columns for one view.")
    view = visible(actor).filter(name__iexact=name).select_for_update().first()
    if view is None:
        if visible(actor).count() >= MAX_VIEWS:
            raise AccessDenied(f"You have {MAX_VIEWS} views; delete one first.")
        view, before = ResultView(owner=actor), None
    else:
        before = _snapshot(view)
    view.name, view.columns = name, cols
    try:
        with transaction.atomic():
            view.save()
    except IntegrityError:
        raise AccessDenied(f'There is already a view named "{name}".') from None
    _record("result_view.changed" if before else "result_view.added", view, actor, before, _snapshot(view))
    return view


@transaction.atomic
def delete(actor, view: ResultView) -> None:
    if view.owner_id != actor.pk:
        raise AccessDenied("This is someone else's view.")
    _record("result_view.deleted", view, actor, before=_snapshot(view))
    view.delete()
