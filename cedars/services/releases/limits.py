"""The limits people meet in CEDARS (BR-12.1: "the UI shall always show current limits"), each read from the setting that enforces it,
so the public Limits page (/limits/) and the text beside each upload field say what the site does now. A planned change to a limit is
a release note's change naming the limit (models.Change.limit); the Limits page shows it as coming, with its date.

A new limit people can meet goes in LIMITS, read from its setting (never a copy of the number)."""

from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings


def _rate(text: str) -> str:
    """DRF's "120/minute" as "120 calls a minute"."""
    try:
        n, per = text.split("/", 1)
    except ValueError:
        return text
    per = {"s": "second", "sec": "second", "m": "minute", "min": "minute", "h": "hour", "d": "day"}.get(per, per)
    return f"{int(n):,} calls a {per}"


def _rates() -> dict:
    return settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})


def _edit_rows():
    """Edit inputs' cap is the engine's own (ReCET's editor.MAX_EDIT_ROWS); None where the engine is not installed."""
    try:
        from recet import editor
    except ImportError:
        return None
    return editor.MAX_EDIT_ROWS


@dataclass(frozen=True)
class Limit:
    key: str
    label: str
    group: str
    value: Callable[[], object]  # read when shown
    unit: str  # after the number: "MB", "files", "rows" ...; "" when value() is already text
    applies: str  # where it applies
    over: str  # what happens over it
    setting: str  # the setting that sets it (for administrators)

    def current(self) -> str:
        v = self.value()
        if v is None:
            return "Set by the calculation engine"
        if isinstance(v, int):
            return f"{v:,} {self.unit}".strip()
        return f"{v} {self.unit}".strip()


UPLOADS, RUNS, DOWNLOADS, API = "Uploads", "Calculations", "Downloads", "The API and signing in"
LIMITS = [
    Limit(
        "upload_mb",
        "Size of each uploaded file",
        UPLOADS,
        lambda: settings.CEDARS_UPLOAD_MAX_MB,
        "MB",
        "Each file uploaded to Budgets, Claims or Monthly reports, and a calculation job's .zip (pages and API).",
        "The upload is refused, naming the file and its size. Split a table into parts (Measure_1.csv, Measure_2.csv) across files.",
        "CEDARS_UPLOAD_MAX_MB",
    ),
    Limit(
        "upload_files",
        "Files in one filing upload",
        UPLOADS,
        lambda: settings.CEDARS_UPLOAD_MAX_FILES,
        "files",
        "One upload to Budgets, Claims or Monthly reports (joined into one filing), and the files on one comment on a filing.",
        "The upload is refused. Put more tables in a .zip.",
        "CEDARS_UPLOAD_MAX_FILES",
    ),
    Limit(
        "correction_rows",
        "Records in one partial correction",
        UPLOADS,
        lambda: settings.CEDARS_CORRECTION_MAX_ROWS,
        "records",
        "A partial correction of a draft filing's records (only the changed and new records, and the ones it deletes).",
        "The correction is refused before anything changes. Upload the whole corrected file instead.",
        "CEDARS_CORRECTION_MAX_ROWS",
    ),
    Limit(
        "correction_list",
        "Records listed on a correction page",
        UPLOADS,
        lambda: settings.CEDARS_CORRECTION_LIST_ROWS,
        "records",
        "The records a search on Correct records lists, and the changes a partial correction's preview lists.",
        "The rest are counted: narrow the search, or download a preview's every change as CSV.",
        "CEDARS_CORRECTION_LIST_ROWS",
    ),
    Limit(
        "document_mb",
        "Size of each document file",
        UPLOADS,
        lambda: settings.DOCUMENTS_MAX_FILE_MB,
        "MB",
        "A document version's main file and each attachment, and each file attached to a comment on a filing.",
        "The file is refused.",
        "DOCUMENTS_MAX_FILE_MB",
    ),
    Limit(
        "comment_chars",
        "Length of a comment on a filing",
        UPLOADS,
        lambda: settings.CEDARS_COMMENT_MAX_CHARS,
        "characters",
        "One comment on a budget filing, monthly report or claims version.",
        "The comment is refused. Post it in parts.",
        "CEDARS_COMMENT_MAX_CHARS",
    ),
    Limit(
        "feedback_chars",
        "Length of a feedback message",
        UPLOADS,
        lambda: settings.CEDARS_FEEDBACK_MAX_CHARS,
        "characters",
        "One message sent from a page's Feedback link.",
        "The message is refused. Send it in parts.",
        "CEDARS_FEEDBACK_MAX_CHARS",
    ),
    Limit(
        "feedback_per_hour",
        "Feedback messages one person sends an hour",
        UPLOADS,
        lambda: settings.CEDARS_FEEDBACK_PER_HOUR,
        "messages",
        "Messages sent from the Feedback link by one account, or from one address while signed out.",
        "The message is refused until the hour has passed.",
        "CEDARS_FEEDBACK_PER_HOUR",
    ),
    Limit(
        "load_shape_mb",
        "Size of a load shape upload",
        UPLOADS,
        lambda: settings.LOAD_SHAPE_MAX_UPLOAD_MB,
        "MB",
        "A measure package's load shapes (CSV, or a ZIP holding one), in Impact profiles.",
        "The upload is rejected with finding LS-F02.",
        "LOAD_SHAPE_MAX_UPLOAD_MB",
    ),
    Limit(
        "load_shape_unzipped_mb",
        "Size of the CSV inside a load shape ZIP",
        UPLOADS,
        lambda: settings.LOAD_SHAPE_MAX_UNZIPPED_MB,
        "MB",
        "The CSV file inside a zipped load shape upload.",
        "The upload is rejected with finding LS-F02.",
        "LOAD_SHAPE_MAX_UNZIPPED_MB",
    ),
    Limit(
        "custom_profile_mb",
        "Size of a custom impact profile file",
        UPLOADS,
        lambda: settings.IMPACT_PROFILES_CUSTOM_MAX_MB,
        "MB",
        "A PA's own impact profile, in Impact profiles.",
        "The file is rejected with finding LS-F02.",
        "IMPACT_PROFILES_CUSTOM_MAX_MB",
    ),
    Limit(
        "run_records",
        "Measures in one calculation run",
        RUNS,
        lambda: settings.CALCULATIONS_MAX_RECORDS,
        "measures",
        "Every run: New run, the API, and the runs that price budget filings, claims and monthly reports.",
        "The run is refused before pricing (too_many_records). Split the job.",
        "CALCULATIONS_MAX_RECORDS",
    ),
    Limit(
        "small_run_records",
        "Measures in a run on the faster queue",
        RUNS,
        lambda: settings.CALCULATIONS_SMALL_RUN_RECORDS,
        "measures",
        "Runs up to this size go to the small-runs queue, so they do not wait behind large ones.",
        "Nothing is refused: a larger run takes the large-runs queue.",
        "CALCULATIONS_SMALL_RUN_RECORDS",
    ),
    Limit(
        "edit_rows",
        "Measures in Edit inputs",
        RUNS,
        _edit_rows,
        "measures",
        "A job's tables edited row by row on the Edit inputs pages.",
        "Edit inputs is not offered; edit larger jobs offline and upload them.",
        "the engine (ReCET's editor.MAX_EDIT_ROWS)",
    ),
    Limit(
        "etrm_packages",
        "eTRM packages listed per search",
        RUNS,
        lambda: settings.ETRM_PACKAGE_LIST,
        "packages",
        "The measure packages Add a measure from eTRM lists for one search (Edit inputs).",
        "The first ones are listed and the page says there are more: narrow the search.",
        "ETRM_PACKAGE_LIST",
    ),
    Limit(
        "download_rows",
        "Rows in a download made while you wait",
        DOWNLOADS,
        lambda: settings.DATA_DOWNLOAD_MAX_ROWS,
        "rows",
        "Dashboard and record downloads.",
        "Nothing is refused: a larger download is made in the background and listed under your downloads (signed in).",
        "DATA_DOWNLOAD_MAX_ROWS",
    ),
    Limit(
        "download_jobs",
        "Background downloads waiting at once",
        DOWNLOADS,
        lambda: settings.DATA_DOWNLOAD_JOBS_PER_USER,
        "per person",
        "Downloads too large to make while you wait.",
        "Another request waits until one of yours is made.",
        "DATA_DOWNLOAD_JOBS_PER_USER",
    ),
    Limit(
        "saved_views",
        "Saved views one person keeps",
        DOWNLOADS,
        lambda: settings.DATA_SAVED_VIEWS_PER_USER,
        "views",
        "Views saved from the dashboard pages and the records page.",
        "Saving another is refused until you delete one under Saved views.",
        "DATA_SAVED_VIEWS_PER_USER",
    ),
    Limit(
        "saved_view_shares",
        "People one saved view is shared with by name",
        DOWNLOADS,
        lambda: settings.DATA_SAVED_VIEW_SHARES,
        "people",
        "Sharing a saved view by email address. Sharing with an organization's members has no limit.",
        "Sharing with one more person is refused; share the view with your organization instead.",
        "DATA_SAVED_VIEW_SHARES",
    ),
    Limit(
        "api_rate_user",
        "API calls with a token",
        API,
        lambda: _rate(_rates().get("user", "")),
        "",
        "Every call made with any of one person's tokens, or from their signed-in pages' scripts, counted together.",
        "The call gets 429 Too Many Requests with a Retry-After header in seconds.",
        "CEDARS_API_RATE_USER",
    ),
    Limit(
        "api_rate_anon",
        "API calls without a token",
        API,
        lambda: _rate(_rates().get("anon", "")),
        "",
        "Calls with no token, counted per address.",
        "The call gets 429 Too Many Requests with a Retry-After header in seconds.",
        "CEDARS_API_RATE_ANON",
    ),
    Limit(
        "api_data_rows",
        "Rows in one page of the data API",
        API,
        lambda: settings.DATA_API_MAX_ROWS,
        "rows",
        "Aggregated queries and record reads at /api/v1/data (each page; the next page is linked).",
        "The call gets 400 Bad Request. Ask for fewer rows with limit and read the next page.",
        "DATA_API_MAX_ROWS",
    ),
    Limit(
        "api_token_days",
        "Longest an API token lasts",
        API,
        lambda: settings.CEDARS_API_TOKEN_MAX_DAYS,
        "days",
        "A token made on your account page.",
        "The token stops working; make a new one.",
        "CEDARS_API_TOKEN_MAX_DAYS",
    ),
]
BY_KEY = {lim.key: lim for lim in LIMITS}


def current(key: str) -> str:
    """A limit's value as it stands, as text ("150 MB"), for FAQ answers and help text; "" for an unknown key."""
    lim = BY_KEY.get(key)
    return lim.current() if lim else ""


def groups() -> list[tuple[str, list[Limit]]]:
    out: dict[str, list[Limit]] = {}
    for lim in LIMITS:
        out.setdefault(lim.group, []).append(lim)
    return list(out.items())
