"""Dashboard parity (spec PR 9): the dashboard's query layer checked against totals CEDARS published, for the years the platform holds
as legacy imports. Run on an instance that has them loaded (`manage.py dashboard_parity --expected totals.csv`); the published totals
are not part of this repository.

The expected file is one row per published number, columns:
  dataset   claims | forecast
  year      the program (claim) year
  pa        a CEDARS PA code, or ALL for the portfolio row the page printed
  filing    forecast only: the filing kind or "<cycle>|<kind>" (the dashboards' filing choice); blank = each PA's latest
  metric    a metric catalog code (tsb, expenditure, kwh_first_year_net, trc ...); other codes are reported as not checked
  value     as printed, in the summed fields' own units ($, kWh, kW, therms, lb or metric tons as filed; ratios as ratios)
  scope     what the page included, separated by semicolons: cs (codes and standards), esa, pension, cca_ren (CCAs and RENs),
            statewide (statewide unallocated claims), or all
  source    where the number came from (shown in the report only)

Each (dataset, year, filing, scope) runs one query by PA and one total, with codes and standards and ESA left out unless the scope
names them, and an ALL row limited to the IOUs unless it names cca_ren. Pension and Benefits programs are always counted: CEDARS' pages
have no switch for them (the platform's pages leave them out by default). Statewide unallocated claims are never in the dashboard
(they stay outside every total), so a scope that names them is noted on every row it touches.

A sum agrees within `rel` of the published value (default 0.5%, the reconciliation's threshold) or 0.5 of its unit (the pages print
whole numbers); a ratio, printed to two decimals, within 0.005 + `rel`. A published ratio of 0 agrees with a blank one: the dashboard
leaves a ratio blank over a zero cost where CEDARS prints 0. A known-differences file (dataset, year, pa, metric, reason; blank year,
pa or metric = any) marks the differences already explained so they do not fail the check."""

import csv
from collections import defaultdict
from dataclasses import dataclass, field

from cedars.services.registry.goals import UTILITIES

from . import metrics as catalog
from . import query as dq

COLUMNS = ("dataset", "year", "pa", "filing", "metric", "value", "scope", "source")
SCOPES = {"cs", "esa", "pension", "cca_ren", "statewide", "all"}
REL = 0.005
RATIO_ABS = 0.005
SUM_ABS = 0.5
OUTCOMES = ("agrees", "differs", "known", "missing", "not checked")


class ParityError(ValueError):
    """An expected or known-differences file that cannot be read; the message says which row."""


@dataclass
class Check:
    dataset: str
    year: int
    pa: str
    filing: str
    metric: str
    scope: str
    source: str
    expected: float
    platform: float | None = None
    outcome: str = "not checked"
    note: str = ""

    @property
    def difference(self) -> float | None:
        return None if self.platform is None else self.platform - self.expected

    @property
    def percent(self) -> float | None:
        return None if self.platform is None or not self.expected else 100 * (self.platform - self.expected) / abs(self.expected)


@dataclass
class Report:
    checks: list[Check]
    releases: dict = field(default_factory=dict)  # dataset -> release number read

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.checks if c.outcome == outcome)

    @property
    def ok(self) -> bool:
        return not self.count("differs") and not self.count("missing")

    def lines(self) -> list[str]:
        out = [", ".join(f"{self.count(o)} {o}" for o in OUTCOMES)]
        out += [f"{d} release {n}" for d, n in sorted(self.releases.items())]
        for c in self.checks:
            if c.outcome in ("differs", "missing"):
                pct = f" ({c.percent:+.3f}%)" if c.percent is not None else ""
                got = "nothing" if c.platform is None else f"{c.platform:,.4f}"
                out.append(f"  {c.outcome}: {c.dataset} {c.year} {c.pa} {c.metric}: published {c.expected:,.4f}, dashboard {got}{pct}")
        return out

    def write_csv(self, f) -> None:
        w = csv.writer(f, lineterminator="\n")
        w.writerow([*COLUMNS[:5], "scope", "source", "published", "dashboard", "difference", "percent", "outcome", "note"])
        for c in self.checks:
            w.writerow(
                [c.dataset, c.year, c.pa, c.filing, c.metric, c.scope, c.source, c.expected, _blank(c.platform), _blank(c.difference),
                 _blank(c.percent), c.outcome, c.note]
            )  # fmt: skip


def _blank(v):
    return "" if v is None else v


def _number(text: str, where: str) -> float:
    try:
        return float(str(text).replace(",", "").replace("$", "").strip())
    except ValueError as e:
        raise ParityError(f"{where}: {text!r} is not a number") from e


def read_expected(f) -> list[Check]:
    reader = csv.DictReader(f)
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ParityError(f"the expected file has no {', '.join(missing)} column")
    out = []
    for i, r in enumerate(reader, start=2):
        where = f"row {i}"
        if (r["dataset"] or "").strip() not in ("claims", "forecast"):
            raise ParityError(f"{where}: dataset {r['dataset']!r} is not claims or forecast")
        scope = ";".join(sorted({s.strip().lower() for s in (r["scope"] or "").split(";") if s.strip()}))
        unknown = set(scope.split(";")) - SCOPES - {""}
        if unknown:
            raise ParityError(f"{where}: unknown scope {', '.join(sorted(unknown))}")
        out.append(
            Check(
                dataset=r["dataset"].strip(),
                year=int(_number(r["year"], where)),
                pa=(r["pa"] or "").strip().upper(),
                filing=(r["filing"] or "").strip(),
                metric=(r["metric"] or "").strip(),
                scope=scope,
                source=(r["source"] or "").strip(),
                expected=_number(r["value"], where),
            )
        )
    return out


def read_known(f) -> list[dict]:
    reader = csv.DictReader(f)
    if "reason" not in (reader.fieldnames or []):
        raise ParityError("the known-differences file has no reason column")
    return [{k: (r.get(k) or "").strip() for k in ("dataset", "year", "pa", "metric", "reason")} for r in reader]


def _known(c: Check, known: list[dict]) -> str:
    for k in known:
        if all(not k[f] or k[f].upper() == str(getattr(c, f)).upper() for f in ("dataset", "year", "pa", "metric")):
            return k["reason"] or "known difference"
    return ""


def _exclusions(scope: set[str]) -> tuple[str, ...]:
    if "all" in scope:
        return ()
    return tuple(e for e in ("esa", "cs") if e not in scope)


def _raw(m: catalog.Metric, value):
    """A query value back in the summed fields' units (the query divides by the metric's display scale)."""
    if value is None:
        return None
    return value if m.denominator else value * m.scale


def run(expected: list[Check], known: list[dict] = (), rel: float = REL) -> Report:
    report = Report(expected)
    groups: dict[tuple, list[Check]] = defaultdict(list)
    for c in expected:
        if c.metric not in catalog.CATALOG:
            c.note = "not a dashboard metric"
            continue
        groups[(c.dataset, c.year, c.filing, c.scope)].append(c)
    for (dataset, year, filing, scope_text), checks in groups.items():
        scope = set(scope_text.split(";")) - {""}
        metrics = tuple(dict.fromkeys(c.metric for c in checks))
        base = dq.Query(dataset=dataset, metrics=metrics, year_from=year, year_to=year, exclude=_exclusions(scope), filing=filing)
        by_pa = dq.run(base.but(by=("pa",)))
        portfolio = base if "cca_ren" in scope or "all" in scope else base.but(filters=(("pa", UTILITIES),))
        total = dq.run(portfolio)
        if by_pa.release:
            report.releases[dataset] = by_pa.release.number
        rows = {str(r["pa"]).upper(): r for r in by_pa.rows}
        for c in checks:
            m = catalog.get(c.metric)
            source = total.totals if c.pa == "ALL" else rows.get(c.pa)
            c.platform = _raw(m, source.get(c.metric)) if source else None
            notes = []
            if "statewide" in scope:
                notes.append("the page includes statewide unallocated claims; the dashboard never does")
            if c.platform is None and m.denominator and c.expected == 0 and source:
                c.outcome = "agrees"
                notes.append("blank over a zero cost; CEDARS prints 0")
            elif c.platform is None:
                c.outcome = "missing"
            else:
                tolerance = rel * abs(c.expected) + (RATIO_ABS if m.denominator else SUM_ABS)
                c.outcome = "agrees" if abs(c.platform - c.expected) <= max(tolerance, 1e-9) else "differs"
            if c.outcome in ("differs", "missing") and (reason := _known(c, known)):
                c.outcome = "known"
                notes.insert(0, reason)
            c.note = "; ".join(notes)
    return report
