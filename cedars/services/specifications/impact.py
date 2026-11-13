"""The impact test: how real filings already in CEDARS would fare under a version, before it is published.

The test takes, by default, the latest confirmed version of each organization's most recent filing of the specification's kind (`filings`),
filed on this platform (legacy CEDARS years imported as published were never checked here). Each is checked twice by the same checks a
filing gets: once under the version it was checked against, once under the version being tested, both today and on the same records, so
the difference is the version's alone. Nothing is changed: the filings, their findings and their records stay as they are.

Each filing module registers how its checks run on a confirmed filing's own records under a given version (`register_impact_checker`).
The records are laid out on the tested version's tables first (`refit`): a renamed field's column moves to its new name and a column the
version no longer has is dropped, as an upload would have them.

The report (`ImpactTest.report`) says, per organization and per rule, how many records that were not rejected before would now be rejected
(and not warned, now warned), how many would no longer be rejected, and which required fields the filings do not have or leave blank."""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass

from django.db import transaction

from cedars.services import audit

from . import service
from .models import ImpactTest

MAX_RULE_EXAMPLES = 5  # records shown under each rule: enough to find the pattern, not a findings list


@dataclass(frozen=True)
class ImpactChecker:
    """How a module checks a confirmed filing's records under a version: `rows(v)` gives the records the organization filed in submission
    version `v` ({table: (columns, [row dicts])}); `run(version, v, rows)` gives the findings (dataclasses or dicts with row, record_id,
    table, field, rule_id, severity, message) of the module's checks, its rule set and its built-in checks under `version`."""

    rows: object
    run: object
    list_rule: str = ""


_checkers: dict[str, ImpactChecker] = {}


def register_impact_checker(module: str, rows, run, *, list_rule: str = "") -> None:
    """Called by a filing module in its AppConfig.ready(): how the impact test reads a confirmed filing's records and checks them
    under a specification version, and the ID of its check on value lists made for a specification (`list_rule`), under which a value
    not on a list the draft makes, not yet published, is reported."""
    _checkers[module] = ImpactChecker(rows, run, list_rule)


# --- the filings tested


def filings(version) -> list:
    """The submission versions the test checks: for each organization, the latest confirmed version of its most recent filing of the
    version's module (by due date) that was filed on this platform and is on the calendar."""
    from cedars.services.submissions.models import SubmissionVersion

    published = (
        SubmissionVersion.objects.filter(
            status=SubmissionVersion.Status.PUBLISHED,
            origin=SubmissionVersion.Origin.FILED,
            submission__deadline__module=version.specification.module,
            submission__deadline__retired_at__isnull=True,
        )
        .select_related("submission__deadline__period", "submission__deadline__cycle", "submission__organisation", "specification_version")
        .order_by(
            "submission__organisation__code",
            "-submission__deadline__due_at",
            "-submission__deadline__period__year",  # filings due the same day: the later period
            "-submission__deadline__period__number",
            "-submission__deadline__pk",
            "-number",
        )
    )
    out, seen = [], set()
    for v in published:
        org = v.submission.organisation_id
        if org not in seen:
            seen.add(org)
            out.append(v)
    return out


def refit(version, rows: dict[str, tuple[list[str], list[dict]]]) -> dict[str, tuple[list[str], list[dict]]]:
    """A filing's records laid out on the version's tables: a column under another accepted name (a renamed field's old one) moved to the
    field, a column the table does not have dropped. Tables the version does not have are dropped; blank fields stay blank."""
    lay = service.layout(version)
    out = {}
    for name, (columns, records) in rows.items():
        t = lay.table(name)
        if t is None:
            continue
        moves = {}
        for c in columns:
            to = t.canonical(c)
            if to is not None and to not in moves.values():
                moves[c] = to
        out[name] = (list(moves.values()), [{to: r.get(c, "") for c, to in moves.items()} for r in records])
    return out


# --- running it


def _as_dict(f) -> dict:
    get = f.get if isinstance(f, dict) else lambda k: getattr(f, k, "")
    return {
        "row": get("row") or 0,
        "record_id": str(get("record_id") or ""),
        "table": get("table") or "",
        "field": get("field") or "",
        "rule_id": str(get("rule_id") or ""),
        "severity": "reject" if get("severity") == "reject" else "warning",
        "message": get("message") or "",
    }


def _year(v) -> int:
    d = v.submission.deadline
    return d.period.year if d.period_id else d.cycle.first_year


def _check(checker: ImpactChecker, version, v, filed) -> list[dict]:
    from . import examples

    rows = refit(version, copy.deepcopy(filed))  # a module may fill values in place (eTRM's deemed values): each check gets its own
    found = [_as_dict(f) for f in checker.run(version, v, rows)]
    # a list the draft makes is checked by the modules once published; until then, here (as on the Examples page)
    records = {t: recs for t, (_cols, recs) in rows.items()}
    made = examples.made_list_findings(version, records, _year(v), checker.list_rule or "FIELD")
    return found + [_as_dict(f) for f in made]


def _records(findings: list[dict], severity: str) -> set[tuple]:
    return {(f["table"], f["row"]) for f in findings if f["severity"] == severity and int(f["row"] or 0) > 1}


def _fields(version, filed) -> tuple[list[str], dict[str, int]]:
    """The version's required fields a filing's tables do not have, and those it has but leaves blank (with how many records)."""
    lay = service.layout(version)
    rows = refit(version, filed)
    absent, blank = [], {}
    for name, (columns, records) in rows.items():
        t = lay.table(name)
        for f in t.fields:
            if not f.required or f.requirement == "system":
                continue
            label = f"{name}.{f.name}"
            if f.name not in columns:
                absent.append(label)
                continue
            n = sum(1 for r in records if not str(r.get(f.name, "") or "").strip())
            if n:
                blank[label] = n
    return absent, blank


def _filing_label(v) -> str:
    return v.submission.deadline.plain_name(v.submission.organisation.code, due=False)


def check_filing(version, v) -> tuple[dict, list[dict], list[dict]]:
    """One filing under its own version and under `version`: its line of the report, and its findings before and after."""
    checker = _checkers.get(version.specification.module)
    deadline, org = v.submission.deadline, v.submission.organisation
    own = v.specification_version or service.of_filing(deadline) or version
    line = {
        "pa": org.code,
        "filing": _filing_label(v),
        "submission_version": v.pk,
        "number": v.number,
        "checked_with": own.name,
        "records": 0,
        "before": {"rejected": 0, "warned": 0},
        "after": {"rejected": 0, "warned": 0},
        "newly_rejected": 0,
        "newly_warned": 0,
        "cleared": 0,
        "absent": [],
        "blank": {},
        "note": "",
    }
    if checker is None:
        line["note"] = f"The {version.specification.name} checks cannot be run on a filing here."
        return line, [], []
    try:
        filed = checker.rows(v)
        line["records"] = sum(len(r) for _, r in filed.values())
        before = _check(checker, own, v, filed)
        after = _check(checker, version, v, filed)
    except Exception as e:  # noqa: BLE001 - what could not be checked is reported for that filing, and the others are still tested
        line["note"] = f"Its records could not be checked: {e}"
        return line, [], []
    absent_before, blank_before = _fields(own, filed)  # only what the version newly asks for: a filing lacked the rest before
    absent, blank = _fields(version, filed)
    line["absent"] = [x for x in absent if x not in absent_before]
    line["blank"] = {k: n for k, n in blank.items() if k not in blank_before}
    rejected_before, rejected_after = _records(before, "reject"), _records(after, "reject")
    warned_before = _records(before, "warning") - rejected_before
    warned_after = _records(after, "warning") - rejected_after
    line["before"] = {"rejected": len(rejected_before), "warned": len(warned_before)}
    line["after"] = {"rejected": len(rejected_after), "warned": len(warned_after)}
    line["newly_rejected"] = len(rejected_after - rejected_before)
    line["newly_warned"] = len(warned_after - warned_before - rejected_before)
    line["cleared"] = len(rejected_before - rejected_after)
    return line, before, after


def _new_findings(before: list[dict], after: list[dict]) -> list[dict]:
    """The findings under the tested version that the filing did not have under its own (by table, line, rule and severity)."""
    had = {(f["table"], f["row"], f["rule_id"], f["severity"]) for f in before}
    return [f for f in after if (f["table"], f["row"], f["rule_id"], f["severity"]) not in had]


def report_for(version, tested: list) -> dict:
    """The impact test's report for submission versions `tested` under `version`."""
    lines, rules = [], {}
    fields: dict[str, dict] = {}
    for v in tested:
        line, before, after = check_filing(version, v)
        lines.append(line)
        for f in _new_findings(before, after):
            key = (f["rule_id"], f["severity"])
            r = rules.setdefault(
                key,
                {
                    "rule_id": f["rule_id"],
                    "severity": f["severity"],
                    "table": f["table"],
                    "field": f["field"],
                    "message": f["message"],
                    "records": 0,
                    "by_pa": defaultdict(int),
                    "examples": [],
                },
            )
            r["records"] += 1
            r["by_pa"][line["pa"]] += 1
            if len(r["examples"]) < MAX_RULE_EXAMPLES:
                r["examples"].append({"pa": line["pa"], "row": f["row"], "record_id": f["record_id"], "message": f["message"]})
        for label in line["absent"]:
            fields.setdefault(label, {"field": label, "absent": [], "blank": {}})["absent"].append(line["pa"])
        for label, n in line["blank"].items():
            fields.setdefault(label, {"field": label, "absent": [], "blank": {}})["blank"][line["pa"]] = n
    ordered = sorted(rules.values(), key=lambda r: (r["severity"] != "reject", -r["records"], r["rule_id"]))
    for r in ordered:
        r["by_pa"] = dict(sorted(r["by_pa"].items()))
    return {
        "filings": lines,
        "rules": ordered,
        "fields": sorted(fields.values(), key=lambda x: x["field"]),
        "records": sum(x["records"] for x in lines),
        "newly_rejected": sum(x["newly_rejected"] for x in lines),
        "newly_warned": sum(x["newly_warned"] for x in lines),
        "cleared": sum(x["cleared"] for x in lines),
    }


def state(version) -> str:
    """What the test's result depends on in the version: its tables, fields, built-in checks and settings (not its examples or
    wording), its rules, and the codes of the value lists it makes."""
    import hashlib
    import json

    from . import examples

    doc = service.as_document(version)
    doc.pop("examples", None)
    for k in ("name", "slug", "summary", "source"):
        doc["version"].pop(k, None)
    rules = (
        sorted(version.rule_set.rules.values_list("rule_id", "severity", "table", "field", "definition"), key=lambda r: r[0])
        if version.rule_set_id
        else []
    )
    from cedars.services.dictionary.models import ValueList

    named = {f["value_list"].lower() for t in doc["tables"] for f in t["fields"] if f.get("value_list")}
    made = ValueList.objects.exclude(made_for="").values_list("name", flat=True)
    lists = {n: sorted(examples._codes(n, version, examples.example_year())) for n in made if n.lower() in named}
    return hashlib.sha256(json.dumps([doc, rules, lists], sort_keys=True, default=str).encode()).hexdigest()


@transaction.atomic
def run(actor, version) -> ImpactTest:
    """Run the impact test on a version (a draft before it is published) and keep its report. Nothing else changes."""
    from .editor import _require

    _require(actor)
    tested = filings(version)
    report = report_for(version, tested)
    test = ImpactTest.objects.create(version=version, created_by=actor, state=state(version), report=report)
    audit.record(
        "specification.impact_tested",
        obj=version,
        after={
            "filings": len(tested),
            "records": report["records"],
            "newly_rejected": report["newly_rejected"],
            "newly_warned": report["newly_warned"],
        },
        actor=actor,
    )
    return test


def latest(version) -> ImpactTest | None:
    return version.impact_tests.order_by("-created_at", "-pk").first()


def stale(version) -> bool:
    """Whether the version has no impact test for it as it now stands."""
    test = latest(version)
    return test is None or test.state != state(version)
