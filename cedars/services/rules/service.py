"""Rule sets as data, run by the engine's rule runner (recet.filing_rules.check_frame) so the platform, ReCET runs and the pre-upload
checker apply exactly the same rules. The engine is imported only when rules are run or imported from it (engine/README.md)."""

import io
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import Rule, RuleSet

MODULES_FOR = {"claims": ["claims", "calculations"], "budget_filing": ["budgets", "calculations"]}
LABELS = {"claims": "Quarterly claims", "budget_filing": "Budget and application filings"}


class EngineMissing(RuntimeError):
    """The pinned ReCET engine is not installed in this environment."""


def _engine():
    try:
        from recet import fieldnames, filing_rules
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise EngineMissing("The ReCET engine is not installed here (engine/README.md), so rules cannot be run.") from e
    return filing_rules, fieldnames


def suggested_fix(kind: str, field: str, definition: dict, message: str) -> str:
    """A plain-language fix for a finding, from the kind of rule (BR-03.2: every finding says how to fix it)."""
    check = definition.get("check", "")
    if kind == "spec":
        return {
            "not_empty": f"Fill in {field}.",
            "max_length": f"Shorten {field} to {definition.get('n')} characters or fewer.",
            "numeric": f"Enter a plain number in {field}: no text, units, commas or currency symbols.",
            "bit": f"Enter 0 or 1 (or True or False) in {field}.",
            "year_quarter": f"Write {field} as a year and quarter, such as 2028Q1.",
            "value_list": f"Use a code from the {definition.get('list')} value list that is current for the claim year.",
            "range": f"Enter a value for {field} within the allowed range ({message.split(' should be ', 1)[-1]}).",
            "starts_with": f"Start {field} with {definition.get('pattern')}.",
            "unique": f"Make each combination of {field} appear only once.",
        }.get(check, f"Correct {field}.")
    return f"Change {field} so that this holds: {message[:1].lower() + message[1:]}."


# --- rule sets


def in_force(code: str, on: date | None = None) -> RuleSet | None:
    on = on or timezone.localdate()
    return RuleSet.objects.filter(code=code, status=RuleSet.Status.PUBLISHED, effective_from__lte=on).order_by("-effective_from").first()


def _require_admin(actor):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator manages rule sets.")


@transaction.atomic
def import_from_engine(actor, code: str, as_of: str | None = None) -> RuleSet:
    """A draft rule set from the CEDARS rule files the pinned engine ships (data/cet_rules/<code>_<date>/)."""
    _require_admin(actor)
    filing_rules, _ = _engine()
    ers = filing_rules.rule_set(code, as_of)
    effective = date.fromisoformat(ers.version)
    if RuleSet.objects.filter(code=code, effective_from=effective).exists():
        raise AccessDenied(f"The {code} rules effective {ers.version} are already in the library.")
    rs = RuleSet.objects.create(
        code=code,
        label=LABELS.get(code, code),
        effective_from=effective,
        created_by=actor,
        source=f"CEDARS' rule files of {ers.version} ({', '.join(ers.describe()['files'])})",
    )
    Rule.objects.bulk_create(
        Rule(
            rule_set=rs,
            rule_id=r.id,
            severity=r.severity,
            table=r.table,
            field=r.field,
            message=canonical_text(r.text),
            kind=r.kind,
            definition=r.spec,
            source=r.source,
            modules=MODULES_FOR.get(code, []),
            fix=suggested_fix(r.kind, canonical_field(r.field), r.spec, canonical_text(r.text)),
        )
        for r in ers.rules
    )
    audit.record("rule_set.imported", obj=rs, after={"code": code, "effective_from": ers.version, "rules": len(ers.rules)}, actor=actor)
    return rs


@transaction.atomic
def edit_rule(
    actor,
    rule: Rule,
    *,
    message: str | None = None,
    fix: str | None = None,
    difference: str | None = None,
    modules: list[str] | None = None,
) -> Rule:
    """Change a draft rule's wording or module notes. The test itself (its definition) is never edited: a new rule file is a new set."""
    _require_admin(actor)
    if RuleSet.objects.get(pk=rule.rule_set_id).status != RuleSet.Status.DRAFT:
        raise AccessDenied("A published rule set never changes; import or copy a new version.")
    before = audit.snapshot(rule, ["message", "fix", "difference", "modules"])
    for name, value in (("message", message), ("fix", fix), ("difference", difference), ("modules", modules)):
        if value is not None:
            setattr(rule, name, value)
    rule.save()
    audit.record(
        "rule.edited", obj=rule, before=before, after=audit.snapshot(rule, ["message", "fix", "difference", "modules"]), actor=actor
    )
    return rule


@transaction.atomic
def publish(actor, rs: RuleSet) -> RuleSet:
    _require_admin(actor)
    rs = RuleSet.objects.select_for_update().get(pk=rs.pk)
    if rs.status != RuleSet.Status.DRAFT:
        raise AccessDenied("That rule set is already published.")
    rs.status, rs.published_by, rs.published_at = RuleSet.Status.PUBLISHED, actor, timezone.now()
    rs.save()
    audit.record("rule_set.published", obj=rs, after={"rules": rs.rules.count()}, actor=actor)
    return rs


def differences(a: RuleSet, b: RuleSet) -> dict[str, list[Rule]]:
    """Rules in one set and not the other (by stable ID), and rules in both whose module notes record a deliberate difference."""
    ids_a, ids_b = set(a.rules.values_list("rule_id", flat=True)), set(b.rules.values_list("rule_id", flat=True))
    return {
        "only_a": list(a.rules.filter(rule_id__in=ids_a - ids_b)),
        "only_b": list(b.rules.filter(rule_id__in=ids_b - ids_a)),
        "noted": list(a.rules.filter(rule_id__in=ids_a & ids_b).exclude(difference="")),
    }


# --- running rules


@dataclass
class Finding:
    """One problem in one record, in the format every module reports: row, record, field, rule, severity, message, fix."""

    row: int  # the file's line number (header = line 1)
    record_id: str
    table: str
    field: str
    rule_id: str
    severity: str
    message: str
    fix: str
    value: str = ""


def _engine_rule_set(rs: RuleSet):
    filing_rules, _ = _engine()
    rules = [filing_rules.Rule(r.rule_id, r.severity, r.table, r.field, r.message, r.kind, r.source, r.definition) for r in rs.rules.all()]
    return filing_rules.RuleSet(rs.code, rs.effective_from.isoformat(), None, rules)


def read_table(content: bytes):
    """An uploaded CSV as raw text (pipe- or comma-delimited, UTF-8 or Windows-1252), columns under the canonical names."""
    import pandas as pd

    _, fieldnames = _engine()
    text = content.decode("utf-8-sig", errors="strict") if _is_utf8(content) else content.decode("cp1252")
    header = text.split("\n", 1)[0]
    sep = "|" if "|" in header else ("\t" if "\t" in header else ",")
    df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    return fieldnames.to_recet(df)


def _is_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# The engine's note when the profile-combination rules cannot run, and what it means on this server.
_NO_LIBRARY = "the CEDARS impact-profile library is not installed"
_NO_LIBRARY_HERE = (
    "the impact-profile library is not in the engine's reference folder on this server (RECET_DATA for the web service); the "
    "profile-combination rules are checked when the job is priced"
)


def canonical_text(text: str) -> str:
    """A rule's wording with the data dictionary's field names (decision L): the first mention of a CET name becomes
    "AC_Climate_Zone (E3ClimateZone)", later mentions the canonical name alone. Idempotent."""
    import re

    _, fieldnames = _engine()
    for cet, ours in fieldnames.LEGACY_TO_RECET.items():
        if cet == ours or cet not in text:
            continue
        pattern = re.compile(rf"(?<![\w(]){re.escape(cet)}\b")
        first = True

        def swap(m, cet=cet, ours=ours):
            nonlocal first
            out = f"{ours} ({cet})" if first else ours
            first = False
            return out

        text = pattern.sub(swap, text)
    return text


def canonical_field(field: str) -> str:
    """A rule's field under the data dictionary's name (a two-column key keeps both names)."""
    _, fieldnames = _engine()
    return " + ".join(fieldnames.LEGACY_TO_RECET.get(part.strip(), part.strip()) for part in field.split("+")) if field else field


def library_available() -> bool:
    """Whether the engine on this server has the impact-profile library the profile-combination rules read."""
    try:
        from recet.pipeline import real_impact_profiles
    except ImportError:
        return False
    return bool(real_impact_profiles.is_available())


def check(rs: RuleSet, table: str, df, *, filing_year: int | None = None, id_column: str = "CEInputID") -> tuple[list[Finding], list[str]]:
    """Run a rule set's rules for `table` on a raw-text frame: every failing (record, rule) as a Finding, rejects first, and notes on rules
    that could not be checked. Findings name fields and wording with the data dictionary's names."""
    filing_rules, _ = _engine()
    ctx = filing_rules.Context(filing_year=filing_year, library=library_available())
    results, notes = filing_rules.check_frame(_engine_rule_set(rs), table, df, ctx)
    notes = [n.replace(_NO_LIBRARY, _NO_LIBRARY_HERE) for n in notes]
    fixes = dict(rs.rules.values_list("rule_id", "fix"))
    ids = df[id_column].astype(str).to_numpy() if id_column in df.columns else [""] * len(df)
    findings = []
    for res in sorted(results, key=lambda r: r.rule.severity != "reject"):
        actual = res.actual
        for i in res.fail.nonzero()[0]:
            value = "" if actual is None else str(actual.iloc[i]) if hasattr(actual, "iloc") else ""
            findings.append(
                Finding(
                    int(i) + 2,
                    ids[i],
                    table,
                    canonical_field(res.rule.field),
                    res.rule.id,
                    res.rule.severity,
                    canonical_text(res.rule.text) + (f", but is {value}" if value and value != "<NA>" else ""),
                    fixes.get(res.rule.id, ""),
                    "" if value == "<NA>" else value,
                )
            )
    return findings, notes


def findings_csv(findings: list[Finding]) -> str:
    import csv

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["line", "record_id", "table", "field", "rule_id", "severity", "message", "suggested_fix"])
    for f in findings:
        w.writerow([f.row, f.record_id, f.table, f.field, f.rule_id, f.severity, f.message, f.fix])
    return out.getvalue()
