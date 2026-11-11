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
(`NO_MENU`: your account, help, release notes) highlights none.

A menu's heading is a link to the menu's own page (`Menu.landing`, Jake 2026-11-06): what the section is for and every page under it
the visitor may open, each with a sentence on what it is for (`Item.about`); the arrow beside the heading opens the dropdown. A long
menu keeps its dropdown short: an item with `in_menu=False` is listed on the landing page only, and the dropdown ends with a link to
it (Administration, Jake 2026-11-06). The search's list of pages (core/search.py) and the front page's tour
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
    about: str = ""  # one sentence on what the page is for, under its link on the menu's landing page
    in_menu: bool = True  # False: listed on the menu's landing page only, to keep a long dropdown short (Administration)

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
    landing: str = ""  # a menu's own page (view name), the heading's link: what the section is for and every page under it
    about: str = ""  # the landing page's opening paragraph
    flat: bool = False  # the dropdown lists its items without the groups' titles (the landing page keeps the groups)

    @property
    def items(self) -> tuple[Item, ...]:
        return (self.link,) if self.link else tuple(i for g in self.groups for i in g.items)


def _menu(key, label, *items: Item, groups: tuple[Group, ...] = (), landing="", about="", short="") -> Menu:
    return Menu(key, label, ((Group("", items),) if items else ()) + groups, landing=landing, about=about, short=short)


ANON, USER, MEMBER, GUEST, PUBLIC = ("anon",), ("user",), ("member",), ("guest",), ("anon", "guest")
SYSADMIN, ADMIN = ("sysadmin",), ("sysadmin", "org_admin")

CHANGE_LOGS = (
    Item(
        "Budget change log", "budgets:changes", pages=("budgets:change*",), about="What changed in each corrected budget filing, and why."
    ),
    Item("Claims change log", "claims:changes", pages=("claims:change*",), about="What changed in each corrected claim, and why."),
    Item(
        "Monthly report change log",
        "monthly:changes",
        pages=("monthly:change*",),
        about="What changed in each corrected monthly report, and why.",
    ),
)
CALENDAR_ABOUT = "What each kind of filing expects, for which period, and when it is due."
ALLOCATION_ABOUT = "How each statewide program's budget, costs and savings are shared among the program administrators."

CALENDAR = ("submissions:deadline", "submissions:deadline_new")
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
    about="The program administrators and other organizations, their members and what each member may do.",
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
    "limits:index",
    "notifications:*",
    "*-api:*",
    "api-*",
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
        Item(
            "Budgets",
            "budgets:index",
            MEMBER,
            pages=("budgets:*",),
            about="Budget and application filings for each program cycle: upload, check, price and confirm one, and see each version "
            "filed.",
        ),
        Item(
            "Fund shifts",
            "budgets:shifts",
            MEMBER,
            pages=("budgets:shift*", "budgets:operating*"),
            about="Funds moved between programs within an approved budget, and the operating budget they add up to.",
        ),
        Item(
            "Claims",
            "claims:index",
            MEMBER,
            pages=("claims:*",),
            about="Each program year's quarterly and annual claims of savings and spending.",
        ),
        Item(
            "Monthly reports",
            "monthly:index",
            MEMBER,
            pages=("monthly:*",),
            about="Each month's spending and commitments, with the year to date's estimated savings.",
        ),
        Item("Filing calendar", "submissions:calendar", MEMBER, pages=CALENDAR, about=CALENDAR_ABOUT),
        Item(
            "Filing sets",
            "submissions:filing_sets",
            SYSADMIN,
            about="Each budget filing, claim year and year of monthly reports set up: edit one, or cancel one of its filings.",
        ),
        Item(
            "Check a file before you file it",
            "rules:check",
            MEMBER,
            about="Check a file against the rules its filing will be checked against, without filing it.",
        ),
        Item(
            "Correction requests",
            "submissions:reopen_requests",
            ("approver",),
            pages=("submissions:decide",),
            about="Requests to correct a filing after its due date, waiting for an approver's decision.",
        ),
        groups=(Group("Change logs", tuple(replace(i, who=MEMBER) for i in CHANGE_LOGS)),),
        landing="core:filings",
        about="Where program administrators file their budgets, claims and monthly reports with the CPUC, and where the CPUC reviews them.",
    ),
    _menu(
        "calculations",
        "Calculations",
        Item(
            "Runs",
            "calculations:runs",
            USER,
            pages=("calculations:*",),
            about="Your runs and those shared with you, with their status and results.",
        ),
        Item(
            "New run",
            "calculations:new",
            USER,
            pages=("calculations:input_template",),
            about="Upload a job and choose the avoided costs, cost-test set and outputs to price it with.",
        ),
        Item(
            "Edit inputs",
            "calculations:drafts",
            USER,
            pages=("calculations:draft*",),
            about="Change a job's measures and program costs in the browser, check them and run them.",
        ),
        Item(
            "Output templates",
            "calculations:templates",
            USER,
            pages=("calculations:template*",),
            about="Saved choices of the columns a run writes, and their order.",
        ),
        Item(
            "Impact profiles",
            "impact_profiles:library",
            USER,
            pages=("impact_profiles:*",),
            about="The library of hourly load shapes measures are priced with, and uploads of new ones.",
        ),
        Item(
            "Engine documentation",
            "core:recet_docs",
            USER,
            pages=("core:recet_doc",),
            about="How the engine calculates each output, and what every input and output field means.",
        ),
        Item(
            "Engine releases",
            "calculations:engines",
            SYSADMIN,
            pages=("calculations:engine*",),
            about="Which engine release runs and filings are priced with, and moving to a new one.",
        ),
        landing="core:calculations",
        about="Cost-effectiveness runs on the ReCET engine, the same calculations filings are priced with: price a job, edit its inputs, "
        "and read how the engine works.",
    ),
    _menu(
        "published",
        "Published data",
        Item(
            "Record downloads",
            "data:records",
            pages=("data:records*", "data:downloads", "data:download_*"),
            about="The records of published filings, filtered by PA, year and program, to read or download.",
        ),
        Item(
            "Saved views",
            "data:views",
            USER,
            pages=("data:view*",),
            about="Dashboard and record pages saved with their choices, to open again or share.",
        ),
        Item(
            "Documents",
            "documents:library",
            pages=("documents:*", "deer_resources:*"),
            about="Reports, decisions and other documents filed with or published by the CPUC.",
        ),
        Item(
            "Evaluation studies",
            "documents:studies",
            pages=STUDIES,
            about="Evaluation, measurement and verification studies, and the programs and years each one covers.",
        ),
        Item(
            "Data dictionary",
            "dictionary:index",
            pages=("dictionary:*",),
            about="Every field CEDARS holds: its name, definition, type and allowed values.",
        ),
        Item(
            "Statewide allocation factors",
            "administration:allocations",
            PUBLIC,
            pages=("administration:allocation*",),
            about=ALLOCATION_ABOUT,
        ),
        Item("Filing calendar", "submissions:calendar", PUBLIC, pages=CALENDAR, about=CALENDAR_ABOUT),
        groups=(Group("Change logs", tuple(replace(i, who=PUBLIC) for i in CHANGE_LOGS)),),
        landing="core:published",
        short="Published",  # with the arrow beside each heading, the header needs the room to stay one row from 1280 px
        about="What CEDARS publishes, open to everyone: the records of confirmed filings, documents and studies, and the definitions to "
        "read them by.",
    ),
    _menu(
        "reference",
        "Reference data",
        Item(
            "Data packs",
            "refdata:index",
            MEMBER,
            pages=("refdata:*",),
            about="Avoided costs, eTRM data, the impact profile library and the other packs, with the version in force for each.",
        ),
        Item(
            "Cost-test sets",
            "refdata:cost_tests",
            USER,
            pages=("refdata:cost_test*", "refdata:build_*"),
            about="The cost tests a run prices, as published sets; build your own for a run of your own.",
        ),
        Item(
            "Statewide allocation factors",
            "administration:allocations",
            MEMBER,
            pages=("administration:allocation*",),
            about=ALLOCATION_ABOUT,
        ),
        Item(
            "Validation rules",
            "rules:index",
            MEMBER,
            pages=("rules:*",),
            about="The rules each kind of filing is checked against, with the dates each set is in force.",
        ),
        Item(
            "Value lists",
            "dictionary:lists",
            MEMBER,
            pages=VALUE_LISTS,
            about="The codes a field may take, and the years each one is in use.",
        ),
        Item(
            "CPUC-mandated changes",
            "mandates:index",
            MEMBER,
            pages=("mandates:*",),
            about="Changes the CPUC requires to fields, rules or lists, and when each one takes effect.",
        ),
        landing="core:reference",
        short="Reference",
        about="What runs and filings are priced and checked against: avoided costs and the other data packs, cost-test sets, allocation "
        "factors, validation rules and value lists.",
    ),
    Menu(
        "admin",
        "Administration",
        (
            Group(
                "People and access",
                (
                    ORGANIZATIONS,
                    Item(
                        "Role templates",
                        "administration:role_templates",
                        SYSADMIN,
                        pages=("administration:role_template*",),
                        about="Named bundles of module permissions that organization administrators grant together.",
                        in_menu=False,
                    ),
                    Item(
                        "Data stewardship",
                        "administration:stewardship",
                        ("steward",),
                        pages=STEWARDSHIP,
                        about="Approvals of access to personal information, and changes to the rules that protect it.",
                    ),
                    Item(
                        "Access settings",
                        "administration:settings",
                        SYSADMIN,
                        about="How often grants are recertified, and who may see personal information.",
                        in_menu=False,
                    ),
                    Item(
                        "Change limits",
                        "limits:manage",
                        SYSADMIN,
                        about="Sessions, runs waiting per account and the pre-upload checker's file size: limits the CPUC sets.",
                        in_menu=False,
                    ),
                    Item(
                        "Audit log",
                        "administration:audit",
                        ADMIN,
                        about="Every change made in CEDARS: who made it, for which organization, what changed and why.",
                    ),
                ),
            ),
            Group(
                "Data and publishing",
                (
                    Item(
                        "CPUC goal decisions",
                        "administration:goal_sets",
                        SYSADMIN,
                        pages=("administration:goal_*",),
                        about="The savings goals the CPUC adopted by decision, which the dashboards and front page compare against.",
                        in_menu=False,
                    ),
                    Item(
                        "Publication holds",
                        "snapshots:holds",
                        SYSADMIN,
                        pages=("snapshots:*",),
                        about="Confirmed filings held back from the public data because they may hold personal information.",
                    ),
                    Item(
                        "Document accessibility",
                        "accessibility:files",
                        SYSADMIN,
                        pages=("accessibility:*",),
                        about="The accessibility check of every stored PDF and workbook, and the files that fail it.",
                        in_menu=False,
                    ),
                    Item(
                        "Retention",
                        "retention:index",
                        SYSADMIN,
                        about="How long each class of record is kept, and the legal holds that keep it longer.",
                        in_menu=False,
                    ),
                ),
            ),
            Group(
                "Communications",
                (
                    Item(
                        "Write release notes",
                        "releases:manage",
                        SYSADMIN,
                        pages=("releases:edit",),
                        about="Announce changes to CEDARS before they take effect.",
                        in_menu=False,
                    ),
                    Item(
                        "Site notices",
                        "releases:notices",
                        SYSADMIN,
                        pages=("releases:withdraw_notice",),
                        about="A short dated notice every page shows at its top, such as an outage or an extended deadline.",
                        in_menu=False,
                    ),
                    Item(
                        "Register a CPUC-mandated change",
                        "mandates:manage",
                        SYSADMIN,
                        pages=("mandates:edit",),
                        about="Record a change the CPUC requires and when it takes effect.",
                        in_menu=False,
                    ),
                    Item(
                        "Feedback",
                        "feedback:inbox",
                        SYSADMIN,
                        pages=("feedback:detail", "feedback:close", "feedback:reopen"),
                        about="Messages people sent from a page's Feedback link.",
                    ),
                ),
            ),
            Group(
                "System",
                (
                    Item(
                        "Jobs and health",
                        "administration:health",
                        SYSADMIN,
                        about="Health checks, background jobs, eTRM syncs, the engine's workers and storage.",
                    ),
                ),
            ),
        ),
        landing="core:administration",
        about="Running CEDARS: who has access and what they may do, what is published, what people are told, and the system's health.",
        flat=True,
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
    landing_url: str = ""  # the menu's own page, the heading's link
    about: str = ""
    flat: bool = False

    @property
    def menu_groups(self) -> list[tuple[str, list[tuple[Item, str]]]]:
        """What the dropdown lists: the items it shows (`Item.in_menu`) in their groups, or in one untitled list when the menu is flat."""
        groups = [(t, [x for x in xs if x[0].in_menu]) for t, xs in self.groups]
        groups = [(t, xs) for t, xs in groups if xs]
        if self.flat and groups:
            groups = [("", [x for _, xs in groups for x in xs])]
        return groups

    @property
    def more(self) -> bool:
        """Some of the visitor's pages are listed on the landing page only (the dropdown then ends with a link to it)."""
        return any(not item.in_menu for _, xs in self.groups for item, _ in xs)

    @property
    def heading_current(self) -> str:
        """The heading link's aria-current: "page" on the landing page, "true" on a page whose item the dropdown does not list."""
        if not self.current:
            return ""
        if self.landing_url and self.current_url == self.landing_url:
            return "page" if self.current_exact else "true"
        listed = {url for _, xs in self.menu_groups for _, url in xs}
        return "" if self.current_url in listed else "true"


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
            shown.append(
                Shown(m.key, m.label, m.short, groups, landing_url=reverse(m.landing) if m.landing else "", about=m.about, flat=m.flat)
            )
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
    return name not in landing_names() and any(fnmatchcase(name, p) for p in NO_MENU)


def _mark_current(shown: list[Shown], request) -> None:
    name = view_name(request)
    if not name:
        return
    path = getattr(request, "path", "") or ""
    for m in MENUS:
        if m.landing and name == m.landing:  # a menu's own page
            s = next((s for s in shown if s.key == m.key), None)
            if s is not None:
                s.current, s.current_url, s.current_exact = True, s.landing_url, path == s.landing_url
            return
    entries = [(item, url, s) for s in shown for item, url in ([s.link] if s.link else [x for _, xs in s.groups for x in xs])]
    best = holder(name, entries)
    if best:
        item, url, s = best
        s.current, s.current_url = True, url
        s.current_exact = path == url


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


def landing(key: str) -> Menu:
    """The menu whose landing page this is."""
    return next(m for m in MENUS if m.key == key and m.landing)


def landing_names() -> set[str]:
    return {m.landing for m in MENUS if m.landing}
