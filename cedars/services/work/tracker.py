"""Signed events from the developers' tracker (a GitHub webhook on the repository named by CEDARS_ISSUE_REPOSITORY, signed
with CEDARS_ISSUE_WEBHOOK_SECRET). Two things are taken, and nothing else is read or kept:

- a pull request that names a work item, by its number (W-12) or by its issue (#34), in its title or description: the item's
  page then shows the pull request as open, merged or closed without merging;
- a work item's issue closing as done, or reopening: closing marks an item of an approved release plan built, as a backlog
  manager's Mark built does, and reopening takes that mark back.

An event is taken only with a good signature, for that repository. An event older than what is already kept changes nothing,
so a delivery sent twice or out of order does no harm. No words from the tracker are stored."""

import hashlib
import hmac
import re

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime

from . import service
from .models import TrackerPull, WorkItem

NAMED = re.compile(r"(?<![\w-])W-(\d{1,9})(?!\w)", re.IGNORECASE)
ISSUE_REF = re.compile(r"(?<![\w/])#(\d{1,9})(?!\w)")


def enabled() -> bool:
    return bool(getattr(settings, "CEDARS_ISSUE_WEBHOOK_SECRET", "") and getattr(settings, "CEDARS_ISSUE_REPOSITORY", ""))


def signed(body: bytes, signature: str) -> bool:
    """Whether the body carries the tracker's signature (X-Hub-Signature-256: sha256=<HMAC of the body with the secret>)."""
    secret = getattr(settings, "CEDARS_ISSUE_WEBHOOK_SECRET", "")
    if not secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:].strip().lower())


def ours(payload: dict) -> bool:
    name = (payload.get("repository") or {}).get("full_name") or ""
    return isinstance(name, str) and name.lower() == settings.CEDARS_ISSUE_REPOSITORY.lower()


def _issue_address(number) -> str:
    return f"https://github.com/{settings.CEDARS_ISSUE_REPOSITORY}/issues/{number}"


def _items_named(text: str) -> list[WorkItem]:
    """The work items a pull request's words name: W-12, or #34 where 34 is an item's issue."""
    pks = {int(n) for n in NAMED.findall(text)}
    addresses = [_issue_address(n) for n in set(ISSUE_REF.findall(text))]
    found = {it.pk: it for it in WorkItem.objects.filter(pk__in=pks)}
    if addresses:
        found.update({it.pk: it for it in WorkItem.objects.filter(issue_url__iregex=_any_of(addresses))})
    return [found[pk] for pk in sorted(found)]


def _any_of(addresses: list[str]) -> str:
    return "^(" + "|".join(re.escape(a) for a in addresses) + ")/?$"


@transaction.atomic
def take(event: str, payload: dict) -> str:
    """Act on one signed event for our repository. Returns what was done, in a few words (the tracker shows it beside the
    delivery)."""
    if event == "ping":
        return "Ready."
    if event == "issues":
        issue = payload.get("issue") or {}
        action, when = payload.get("action"), parse_datetime(str(issue.get("updated_at") or ""))
        if action not in ("closed", "reopened") or when is None or "pull_request" in issue:
            return "Nothing to do."
        if action == "closed" and issue.get("state_reason") not in (None, "completed"):
            return "Closed, and not as done: nothing changed."
        items = WorkItem.objects.filter(issue_url__iregex=_any_of([_issue_address(issue.get("number"))])).order_by("pk")
        done = [it.number for it in items if service.issue_closed(it, action == "closed", when)]
        return f"Issue {action}: {', '.join(done)}." if done else "No work item has that issue."
    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        when = parse_datetime(str(pull.get("updated_at") or ""))
        number, url = pull.get("number"), str(pull.get("html_url") or "")
        if when is None or not isinstance(number, int) or not url.startswith("https://") or len(url) > 300:
            return "Nothing to do."
        if pull.get("merged"):
            state = TrackerPull.State.MERGED
        elif pull.get("state") == "closed":
            state = TrackerPull.State.CLOSED
        else:
            state = TrackerPull.State.OPEN
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        items = _items_named(text)
        done = [it.number for it in items if service.pull_seen(it, number=number, url=url, state=state, when=when)]
        # a pull request whose words no longer name an item keeps its last state there: a merged change stays merged
        return f"Pull request #{number} {state}: {', '.join(done)}." if done else "It names no work item."
    return "Nothing to do."
