"""Job and system health (BR-12.2): each module and service registers checks here (in its AppConfig.ready, as with the registry's
hooks, so nothing here imports a module), the administrators' page /admin/health/ shows them, and `manage.py check_health` (scheduled
every 10 minutes) runs them, writes one JSON line for CloudWatch's metric filters and alerts the CPUC's system administrators when a
check starts failing. Thresholds are settings (HEALTH_*), so they change without a release."""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

OK, ATTENTION, FAILING = "ok", "attention", "failing"
LABELS = {OK: "OK", ATTENTION: "Needs attention", FAILING: "Failing"}
ORDER = {FAILING: 0, ATTENTION: 1, OK: 2}


@dataclass
class Item:
    """One thing a check found (a stalled run, a failed job): what, since when, why, and where to look."""

    key: str
    label: str
    since: object = None
    detail: str = ""
    link: str = ""


@dataclass
class Result:
    code: str  # stable, e.g. "calculations.stalled"
    area: str  # "Calculations"
    title: str  # "Runs waiting or running too long"
    status: str = OK
    summary: str = ""
    items: list[Item] = field(default_factory=list)
    link: str = ""

    @property
    def label(self) -> str:
        return LABELS[self.status]

    @property
    def fingerprint(self) -> str:
        """Changes when the set of things failing changes, so the same stalled run is alerted once."""
        return hashlib.sha256("|".join(sorted(i.key for i in self.items)).encode()).hexdigest()[:16]


_checks: list[tuple[str, Callable[[], Result]]] = []


def register(code: str, check: Callable[[], Result]) -> None:
    if code not in {c for c, _ in _checks}:
        _checks.append((code, check))


def limit(name: str) -> timedelta:
    """A threshold from settings.HEALTH (minutes)."""
    return timedelta(minutes=settings.HEALTH[name])


def ago(when) -> str:
    """'25 minutes', '3 hours', '2 days': how long since `when`."""
    m = int((timezone.now() - when).total_seconds() // 60)
    if m < 120:
        return f"{m} minute{'s' if m != 1 else ''}"
    h = m // 60
    return f"{h} hours" if h < 48 else f"{h // 24} days"


def run_all() -> list[Result]:
    """Every registered check, failing first. A check that raises is itself a failing result, never an error page."""
    out = []
    for code, check in _checks:
        try:
            out.append(check())
        except Exception as e:  # noqa: BLE001 - a broken check must show, not hide the others
            log.exception("health check %s failed to run", code)
            out.append(Result(code, "Health checks", f"Check {code}", FAILING, f"The check could not run: {type(e).__name__}: {e}"))
    return sorted(out, key=lambda r: (ORDER[r.status], r.area, r.title))


def summary_line(results: list[Result]) -> str:
    """One JSON line for CloudWatch Logs (metric filters on $.event = "cedars_health": $.failing, $.attention, a heartbeat)."""
    return json.dumps(
        {
            "event": "cedars_health",
            "at": timezone.now().isoformat(timespec="seconds"),
            "failing": sum(r.status == FAILING for r in results),
            "attention": sum(r.status == ATTENTION for r in results),
            "checks": {r.code: r.status for r in results},
        },
        sort_keys=True,
    )


def alert(results: list[Result]) -> int:
    """Tell the CPUC's system administrators about each failing check, once for each set of things failing. Returns how many messages."""
    from cedars.services.accounts import permissions as perms
    from cedars.services.accounts import service as accounts
    from cedars.services.notifications import service as notifications

    failing = [r for r in results if r.status == FAILING]
    org = accounts.cpuc()
    if not failing or org is None:
        return 0
    admins = list(accounts.holders(org, perms.SYSTEM_ADMIN))
    sent = 0
    for r in failing:
        lines = [r.summary] + [f"- {i.label}{': ' + i.detail if i.detail else ''}" for i in r.items[:10]]
        if len(r.items) > 10:
            lines.append(f"... and {len(r.items) - 10} more")
        for user in admins:
            n = notifications.notify(
                user, f"{r.area}: {r.title}", "\n".join(lines), "/admin/health/", key=f"health:{r.code}:{r.fingerprint}"
            )
            sent += n is not None
    return sent
