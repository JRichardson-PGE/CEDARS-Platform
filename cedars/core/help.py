"""The Help section (Jake, 2026-10-31): user guides, feedback and support, and the guided tour of the front page.

Guides are templates under core/templates/core/help/ (one per entry of GUIDES). Support and feedback come from settings, so no address
is written into the code: CEDARS_SUPPORT_EMAIL (also given on the sign-in pages) and CEDARS_FEEDBACK_URL (a form or survey; blank =
an email to the support address). The tour's steps are data (`tour_steps`): the front page shows them one at a time beside the part of
the page each describes (core/static/core/tour.js), and /help/tour/ lists them all, for anyone without scripts or who prefers to read."""

from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings


@dataclass(frozen=True)
class Guide:
    slug: str
    title: str
    summary: str
    audience: str  # who it is for, in words
    areas: tuple[str, ...] = ()  # the parts of CEDARS it covers (releases.service.AREAS): a release changing one should revise it
    reader: str = "everyone"  # the Help index's group (READERS)

    @property
    def revised(self) -> str:
        """The platform release this guide was last revised for (cedars/core/revisions.py)."""
        from . import revisions

        return revisions.revised(f"guide:{self.slug}")


READERS = [
    ("everyone", "Everyone"),
    ("pa", "Program administrators"),
    ("analyst", "Analysts"),
    ("cpuc", "CPUC staff"),
    ("admin", "Administrators"),
]

GUIDES = [
    Guide(
        "getting-started",
        "Getting started",
        "Creating an account, the second factor, asking an organization for access, acting for an organization, notifications and "
        "API tokens.",
        "Everyone who signs in",
        ("accounts", "api"),
    ),
    Guide(
        "published-data",
        "Finding published data",
        "The portfolio dashboard, record downloads, the document library and the data dictionary. No account needed.",
        "Everyone",
        ("data", "documents", "reference"),
    ),
    Guide(
        "glossary",
        "Glossary",
        "The acronyms, program administrator codes and terms used on CEDARS pages, in filings and in downloads.",
        "Everyone",
    ),
    Guide(
        "accessibility",
        "Keyboard and screen reader use",
        "How the pages work with a keyboard, a screen reader and zoom, and how to report a barrier.",
        "Everyone",
    ),
    Guide(
        "programs",
        "Programs",
        "The program catalog, a program's page and versions, and asking for a change to a program.",
        "Program administrators and CPUC staff",
        ("programs",),
        "pa",
    ),
    Guide(
        "budgets",
        "Budget filings and fund shifts",
        "Uploading, checking, pricing and confirming a budget filing; comparing versions; fund shifts and the operating budget.",
        "Program administrators",
        ("budgets",),
        "pa",
    ),
    Guide(
        "claims",
        "Claims",
        "Quarterly and annual claims: the template, uploads compiled over earlier quarters, withdrawals, personal information and "
        "corrections.",
        "Program administrators",
        ("claims",),
        "pa",
    ),
    Guide(
        "monthly-reports",
        "Monthly reports",
        "What a monthly report holds, how spending is compared with the operating budget, and the 13th-month report.",
        "Program administrators",
        ("monthly",),
        "pa",
    ),
    Guide(
        "correcting-records",
        "Correcting filed records",
        "Changing, adding and deleting single records of a draft filing, partial corrections with a preview, and what is checked and "
        "recorded.",
        "Program administrators",
        ("claims", "budgets", "monthly"),
        "pa",
    ),
    Guide(
        "documents",
        "Documents and EM&V",
        "The document library, filing a document and its versions, comment periods, evaluation studies, work orders and responses to "
        "recommendations.",
        "Program administrators, evaluators and CPUC Energy Division",
        ("documents",),
        "pa",
    ),
    Guide(
        "calculations",
        "Cost-effectiveness runs",
        "Starting a run, reading its results, comparing two runs, editing a small job's inputs, output templates and impact profiles.",
        "Analysts at PAs and the CPUC",
        ("calculations",),
        "analyst",
    ),
    Guide(
        "impact-profiles",
        "Impact profiles",
        "The profile library, custom profiles, deemed load shapes from eTRM measure packages, their checks and the library files the "
        "engine reads.",
        "Analysts and the PAs that lead measure packages",
        ("calculations",),
        "analyst",
    ),
    Guide(
        "reference-data",
        "Reference data and cost-test sets",
        "Data packs (ACCs, eTRM packs, the profile library), statewide allocation factors, and cost-test sets and the set builder.",
        "Analysts, funding PAs and the CPUC's reference data managers",
        ("reference", "calculations"),
        "analyst",
    ),
    Guide(
        "reviewing",
        "Reviewing and approving",
        "My queue, correction requests, program changes, confirming filings, replying to comments, publishing documents, publication "
        "holds and second confirmations.",
        "CPUC approvers, data stewards and the PA staff who confirm filings",
        ("claims", "budgets", "monthly", "programs"),
        "cpuc",
    ),
    Guide(
        "rules-and-lists",
        "Validation rules and value lists",
        "What filings are checked against, checking a file before you file it, comparing rule sets, and keeping the rules, value lists "
        "and fields in versions.",
        "Filers, and the CPUC staff who keep the rules and lists",
        ("reference",),
        "cpuc",
    ),
    Guide(
        "releases",
        "Release notes and mandated changes",
        "How changes are announced before they take effect, the limits, the specification change log and the register of "
        "CPUC-mandated changes.",
        "Everyone; written for the CPUC staff who announce changes",
        (),
        "cpuc",
    ),
    Guide(
        "administering",
        "Administering CEDARS",
        "Organizations and their members, access requests, role templates, recertification, data stewardship, access settings, the "
        "audit log, retention, publication and accessibility settings, and jobs and health.",
        "CPUC system administrators and organization administrators",
        ("accounts",),
        "admin",
    ),
]
BY_SLUG = {g.slug: g for g in GUIDES}


def by_reader() -> list[tuple[str, str, list[Guide]]]:
    """The Help index's groups: each reader's key, heading and guides, in READERS order."""
    return [(key, label, [g for g in GUIDES if g.reader == key]) for key, label in READERS]


def support() -> dict:
    """Where to get help and send feedback, from settings. Each is blank when the site has not set it."""
    email = getattr(settings, "CEDARS_SUPPORT_EMAIL", "") or ""
    feedback_url = getattr(settings, "CEDARS_FEEDBACK_URL", "") or ""
    mailto = f"mailto:{email}" if email else ""
    feedback = feedback_url or (f"mailto:{email}?subject={quote('CEDARS feedback')}" if email else "")
    return {
        "email": email,
        "mailto": mailto,
        "support_mailto": f"mailto:{email}?subject={quote('CEDARS support request')}" if email else "",
        "feedback": feedback,
        "feedback_is_form": bool(feedback_url),
        "accessibility": f"mailto:{email}?subject={quote('CEDARS accessibility barrier')}" if email else "",
    }


@dataclass(frozen=True)
class Step:
    target: str  # the data-tour value of the part of the front page it describes ("" = the page as a whole)
    title: str
    text: str
    link: str = ""  # where to go to try it
    link_text: str = ""
    heading = ""  # (page_help.TourStep's fields: the tour template reads both kinds of step)
    optional = False

    @property
    def selector(self) -> str:
        return f'[data-tour="{self.target}"]' if self.target else ""


@dataclass(frozen=True)
class Visitor:
    """What the tour may show someone: signed in or not, and which signed-in parts their header has."""

    signed_in: bool = False
    acting_for: bool = False  # belongs to an organization (the header's Acting for)
    sysadmin: bool = False
    org_admin: bool = False
    steward: bool = False

    @property
    def admin(self) -> bool:
        return self.sysadmin or self.org_admin or self.steward


def visitor(request) -> Visitor:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or getattr(request, "assurance", "") == "password":
        return Visitor()
    from cedars.services.accounts import service as access

    orgs = getattr(request, "user_organisations", [])
    return Visitor(
        signed_in=True,
        acting_for=bool(orgs),
        sysadmin=access.is_system_admin(user),
        org_admin=any(access.is_org_admin(user, o) for o in orgs),
        steward=access.is_steward(user),
    )


def tour_steps(who: Visitor | bool) -> list[Step]:
    """The tour of the front page. A signed-out visitor sees the public parts; a signed-in user also gets a step for each part only
    signed-in users see (Jake, 2026-10-31), following their header (core/nav.py): the filings, reference data and My queue for a member
    of an organization, calculations, published data, administration (for administrators), the organization they act for, notifications
    and their account."""
    if isinstance(who, bool):
        who = Visitor(signed_in=who)
    welcome = (
        "CEDARS is where California's energy-efficiency program administrators file their budgets, claims and monthly reports, "
        "and where the CPUC reviews and publishes them. "
    )
    if who.signed_in:
        welcome += (
            "You are signed in, so besides the public pages this tour shows the parts only signed-in users see"
            + (": filings, calculations, reference data and your account. " if who.acting_for else ": calculations and your account. ")
            + "Use Next and Back to move, or Escape to leave at any time."
        )
    else:
        welcome += "This tour shows where things are. Use Next and Back to move, or Escape to leave at any time."
    steps = [
        Step("intro", "Welcome to CEDARS", welcome),
        PAGE_HELP_STEP,
        SEARCH_STEP,
        Step(
            "trends",
            "How the portfolio is doing",
            "Five figures from the published claims, each with its change from the year before and a small trend line: total system "
            "benefit, electricity saved, peak demand avoided, gas saved and spending. Choose a metric, a group of program "
            "administrators or a year; the charts below show that metric by year, against spending, by PA and as benefit per dollar "
            "spent. Point at a chart, or select it and use the arrow keys, to read each value; Show as a table lists them all. Each "
            "chart links to the dashboard page that breaks it down.",
        ),
        Step(
            "nav-dashboard",
            "Portfolio dashboard",
            "Forecast and claimed results side by side, by PA, sector, program or measure. Filter, drill down from any row, and "
            "download exactly the rows you see as CSV, Excel or Parquet. It is public: what it shows is the same signed in or not.",
            "/data/overview/",
            "Open the portfolio dashboard",
        ),
    ]
    programs = Step(
        "nav-programs",
        "Programs",
        "Every program in the registry, closed ones included, with each program's versions, allocation factors and the "
        "evaluations that cover it. A program's own administrators change its attributes from its page; a change to a locked "
        "attribute is a request the CPUC decides, listed under program change requests.",
        "/programs/",
        "Open the program catalog",
    )
    if not who.signed_in:
        return steps + [
            programs,
            Step(
                "nav-published",
                "Published data",
                "What anyone may read: the published measure-level records for every PA, PA documents, CPUC guidance, DEER resources and "
                "EM&V studies, every field's definition, the change logs of corrected filings, the statewide allocation factors and the "
                "filing calendar. None of it needs an account.",
                "/data/records/",
                "Open record downloads",
            ),
            Step(
                "sign-in",
                "Sign in",
                "Program administrators and CPUC staff sign in to file and review, and anyone can sign in to run cost-effectiveness "
                "calculations or follow documents. Anyone can create an account; every account uses a second factor, set up the "
                "first time you sign in. Signed in, the header adds Calculations and your account, and, once you act for a program "
                "administrator, Filings, Reference data and My queue.",
                "/accounts/login/",
                "Sign in",
            ),
            HELP_STEP,
        ]
    steps.append(programs)
    if who.acting_for:
        steps += [
            Step(
                "nav-filings",
                "Filings: budgets and fund shifts",
                "Only signed in. Budget filings follow the four-year cycle: upload the Measure and ProgramCost tables, see every finding "
                "against the rules, price the filing, then confirm it. Every confirmed version is kept and can be compared with any "
                "other. Fund shifts move budget between programs; the standing filing plus the shifts in effect is the operating budget.",
                "/budgets/",
                "Open Budgets",
            ),
            Step(
                "nav-filings",
                "Filings: claims",
                "Each claim year is confirmed after each quarter and once more with the final claims. A quarter's upload holds what is "
                "new or changed and is compiled over the earlier quarters; deemed measures are checked against eTRM. Contacts and sites "
                "go to the PII vault separately and need a data steward's approval to read. Corrections after a due date are new "
                "versions, published with a change letter.",
                "/claims/",
                "Open Claims",
            ),
            Step(
                "nav-filings",
                "Filings: monthly reports and the calendar",
                "Each month a PA reports its commitments and the year's inputs so far; spending is compared with the operating budget. "
                "The filing calendar lists every due date, and drafts and versions not yet confirmed are seen only by your "
                "organization.",
                "/filings/calendar/",
                "Open the filing calendar",
            ),
        ]
    steps += [
        Step(
            "nav-calculations",
            "Calculations: runs",
            "Only signed in. Price any job on the ReCET engine: upload it as a .zip of Measure.csv and ProgramCost.csv, choose the "
            "engine release and the ACC, and start the run. The run page shows its progress, then its files and results, and any "
            "two finished runs can be compared. Runs are yours alone or your organization's.",
            "/calculations/runs/",
            "Open your runs",
        ),
        Step(
            "nav-calculations",
            "Calculations: editing inputs, templates and impact profiles",
            "Edit inputs edits a small job's tables row by row, checked as you save. Output templates choose the columns a run "
            "writes. The impact profile library holds every hourly profile a measure can name, and a PA can add its own custom "
            "profiles.",
            "/calculations/drafts/",
            "Open Edit inputs",
        ),
        Step(
            "nav-published",
            "Published data",
            "What anyone may read, signed in or not: the published records, saved views of them, the document library and the "
            "evaluation studies, and the data dictionary. Large record downloads are made in the background and listed under your "
            "downloads."
            + ("" if who.acting_for else " The change logs, the statewide allocation factors and the filing calendar are here too."),
            "/data/records/",
            "Open record downloads",
        ),
        Step(
            "nav-reference",
            "Reference data",
            (
                "What runs and filings are priced and checked against: the data packs (ACCs, eTRM, the profile library), the cost-test "
                "sets and their builder, the statewide allocation factors the funding PAs load and confirm, the validation rules, the "
                "value lists and the register of CPUC-mandated changes."
            )
            if who.acting_for
            else (
                "The cost-test sets your own runs can be priced with, and the builder for a set of your own. The rest of the reference "
                "data opens once you act for a program administrator."
            ),
            "/reference-data/cost-tests/",
            "Open the cost-test sets",
        ),
    ]
    if who.admin:
        parts = []
        if who.sysadmin:
            parts.append(
                "as a CPUC system administrator, in four groups: people and access (organizations, role templates, access settings, "
                "the audit log), data and publishing (goals, publication holds, document accessibility, retention), communications "
                "(release notes, the register of CPUC-mandated changes, feedback) and the system's jobs and health"
            )
        if who.org_admin:
            parts.append(
                "as an organization administrator: your organization's members, their grants, access requests and the yearly "
                "recertification"
            )
        if who.steward:
            parts.append("as a data steward: approvals of personal-information access and of the rules that govern it")
        steps.append(
            Step(
                "nav-admin",
                "Administration",
                "Only for administrators. You see it " + "; and ".join(parts) + ". Every change made here is recorded in the audit trail.",
            )
        )
    if who.acting_for:
        steps.append(
            Step(
                "acting-for",
                "The organization you act for",
                "Everything you file or run is for the organization shown here, and the audit trail records it. If you belong to "
                "more than one, choose another and Switch.",
            )
        )
    if who.acting_for:
        steps.append(
            Step(
                "queue",
                "My queue",
                "The work waiting on you now, across every module: requests to decide, filings ready to confirm, fund shifts to "
                "acknowledge, comments waiting for your reply, documents to publish and questions to answer. The count beside it is "
                "how many items wait. Each item leaves it once done.",
                "/queue/",
                "Open your queue",
            )
        )
    steps += [
        Step(
            "notifications",
            "Notifications",
            "What needs you: a filing to review, an access request, a due date coming up, a run that finished. The count is how many "
            "are unread, and the same notices go by email.",
            "/notifications/",
            "Open notifications",
        ),
        Step(
            "account",
            "Your account",
            "Your second factor and recovery codes, the organizations you belong to and your permissions in each, your access "
            "requests, and API tokens for your own systems. To act for a program administrator, ask its administrators for access "
            "from here.",
            "/account/",
            "Open your account",
        ),
        HELP_STEP,
    ]
    return steps


# The (?) is taught here, once, on the front page (Jake, 2026-11-02): a page's own tour is started from that button, so it does not
# end on it.
PAGE_HELP_STEP = Step(
    "page-help",
    "Help on every page",
    "The question mark beside this heading, and beside every page's heading, opens a guide to that page and a tour of just that page. "
    "Use it whenever you land somewhere new.",
)

# The site search in the header (core/search.py), on every page.
SEARCH_STEP = Step(
    "search",
    "Search",
    "Find a page, a user guide or answer, a glossary term, a program by PrgID or name, a data dictionary field or a document from any "
    "page. Results include only what you may open.",
)

HELP_STEP = Step(
    "help",
    "Help",
    "User guides for each part of CEDARS, how to get support or send feedback, and this tour again. Help is also in the header and the "
    "footer of every page.",
    "/help/",
    "Open Help",
)
