"""Who may open a page, so a link to it is offered only to people it would open for (UI review 3, U4).

The Help guides name administrators' pages. A reader who cannot open one sees its name and who can open it instead of a link that ends
at "Page not available" ({% page_link %}, cedars/core/templatetags/ui.py).

Two sources, the more specific first:

- A page outside the header's menus whose own check is finer than a role (File a document needs the acting organization to file
  documents; the aging review is for the CPUC's documents staff) is registered by the service or module that owns it, in its
  AppConfig.ready, with its check and who it is for in words: `register("documents:new", check, "people who file documents for
  their organization")`. Core never imports a module, so the check comes to it.
- Every other page: the header's menus already say who sees each item (`core/nav.py`, `Item.who`), and a page in none of them is
  open to everyone.

A signed-out reader is offered a link to a page any signed-in account or member can open (signing in brings them back to it), and
not one for administrators or one with a check of its own."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import nav


@dataclass(frozen=True)
class Rule:
    check: Callable  # check(request) -> bool, for a signed-in visitor
    who: str  # who it is for, in words: "people who file documents for their organization"


_rules: dict[str, Rule] = {}

# Who sees a header item (nav.Item.who), in words. "" and "anon" never restrict a page.
WHO_WORDS = {
    "user": "anyone signed in",
    "member": "members of an organization",
    "guest": "accounts that are members of no organization",
    "sysadmin": "CPUC system administrators",
    "org_admin": "organization administrators",
    "steward": "CPUC data stewards",
    "approver": "CPUC approvers and their delegates",
}
# A signed-out reader could open these once signed in, with an ordinary account.
_SIGNED_IN = {"", "anon", "user", "member", "guest"}


def register(url_name: str, check: Callable, who: str) -> None:
    _rules[url_name] = Rule(check, who)


def rule(url_name: str) -> Rule | None:
    return _rules.get(url_name)


def _item(url_name: str) -> nav.Item | None:
    return next((i for m in nav.MENUS for i in m.items if i.url_name == url_name), None)


def can_open(request, url_name: str) -> bool:
    """Whether the page would open for the visitor (for a signed-out visitor: would open once they sign in, with an ordinary account)."""
    user = getattr(request, "user", None)
    signed_in = user is not None and user.is_authenticated
    registered = _rules.get(url_name)
    if registered is not None:
        return signed_in and bool(registered.check(request))
    item = _item(url_name)
    if item is None:
        return True
    if not signed_in:
        return any(w in _SIGNED_IN for w in item.who)
    return any(w in nav.roles(request) for w in item.who)


def who_opens(url_name: str) -> str:
    """Who the page is for, in words ("CPUC system administrators"); "" for a page open to everyone."""
    registered = _rules.get(url_name)
    if registered is not None:
        return registered.who
    item = _item(url_name)
    if item is None:
        return ""
    words = [WHO_WORDS[w] for w in item.who if w in WHO_WORDS]
    return " and ".join(words)
