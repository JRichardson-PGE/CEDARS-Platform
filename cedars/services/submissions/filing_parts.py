"""The parts every filing page shares: claims, statewide claims, budget filings and monthly reports, and their version pages (UI review 3,
U1). Each module's page draws them with the same templates (`submissions/_filing_steps.html`, `_filing_findings.html`,
`_filing_pricing.html`), so the four pages say where a filing stands in the same words:

- the step a filing is at (Upload, Check, Price, Confirm, Published), said in words, and the status words both pages use;
- the findings grouped by rule: each rule links to its rule (a rule set's rule page, or the built-in checks), lists its records, each
  with a link to correct it for a person who may, and a column rule lists every missing column;
- while the filing is priced, its pricing run's state as JSON for the page to update itself (`pricing_state`, the
  submissions:pricing_status view)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse

from . import corrections
from . import service as submissions
from .models import SubmissionVersion

# what a filer is told after an upload with rejected records, on every filing page
REJECTED_NEXT = "The findings below say why: upload a corrected file, or correct the records one by one."

STEPS = ("Upload", "Check", "Price", "Confirm", "Published")


def records_shown() -> int:
    """Records listed under each rule on a filing page (CEDARS_FINDING_RECORDS_SHOWN); the rest are counted and in the CSV."""
    return settings.CEDARS_FINDING_RECORDS_SHOWN


def status_words(v: SubmissionVersion | None, priced: bool) -> str:
    """A version's status as the filing page and its version page both say it: "Validated and priced" once a validated version is
    priced, else its status."""
    if v is None:
        return "Not started"
    if v.status == SubmissionVersion.Status.VALIDATED and priced:
        return "Validated and priced"
    return v.get_status_display()


@dataclass
class Stage:
    """Where a filing stands: `key` names the state the page's outcome card shows (upload, fix, stale, price, nothing, pricing,
    failed, confirm, confirmed, published, legacy), `step` the step line's current step (0-4), `words` the step in a sentence."""

    key: str
    step: int
    words: str
    finished: bool = False

    @property
    def steps(self) -> list[dict]:
        out = []
        for i, name in enumerate(STEPS):
            done = i < self.step or (self.finished and i == self.step)
            out.append({"n": i + 1, "name": name, "done": done, "current": i == self.step})
        return out

    @property
    def lead(self) -> str:
        return f"Step {self.step + 1} of {len(STEPS)}, {STEPS[self.step]}:"


def _n(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def stage(
    v: SubmissionVersion | None,
    *,
    has_file: bool,
    priced: bool,
    pricing: dict | None = None,
    stale: bool = False,
    nothing_to_price: bool = False,
    reject_rules: int = 0,
) -> Stage:
    """The filing's stage, from its latest version: `has_file` (it has an upload), `priced` (the module's own test), `pricing` (the
    submissions service's pricing_run state), `stale` (Claims: compiled over an earlier confirmation that has changed),
    `nothing_to_price` (Claims: no measures in the year to date), `reject_rules` (how many rules rejected records)."""
    Status = SubmissionVersion.Status
    if v is not None and v.imported:
        return Stage("legacy", 4, "imported from legacy CEDARS, read-only.", finished=True)
    if v is None or not has_file:
        return Stage("upload", 0, "nothing has been uploaded yet.")
    if v.status == Status.PUBLISHED:
        when = f" on {v.confirmed_at:%Y-%m-%d}" if v.confirmed_at else ""
        return Stage("published", 4, f"version {v.number} was confirmed{when} and is published.", finished=True)
    if v.status == Status.CONFIRMED:
        return Stage("confirmed", 4, f"version {v.number} is confirmed; it is published once its publication check passes.")
    if stale:
        return Stage("stale", 1, "an earlier confirmation has changed since this version was compiled on it, so it is checked again.")
    if v.status == Status.DRAFT:
        rejected = (v.findings or {}).get("rejected", 0)
        if not v.findings:
            return Stage("fix", 1, "the upload has not been checked yet.")
        by = f" by {_n(reject_rules, 'rule')}" if reject_rules else ""
        were = "was" if rejected == 1 else "were"
        return Stage("fix", 1, f"{_n(rejected, 'record')} {were} rejected{by}. Fix them, then they are checked again.")
    if priced:
        return Stage("confirm", 3, "priced. Review the results, then confirm them to publish.")
    if pricing and not pricing.get("ended"):
        return Stage("pricing", 2, "being priced. This page updates itself when the run finishes.")
    if nothing_to_price:
        return Stage("nothing", 2, "the records passed their checks, but there are no measures to price yet.")
    if pricing:
        return Stage("failed", 2, "the pricing run ended without pricing this version. Price it again once what it says is put right.")
    warnings = (v.findings or {}).get("warnings", 0)
    also = f" ({_n(warnings, 'record')} with warnings only)" if warnings else ""
    return Stage("price", 2, f"every record passed its checks{also}. Price the version next; confirming needs it.")


# --- findings by rule


@dataclass
class RuleGroup:
    """One rule's findings on a version: its records (table, line, record id), or for a column rule the missing columns."""

    rule_id: str
    severity: str
    text: str  # the rule in words (the rule set's wording, else the first finding's message)
    fix: str
    url: str  # the rule's page, "" when it has none
    column_rule: bool
    records: list[dict] = field(default_factory=list)  # {label, url, message, line}
    count: int = 0
    columns: list[tuple[str, list[str]]] = field(default_factory=list)  # (table, its missing columns)

    @property
    def more(self) -> int:
        return max(self.count - len(self.records), 0)

    @property
    def unit(self) -> str:
        noun = "column" if self.column_rule else "record"
        return noun if self.count == 1 else noun + "s"


def _rule_set_texts(v: SubmissionVersion) -> dict[str, str]:
    if not v.rule_set_id:
        return {}
    from cedars.services.rules.models import Rule

    return dict(Rule.objects.filter(rule_set_id=v.rule_set_id).values_list("rule_id", "message"))


def _canonical(text: str) -> str:
    try:
        from cedars.services.rules import service as rules

        return rules.canonical_text(text)
    except Exception:  # noqa: BLE001 - the engine is not installed here: the text as stored
        return text


def rule_url(v: SubmissionVersion, rule_id: str, in_set: bool, coded: dict | None = None) -> str:
    """The page that says what a rule checks: the rule set's rule page for a rule of the set the version was checked against, else the
    built-in checks list when the platform's code raises it; "" when neither knows it."""
    if in_set and v.rule_set_id:
        return reverse("rules:rule", args=[v.rule_set_id, rule_id])
    return _coded_check_url(rule_id, coded)


def _coded_checks() -> dict:
    """The checks written in code (rules.coded), by rule ID."""
    from cedars.services.rules import coded

    return {c.rule_id: c for g in coded.groups() for c in g.checks}


def _coded_check_url(rule_id: str, known: dict | None = None) -> str:
    """The rule's row on the Built-in checks page, when the platform's code raises it; "" otherwise."""
    known = _coded_checks() if known is None else known
    return reverse("rules:built_in") + "#" + rule_id if rule_id in known else ""


def _key_query(t, key) -> str:
    return urlencode([("k", k) for k in key])


def findings_by_rule(v: SubmissionVersion, *, link_records: bool = False, limit: int | None = None) -> list[RuleGroup]:
    """The version's findings grouped by rule, rejects first, then the rules that touch the most records. Each rule lists its first
    `limit` records (records_shown()), linked to the record's correction page when `link_records`; a column rule lists every missing
    column by table. A record a rule flags twice is listed once."""
    e = corrections.editor_for(v.submission.deadline.module)
    found = e.findings(v) if e else []
    if not found:
        return []
    limit = records_shown() if limit is None else limit
    texts = _rule_set_texts(v)
    coded = _coded_checks()
    own, defs = {}, {}
    if link_records:
        try:
            own = e.rows(v)
            defs = {t.name: t for t in e.tables(v)}
        except Exception:  # noqa: BLE001 - a file that cannot be read now: list the records without links
            own, defs = {}, {}
    groups: dict[str, RuleGroup] = {}
    seen: dict[str, set] = {}
    columns: dict[str, dict[str, list[str]]] = {}
    for f in found:
        rid = str(f.get("rule_id", ""))
        g = groups.get(rid)
        if g is None:
            column_rule = rid.endswith("-COL")
            check = None if rid in texts else coded.get(rid)
            if texts.get(rid):
                text = _canonical(texts[rid])
            else:  # a check written in code says what it finds in general; else the first finding's own words
                text = check.finds if check else str(f.get("message", ""))
            g = groups[rid] = RuleGroup(
                rid,
                str(f.get("severity", "")),
                text,
                str(f.get("fix", "")) or (check.fix if check else ""),
                rule_url(v, rid, rid in texts, coded),
                column_rule,
            )
            seen[rid] = set()
        if f.get("severity") == "reject":
            g.severity = "reject"
        if g.column_rule:
            table, col = str(f.get("table", "")), str(f.get("field", ""))
            cols = columns.setdefault(rid, {}).setdefault(table, [])
            name = _canonical(col) if col else col
            if (table, col) not in seen[rid]:
                seen[rid].add((table, col))
                cols.append(name)
                g.count += 1
            continue
        key = (f.get("table"), f.get("row"), f.get("record_id"))
        if key in seen[rid]:
            continue
        seen[rid].add(key)
        g.count += 1
        if len(g.records) < limit:
            g.records.append(_record(v, f, own, defs, link_records))
    for rid, by_table in columns.items():
        groups[rid].columns = list(by_table.items())
    return sorted(groups.values(), key=lambda g: (g.severity != "reject", -g.count, g.rule_id))


def _record(v: SubmissionVersion, f: dict, own: dict, defs: dict, link: bool) -> dict:
    table, line, record_id = str(f.get("table", "")), f.get("row"), str(f.get("record_id", "") or "")
    url = ""
    t = defs.get(table)
    rows = own.get(table)
    row = rows.rows[line - 2] if rows and isinstance(line, int) and 2 <= line < len(rows.rows) + 2 else None
    if link and t is not None and row is not None:
        key = [row.get(c, "") for c in t.key]
        if all(k.strip() for k in key):
            url = reverse("submissions:record", args=[v.pk, table]) + "?" + _key_query(t, key)
            record_id = record_id or " / ".join(key)
    label = f"{table} {record_id}".strip() if record_id else (f"{table} line {line:,}" if isinstance(line, int) else table)
    return {"label": label, "url": url, "line": line if isinstance(line, int) and line > 1 else None, "message": str(f.get("message", ""))}


def reject_rules(groups: list[RuleGroup]) -> int:
    return sum(1 for g in groups if g.severity == "reject")


# --- the pricing run, while it runs


def pricing_state(v: SubmissionVersion, run_id: str) -> dict:
    """What a filing page shows while its version is priced by run `run_id` (the run the page named), and polls
    (submissions:pricing_status): whether pricing has ended (the version records its calculation, or that run ended or is gone) and
    the run's state in words."""
    if v.calculation:
        return {"ended": True, "priced": True, "status": "succeeded", "text": "Priced: the results are ready."}
    state = submissions.run_state(run_id) if submissions.started_run(v, run_id) else None
    if state is None:
        return {"ended": True, "priced": False, "status": "", "text": "The pricing run is no longer known."}
    label = (state.get("label") or state.get("status") or "").lower()
    if state.get("ended"):
        return {"ended": True, "priced": False, "status": state.get("status", ""), "text": f"The pricing run ended ({label})."}
    return {"ended": False, "priced": False, "status": state.get("status", ""), "text": f"Being priced: the run is {label}."}


# --- what every filing page's view passes its templates


def split_notes(notes) -> tuple[list[str], list[str]]:
    """A check's notes: (those the page shows, the rules the engine could not check here, which the page folds away)."""
    shown, unchecked = [], []
    for n in notes or []:
        (unchecked if re.match(r"^\S+ not checked: ", str(n)) else shown).append(str(n))
    return shown, unchecked


def context(
    v: SubmissionVersion | None,
    *,
    has_file: bool,
    priced: bool,
    pricing: dict | None = None,
    stale: bool = False,
    nothing_to_price: bool = False,
    can_correct: bool = False,
) -> dict:
    """The shared parts' context for a filing page: its stage and status words, its findings by rule (records linked when the person
    may correct them), the check's notes, where Correct records is, and whether the upload section is open."""
    groups = findings_by_rule(v, link_records=can_correct) if v is not None and not v.imported and v.findings else []
    st = stage(
        v,
        has_file=has_file,
        priced=priced,
        pricing=pricing,
        stale=stale,
        nothing_to_price=nothing_to_price,
        reject_rules=reject_rules(groups),
    )
    shown, unchecked = split_notes((v.findings or {}).get("notes") if v is not None else [])
    return {
        "stage": st,
        "status_words": status_words(v, priced),
        "rule_groups": groups,
        "run_notes": shown,
        "unchecked_notes": unchecked,
        "records_url": reverse("submissions:records", args=[v.pk]) if v is not None and can_correct else "",
        "upload_open": st.key in ("upload", "fix"),
    }


def version_page(request, v: SubmissionVersion, *, priced: bool, ready: bool = True) -> dict:
    """What a filing's version page shares with its filing page: the status words, and for the latest version, validated and priced
    (and `ready`: Claims, not compiled over a changed confirmation), a Confirm and publish button for someone who may confirm for the
    acting organization. The button posts to the filing's own confirm action, which confirms the latest version."""
    from cedars.services.accounts import service as access

    sub = v.submission
    latest = submissions.latest(sub)
    can = (
        latest is not None
        and latest.pk == v.pk
        and v.status == SubmissionVersion.Status.VALIDATED
        and priced
        and ready
        and sub.organisation_id == getattr(getattr(request, "organisation", None), "pk", None)
        and access.has_permission(request.user, sub.organisation, f"{sub.deadline.module}.confirm")
    )
    return {"status_words": status_words(v, priced), "can_confirm_here": can}
