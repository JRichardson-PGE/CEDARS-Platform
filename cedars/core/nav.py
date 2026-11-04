"""The header's menus, built from what the account may do (navigation and help review, D1, Jake 2026-11-04).

One list of menus for everyone, each item naming who sees it; `header(request)` keeps the items the visitor sees and drops a menu left
empty. Who sees an item (`Item.who`, any one of them is enough):

  "anon"      signed out only              "user"      anyone signed in
  "member"    signed in, a member of an organization (files, reads its filings)
  "guest"     signed in, a member of none (a consultant waiting for access, a community account)
  "public"    signed out, or a member of none: the Published data items a member finds under Filings and Reference data instead
  "sysadmin" / "org_admin" / "steward" / "approver" (a CPUC approver of any filing module, or their delegate)

The menu that holds the open page is highlighted, set by the items' own `match` prefixes (the longest match wins), not by a list of URL
prefixes per menu: the engine documentation is Calculations', at its old /help/recet/ address too (it redirects). The search's list of pages
(core/search.py) and the front page's tour (core/help.py) follow these menus."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import reverse


@dataclass(frozen=True)
class Item:
    label: str
    url_name: str
    who: tuple[str, ...] = ("",)  # "" everyone
    match: tuple[str, ...] = ()  # the paths (prefixes) of the pages it holds; blank = its own URL and below

    def paths(self) -> tuple[str, ...]:
        return self.match or (reverse(self.url_name),)


@dataclass(frozen=True)
class Group:
    title: str  # "" for a menu's ungrouped items
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Menu:
    key: str  # its data-tour target is nav-<key>
    label: str
    groups: tuple[Group, ...] = ()
    link: Item | None = None  # a plain link instead of a menu
    short: str = ""  # the label shown where the header is tight (861-1600 px wide); the full label stays its accessible name

    @property
    def items(self) -> tuple[Item, ...]:
        return (self.link,) if self.link else tuple(i for g in self.groups for i in g.items)


def _menu(key, label, *items: Item, groups: tuple[Group, ...] = ()) -> Menu:
    return Menu(key, label, ((Group("", items),) if items else ()) + groups)


ANON, USER, MEMBER, GUEST, PUBLIC = ("anon",), ("user",), ("member",), ("guest",), ("anon", "guest")
SYSADMIN, ADMIN = ("sysadmin",), ("sysadmin", "org_admin")

CHANGE_LOGS = (
    Item("Budget change log", "budgets:changes"),
    Item("Claims change log", "claims:changes"),
    Item("Monthly report change log", "monthly:changes"),
)

MENUS = (
    Menu("dashboard", "Portfolio dashboard", link=Item("Portfolio dashboard", "data:dashboards", match=("/data/",)), short="Dashboard"),
    Menu("programs", "Programs", link=Item("Programs", "programs:catalog")),
    _menu(
        "filings",
        "Filings",
        Item("Budgets", "budgets:index", MEMBER),
        Item("Fund shifts", "budgets:shifts", MEMBER),
        Item("Claims", "claims:index", MEMBER),
        Item("Monthly reports", "monthly:index", MEMBER),
        Item("Filing calendar", "submissions:calendar", MEMBER, match=("/filings/",)),
        Item("Check a file before you file it", "rules:check", MEMBER),
        Item("Correction requests", "submissions:reopen_requests", ("approver",)),
        groups=(Group("Change logs", tuple(Item(i.label, i.url_name, MEMBER) for i in CHANGE_LOGS)),),
    ),
    _menu(
        "calculations",
        "Calculations",
        Item("Runs", "calculations:runs", USER),
        Item("New run", "calculations:new", USER),
        Item("Edit inputs", "calculations:drafts", USER),
        Item("Output templates", "calculations:templates", USER),
        Item("Impact profiles", "impact_profiles:library", USER),
        Item("Engine documentation", "core:recet_docs", USER, match=("/calculations/engine-docs/", "/help/recet/")),
        Item("Engine releases", "calculations:engines", SYSADMIN),
    ),
    _menu(
        "published",
        "Published data",
        Item("Record downloads", "data:records", match=("/data/records/", "/data/downloads/")),
        Item("Saved views", "data:views", USER),
        Item("Documents", "documents:library"),
        Item("Evaluation studies", "documents:studies", match=("/documents/studies/", "/documents/work-orders/")),
        Item("Data dictionary", "dictionary:index"),
        Item("Statewide allocation factors", "administration:allocations", PUBLIC),
        Item("Filing calendar", "submissions:calendar", PUBLIC, match=("/filings/calendar/",)),
        groups=(Group("Change logs", tuple(Item(i.label, i.url_name, PUBLIC) for i in CHANGE_LOGS)),),
    ),
    _menu(
        "reference",
        "Reference data",
        Item("Data packs", "refdata:index", MEMBER),
        Item("Cost-test sets", "refdata:cost_tests", USER),
        Item("Statewide allocation factors", "administration:allocations", MEMBER),
        Item("Validation rules", "rules:index", MEMBER),
        Item("Value lists", "dictionary:lists", MEMBER),
        Item("CPUC-mandated changes", "mandates:index", MEMBER),
    ),
    Menu(
        "admin",
        "Administration",
        (
            Group(
                "People and access",
                (
                    Item("Organizations", "administration:organisations", ADMIN),
                    Item("Role templates", "administration:role_templates", SYSADMIN),
                    Item("Data stewardship", "administration:stewardship", ("steward",)),
                    Item("Access settings", "administration:settings", SYSADMIN),
                    Item("Audit log", "administration:audit", ADMIN),
                ),
            ),
            Group(
                "Data and publishing",
                (
                    Item("Energy efficiency goals", "administration:goal_sets", SYSADMIN),
                    Item("Publication holds", "snapshots:holds", SYSADMIN, match=("/publication/",)),
                    Item("Document accessibility", "accessibility:files", SYSADMIN),
                    Item("Retention", "retention:index", SYSADMIN),
                ),
            ),
            Group(
                "Communications",
                (
                    Item("Write release notes", "releases:manage", SYSADMIN),
                    Item("Register a CPUC-mandated change", "mandates:manage", SYSADMIN),
                    Item("Feedback", "feedback:inbox", SYSADMIN),
                ),
            ),
            Group("System", (Item("Jobs and health", "administration:health", SYSADMIN),)),
        ),
    ),
)


@dataclass
class Shown:
    """A menu as one visitor sees it."""

    key: str
    label: str
    short: str = ""
    groups: list[tuple[str, list[tuple[Item, str]]]] = field(default_factory=list)  # (title, [(item, url)])
    link: tuple[Item, str] | None = None
    current: bool = False
    current_url: str = ""  # the item holding the open page


def roles(request) -> set[str]:
    """The `who` values the visitor has."""
    return roles_of(getattr(request, "user", None), getattr(request, "user_organisations", None))


def roles_of(user, orgs=None) -> set[str]:
    """The `who` values of a user; `orgs` the organizations they are a member of (read when not given)."""
    if user is None or not user.is_authenticated:
        return {"", "anon"}
    from cedars.services.accounts import permissions as perms
    from cedars.services.accounts import service as access

    if orgs is None:
        orgs = list(access.organisations_of(user))
    out = {"", "user", "member" if orgs else "guest"}
    cpuc = access.cpuc()
    mine = access.effective_permissions(user, cpuc)  # read once: system administrator, steward and approver are all CPUC grants
    if perms.SYSTEM_ADMIN in mine:
        out.add("sysadmin")
    if perms.STEWARD in mine:
        out.add("steward")
    if any(access.is_org_admin(user, o) for o in orgs):
        out.add("org_admin")
    if cpuc is not None and is_approver(user, cpuc, mine):
        out.add("approver")
    return out


FILING_MODULES = ("budgets", "claims", "monthly")


def is_approver(user, cpuc, mine: set[str]) -> bool:
    """Holds `<module>.approve` in the CPUC for a filing module, or is an approver's delegate today (submissions.approver_for's rule)."""
    if any(f"{m}.approve" in mine for m in FILING_MODULES):
        return True
    from django.utils import timezone

    from cedars.services.accounts import service as access
    from cedars.services.submissions.models import Delegation

    day = timezone.localdate()
    delegations = Delegation.objects.filter(module__in=FILING_MODULES, delegate=user, starts__lte=day, ends__gte=day).select_related(
        "approver"
    )
    return any(access.has_permission(d.approver, cpuc, f"{d.module}.approve") for d in delegations)


def _sees(item: Item, have: set[str]) -> bool:
    return any(w in have for w in item.who)


def header(request, have: set[str] | None = None) -> list[Shown]:
    """The menus the visitor sees, in order, with the one holding the open page marked."""
    have = roles(request) if have is None else have
    shown = []
    for m in MENUS:
        if m.link is not None:
            if _sees(m.link, have):
                shown.append(Shown(m.key, m.label, m.short, link=(m.link, reverse(m.link.url_name))))
            continue
        groups = [(g.title, [(i, reverse(i.url_name)) for i in g.items if _sees(i, have)]) for g in m.groups]
        groups = [(t, items) for t, items in groups if items]
        if groups:
            shown.append(Shown(m.key, m.label, m.short, groups))
    _mark_current(shown, getattr(request, "path", "") or "")
    return shown


def _mark_current(shown: list[Shown], path: str) -> None:
    best, best_len = None, 0
    for s in shown:
        for item, url in [s.link] if s.link else [x for _, items in s.groups for x in items]:
            for p in item.paths():
                if path.startswith(p) and len(p) > best_len and p != "/":
                    best, best_len = (s, url), len(p)
    if best:
        best[0].current, best[0].current_url = True, best[1]


def items(have: set[str]) -> list[Item]:
    """Every item the roles see, once each (search's list of pages)."""
    out, seen = [], set()
    for m in MENUS:
        for i in m.items:
            if _sees(i, have) and i.url_name not in seen:
                seen.add(i.url_name)
                out.append(i)
    return out


def all_url_names() -> set[str]:
    return {i.url_name for m in MENUS for i in m.items}
