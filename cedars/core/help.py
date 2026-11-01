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

    @property
    def revised(self) -> str:
        """The platform release this guide was last revised for (BR-12.5; cedars/core/revisions.py)."""
        from . import revisions

        return revisions.revised(f"guide:{self.slug}")


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
        "The portfolio dashboard, record downloads, the document library, the data dictionary and the validation rules. No account needed.",
        "Everyone",
        ("data", "documents", "reference"),
    ),
    Guide(
        "programs",
        "Programs",
        "The program catalog, a program's page and versions, and asking for a change to a program.",
        "Program administrators and CPUC staff",
        ("programs",),
    ),
    Guide(
        "budgets",
        "Budget filings and fund shifts",
        "Uploading, checking, pricing and confirming a budget filing; comparing versions; fund shifts and the operating budget.",
        "Program administrators",
        ("budgets",),
    ),
    Guide(
        "claims",
        "Claims",
        "Quarterly and annual claims: the template, uploads compiled over earlier quarters, withdrawals, personal information and "
        "corrections.",
        "Program administrators",
        ("claims",),
    ),
    Guide(
        "monthly-reports",
        "Monthly reports",
        "What a monthly report holds, how spending is compared with the operating budget, and the 13th-month report.",
        "Program administrators",
        ("monthly",),
    ),
    Guide(
        "calculations",
        "Cost-effectiveness runs",
        "Starting a run, reading its results, comparing two runs, editing a small job's inputs, output templates and impact profiles.",
        "Analysts at PAs and the CPUC",
        ("calculations",),
    ),
    Guide(
        "accessibility",
        "Keyboard and screen reader use",
        "How the pages work with a keyboard, a screen reader and zoom, and how to report a barrier.",
        "Everyone",
    ),
]
BY_SLUG = {g.slug: g for g in GUIDES}


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
    signed-in users see (Jake, 2026-10-31): the filings, calculations, reference data, administration (for administrators), the
    organization they act for, notifications and their account."""
    if isinstance(who, bool):
        who = Visitor(signed_in=who)
    welcome = (
        "CEDARS is where California's energy-efficiency program administrators file their budgets, claims and monthly reports, "
        "and where the CPUC reviews and publishes them. "
    )
    if who.signed_in:
        welcome += (
            "You are signed in, so besides the public pages this tour shows the parts only signed-in users see: filings, "
            "calculations, reference data and your account. Use Next and Back to move, or Escape to leave at any time."
        )
    else:
        welcome += "This tour shows where things are. Use Next and Back to move, or Escape to leave at any time."
    steps = [
        Step("intro", "Welcome to CEDARS", welcome),
        Step(
            "cycle",
            "This program cycle at a glance",
            "The cards sum the published filings for one four-year program cycle: spending against the budget, total system benefit "
            "against the CPUC's goal, the energy saved, and the statewide TRC and PAC ratios. Each card links to the dashboard page "
            "that breaks its numbers down. Choose another cycle with the Program cycle box.",
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
    if not who.signed_in:
        return steps + [
            Step(
                "nav-records",
                "Record downloads",
                "The published measure-level records for every PA in one file: choose the records and the columns, then download them.",
                "/data/records/",
                "Open record downloads",
            ),
            Step(
                "nav-reference",
                "Documents, data dictionary and rules",
                "PA documents, CPUC guidance, DEER resources and EM&V studies; every field's definition; and the validation rules "
                "every filing is checked against. All open without an account.",
                "/documents/",
                "Open the document library",
            ),
            Step(
                "sign-in",
                "Sign in",
                "Program administrators and CPUC staff sign in to file and review, and anyone can sign in to run cost-effectiveness "
                "calculations or follow documents. Anyone can create an account; every account uses a second factor, set up the "
                "first time you sign in. Signed in, the header adds Filings, Calculations, notifications and your account.",
                "/accounts/login/",
                "Sign in",
            ),
            HELP_STEP,
        ]
    steps += [
        Step(
            "nav-programs",
            "Programs",
            "Every program in the registry, closed ones included, with each program's versions, allocation factors and the "
            "evaluations that cover it. A program's own administrators change its attributes from its page; a change to a locked "
            "attribute is a request the CPUC decides, listed under program change requests.",
            "/programs/",
            "Open the program catalog",
        ),
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
        Step(
            "nav-calculations",
            "Calculations: runs",
            "Only signed in. Price any job on the ReCET engine: upload it as a .zip of Measure.csv and ProgramCost.csv, choose the "
            "engine release and the ACC, and start the run. The run page shows its progress, then its files and results, and any "
            "two finished runs can be compared. Runs are yours alone or your organization's.",
            "/calculations/",
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
            "nav-reference",
            "Reference",
            "Record downloads, documents, the data dictionary and the validation rules, as for everyone, plus the reference data "
            "every filing is checked and priced with (ACCs, cost-test sets, eTRM packs). Large record downloads are made in the "
            "background and listed under your downloads.",
            "/reference-data/",
            "Open reference data",
        ),
    ]
    if who.admin:
        parts = []
        if who.sysadmin:
            parts.append(
                "as a CPUC system administrator: organizations, allocation factors, energy efficiency goals, retention, jobs and "
                "health, site settings and the audit log"
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


HELP_STEP = Step(
    "help",
    "Help",
    "User guides for each part of CEDARS, how to get support or send feedback, and this tour again. Help is also in the header and the "
    "footer of every page.",
    "/help/",
    "Open Help",
)
