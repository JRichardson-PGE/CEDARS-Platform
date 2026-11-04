"""The header's menus, built from what the account may do (navigation and help review, D1, Jake 2026-11-04).

One list of menus for everyone, each item naming who sees it; `header(request)` keeps the items the visitor sees and drops a menu left
empty. Who sees an item (`Item.who`, any one of them is enough):

  "anon"      signed out only              "user"      anyone signed in
  "member"    signed in, a member of an organization (files, reads its filings)
  "guest"     signed in, a member of none (a consultant waiting for access, a community account)
  "public"    signed out, or a member of none: the Published data items a member finds under Filings and Reference data instead
  "sysadmin" / "org_admin" / "steward" / "approver" (a CPUC approver of any filing module, or their delegate)

The menu that holds the open page is highlighted, set by the page and not by its address (navigation review D4): each item names the
pages it holds (`Item.pages`, view names, `*` for any ending; its own page always), and the open page's view name picks the item, the
most specific name winning (an exact name, then the longest pattern). A page shared by several menus' work (a filing's records, its
comments) names its item itself: its view sets `request.nav_item` to that item's view name (`belongs_to`). A page in no menu
(`NO_MENU`: your account, help, release notes) highlights none. The search's list of pages (core/search.py) and the front page's tour
(core/help.py) follow these menus."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase

from django.urls import reverse


@dataclass(frozen=True)
class Item:
    label: str
    url_name: str
    who: tuple[str, ...] = ("",)  # "" everyone
    pages: tuple[str, ...] = ()  # the view names of the pages it holds besides its own ("budgets:*": any budgets page)

    def holds(self, view_name: str) -> int:
        """How specifically this item holds the page (0: not at all): its own page beats a pattern, a longer pattern a shorter one."""
        if view_name == self.url_name:
            return 10_000
        return max((len(p.rstrip("*")) + (1_000 if "*" not in p else 0) for p in self.pages if fnmatchcase(view_name, p)), default=0)


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
    Item("Budget change log", "budgets:changes", pages=("budgets:change*",)),
    Item("Claims change log", "claims:changes", pages=("claims:change*",)),
    Item("Monthly report change log", "monthly:changes", pages=("monthly:change*",)),
)

CALENDAR = ("submissions:deadline",)
STUDIES = (
    "documents:stud*",
    "documents:new_study",
    "documents:change_study",
    "documents:aging",
    "documents:add_coverage",
    "documents:remove_coverage",
    "documents:work_order*",
    "documents:add_work_order",
)
VALUE_LISTS = (
    "dictionary:list*",
    "dictionary:start_version",
    "dictionary:draft",
    "dictionary:add_code",
    "dictionary:upload_codes",
    "dictionary:publish_list",
    "dictionary:discard_list_draft",
    "dictionary:code",
    "dictionary:drop_code",
)
ORGANIZATIONS = Item(
    "Organizations",
    "administration:organisations",
    ADMIN,
    pages=(
        "administration:organisation*",
        "administration:grant*",
        "administration:open_recertification",
        "administration:recertify",
        "administration:revoke",
        "administration:reinstate",
        "administration:end_membership",
        "administration:decide_request",
    ),
)
STEWARDSHIP = ("administration:pii_decision", "administration:propose_change", "administration:decide_change")

# Pages in no menu: they highlight none (tests/test_navigation.py holds every other page to an item).
NO_MENU = (
    "core:*",
    "account_*",
    "mfa_*",
    "socialaccount_*",
    "openid_connect_*",
    "administration:account",
    "administration:switch_organisation",
    "administration:request_access",
    "administration:withdraw_request",
    "administration:token_*",
    "feedback:form",
    "releases:index",
    "releases:feed",
    "releases:subscribe",
    "releases:detail",
    "releases:withdraw",
    "limits:*",
    "notifications:*",
    "*-api:*",
    "api-*",
    "admin:*",  # Django's own admin site, for the platform's operators
)
# Pages shared by several menus' work: each names its item itself (`belongs_to`), from the filing it shows.
SHARED = ("submissions:attachment", "submissions:comment", "submissions:correction*", "submissions:record*")

MENUS = (
    Menu(
        "dashboard",
        "Portfolio dashboard",
        link=Item("Portfolio dashboard", "data:dashboards", pages=("data:dashboard", "data:download", "data:metrics_csv")),
        short="Dashboard",
    ),
    Menu("programs", "Programs", link=Item("Programs", "programs:catalog", pages=("programs:*",))),
    _menu(
        "filings",
        "Filings",
        Item("Budgets", "budgets:index", MEMBER, pages=("budgets:*",)),
        Item("Fund shifts", "budgets:shifts", MEMBER, pages=("budgets:shift*", "budgets:operating*")),
        Item("Claims", "claims:index", MEMBER, pages=("claims:*",)),
        Item("Monthly reports", "monthly:index", MEMBER, pages=("monthly:*",)),
        Item("Filing calendar", "submissions:calendar", MEMBER, pages=CALENDAR),
        Item("Check a file before you file it", "rules:check", MEMBER),
        Item("Correction requests", "submissions:reopen_requests", ("approver",), pages=("submissions:decide",)),
        groups=(Group("Change logs", tuple(replace(i, who=MEMBER) for i in CHANGE_LOGS)),),
    ),
    _menu(
        "calculations",
        "Calculations",
        Item("Runs", "calculations:runs", USER, pages=("calculations:*",)),
        Item("New run", "calculations:new", USER, pages=("calculations:input_template",)),
        Item("Edit inputs", "calculations:drafts", USER, pages=("calculations:draft*",)),
        Item("Output templates", "calculations:templates", USER, pages=("calculations:template*",)),
        Item("Impact profiles", "impact_profiles:library", USER, pages=("impact_profiles:*",)),
        Item("Engine documentation", "core:recet_docs", USER, pages=("core:recet_doc",)),
        Item("Engine releases", "calculations:engines", SYSADMIN, pages=("calculations:engine*",)),
    ),
    _menu(
        "published",
        "Published data",
        Item("Record downloads", "data:records", pages=("data:records*", "data:downloads", "data:download_*")),
        Item("Saved views", "data:views", USER, pages=("data:view*",)),
        Item("Documents", "documents:library", pages=("documents:*", "deer_resources:*")),
        Item("Evaluation studies", "documents:studies", pages=STUDIES),
        Item("Data dictionary", "dictionary:index", pages=("dictionary:*",)),
        Item("Statewide allocation factors", "administration:allocations", PUBLIC, pages=("administration:allocation*",)),
        Item("Filing calendar", "submissions:calendar", PUBLIC, pages=CALENDAR),
        groups=(Group("Change logs", tuple(replace(i, who=PUBLIC) for i in CHANGE_LOGS)),),
    ),
    _menu(
        "reference",
        "Reference data",
        Item("Data packs", "refdata:index", MEMBER, pages=("refdata:*",)),
        Item("Cost-test sets", "refdata:cost_tests", USER, pages=("refdata:cost_test*", "refdata:build_*")),
        Item("Statewide allocation factors", "administration:allocations", MEMBER, pages=("administration:allocation*",)),
        Item("Validation rules", "rules:index", MEMBER, pages=("rules:*",)),
        Item("Value lists", "dictionary:lists", MEMBER, pages=VALUE_LISTS),
        Item("CPUC-mandated changes", "mandates:index", MEMBER, pages=("mandates:*",)),
    ),
    Menu(
        "admin",
        "Administration",
        (
            Group(
                "People and access",
                (
                    ORGANIZATIONS,
                    Item("Role templates", "administration:role_templates", SYSADMIN, pages=("administration:role_template*",)),
                    Item("Data stewardship", "administration:stewardship", ("steward",), pages=STEWARDSHIP),
                    Item("Access settings", "administration:settings", SYSADMIN),
                    Item("Audit log", "administration:audit", ADMIN),
                ),
            ),
            Group(
                "Data and publishing",
                (
                    Item("Energy efficiency goals", "administration:goal_sets", SYSADMIN, pages=("administration:goal_*",)),
                    Item("Publication holds", "snapshots:holds", SYSADMIN, pages=("snapshots:*",)),
                    Item("Document accessibility", "accessibility:files", SYSADMIN, pages=("accessibility:*",)),
                    Item("Retention", "retention:index", SYSADMIN),
                ),
            ),
            Group(
                "Communications",
                (
                    Item("Write release notes", "releases:manage", SYSADMIN, pages=("releases:edit",)),
                    Item("Register a CPUC-mandated change", "mandates:manage", SYSADMIN, pages=("mandates:edit",)),
                    Item("Feedback", "feedback:inbox", SYSADMIN, pages=("feedback:detail", "feedback:close", "feedback:reopen")),
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
    current_exact: bool = False  # the open page is that item's own page (aria-current="page"; a page below it is "true")


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
    _mark_current(shown, request)
    return shown


def view_name(request) -> str:
    """The open page's view name, or the item it names for itself (`belongs_to`)."""
    named = getattr(request, "nav_item", "")
    if named:
        return named
    match = getattr(request, "resolver_match", None)
    return match.view_name if match else ""


def belongs_to(request, url_name: str) -> None:
    """For a page shared by several menus' work: highlight the item whose page is url_name (e.g. "claims:index")."""
    request.nav_item = url_name


def holder(name: str, shown_items) -> tuple | None:
    """Of (item, ...) tuples, the one whose item holds the page best; None when none holds it."""
    best, score = None, 0
    for entry in shown_items:
        n = entry[0].holds(name)
        if n > score:
            best, score = entry, n
    return best


def in_no_menu(name: str) -> bool:
    return any(fnmatchcase(name, p) for p in NO_MENU)


def _mark_current(shown: list[Shown], request) -> None:
    name = view_name(request)
    if not name:
        return
    entries = [(item, url, s) for s in shown for item, url in ([s.link] if s.link else [x for _, xs in s.groups for x in xs])]
    best = holder(name, entries)
    if best:
        item, url, s = best
        s.current, s.current_url = True, url
        s.current_exact = (getattr(request, "path", "") or "") == url


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
