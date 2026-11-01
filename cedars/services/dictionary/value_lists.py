"""Value lists (code sets) as versioned data an administrator edits (BR-12.6). The pinned engine ships CEDARS' lists
(recet/data/value_lists); a list's first version here is a copy of the shipped one, and each later version is a draft copied from the
newest published version, edited code by code (or from a CSV), and published with the date it takes effect and the reason.

Codes are never deleted: a code is retired by giving it an end year (the last claim year it is accepted), and reinstated by clearing it.
A draft may drop only a code it added itself. Every change is an audit event.

The rules (rules.service.check) and the input editor (calculations Edit inputs) read the lists through the engine, so `apply_to_engine()`
hands the engine the versions in force as changes over its shipped lists (recet.editor.set_value_list_changes). The engine is imported
only when a list is read from it or handed to it (engine/README.md)."""

import csv
import io
import threading
from datetime import date

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import Code, ValueList, ValueListVersion

Status = ValueListVersion.Status
# lists the engine checks by other means (PrgID against the program registry) or does not ship
NOT_EDITED_HERE = {"prgid", "avoidedcostcombo", "semphase"}
CSV_COLUMNS = ["code", "description", "start_year", "end_year"]
MAX_UPLOAD = 20 * 1024 * 1024


class EngineMissing(RuntimeError):
    """The pinned ReCET engine is not installed in this environment."""


class ValueListError(ValueError):
    """A change the value-list rules do not allow; the message says why and what to do instead."""


def _engine():
    try:
        from recet import editor, paths
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise EngineMissing("The ReCET engine is not installed here (engine/README.md), so its value lists cannot be read.") from e
    return editor, paths


def _require_admin(actor):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes value lists.")


def can_edit(user) -> bool:
    return bool(user and user.is_authenticated and access.is_system_admin(user))


# --- the engine's lists


def engine_lists() -> list[str]:
    """The names of the value lists the pinned engine ships."""
    from pathlib import Path

    _, paths = _engine()
    folder = Path(paths.package_data("value_lists", ""))
    return sorted(p.stem for p in folder.glob("*.csv") if p.stem not in NOT_EDITED_HERE)


def engine_release() -> str:
    try:
        import recet
    except ImportError:
        return ""
    return getattr(recet, "__version__", "")


def _year(value) -> int | None:
    v = str(value if value is not None else "").strip()
    if not v or v.lower() in ("nan", "none"):
        return None
    try:
        y = int(float(v))
    except ValueError as e:
        raise ValueListError(f"{v!r} is not a year.") from e
    if not 1990 <= y <= 2100:
        raise ValueListError(f"{y} is not a claim year CEDARS uses (1990 to 2100).")
    return y


def shipped(name: str) -> list[dict]:
    """The engine's list as shipped: one dict per code (code, description, start_year, end_year, extra), codes stripped and each kept
    once (the shipped ntg list has one code twice, once with a trailing tab)."""
    from pathlib import Path

    _, paths = _engine()
    path = Path(paths.package_data("value_lists", f"{name}.csv"))
    if not path.exists():
        raise ValueListError(f"The engine ships no value list called {name}.")
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out, seen = [], set()
    for row in rows:
        code = (row.get("code") or "").strip()
        if not code or code.lower() in seen:
            continue
        seen.add(code.lower())
        extra = {k: v for k, v in row.items() if k not in CSV_COLUMNS and k is not None and v not in ("", None)}
        out.append(
            {
                "code": code,
                "description": (row.get("description") or "").strip(),
                "start_year": _year(row.get("start_year")),
                "end_year": _year(row.get("end_year")),
                "extra": extra,
            }
        )
    return out


def label_for(name: str) -> str:
    """The field a list fills, by the data dictionary (its names and aliases, any case), else the list's own name."""
    from . import service

    f = service.resolve(name)
    return f.name if f is not None else name


def get_list(name: str) -> ValueList:
    """The list's record, made the first time it is asked for (only for a list the engine ships)."""
    vl = ValueList.objects.filter(name=name).first()
    if vl is not None:
        return vl
    if name not in engine_lists():
        raise ValueListError(f"There is no value list called {name}.")
    return ValueList.objects.create(name=name, label=label_for(name))


# --- versions


def in_force(vl: ValueList, on: date | None = None) -> ValueListVersion | None:
    on = on or timezone.localdate()
    return vl.versions.filter(status=Status.PUBLISHED, effective_from__lte=on).order_by("-effective_from", "-number").first()


def latest_published(vl: ValueList) -> ValueListVersion | None:
    return vl.versions.filter(status=Status.PUBLISHED).order_by("-number").first()


def draft_of(vl: ValueList) -> ValueListVersion | None:
    return vl.versions.filter(status=Status.DRAFT).first()


def _require_draft(version: ValueListVersion) -> None:
    if version.status != Status.DRAFT:
        raise ValueListError("A published version never changes: start a new version of the list and change that.")


def _rows(version: ValueListVersion) -> dict[str, dict]:
    return {
        c.code.lower(): {"code": c.code, "description": c.description, "start_year": c.start_year, "end_year": c.end_year}
        for c in version.codes.all()
    }


@transaction.atomic
def start_draft(actor, vl: ValueList) -> ValueListVersion:
    """A new draft: a copy of the newest published version, or of the engine's shipped list when none is published yet."""
    _require_admin(actor)
    vl = ValueList.objects.select_for_update().get(pk=vl.pk)
    if draft_of(vl):
        raise ValueListError(f"{vl} already has a draft: change that one, publish it or discard it.")
    base = latest_published(vl)
    number = (vl.versions.aggregate(n=Max("number"))["n"] or 0) + 1
    if base is not None:
        rows = [
            {"code": c.code, "description": c.description, "start_year": c.start_year, "end_year": c.end_year, "extra": c.extra}
            for c in base.codes.all()
        ]
        based_on = f"version {base.number}"
    else:
        rows = shipped(vl.name)
        based_on = f"the list shipped with ReCET {engine_release()}".strip()
    draft = ValueListVersion.objects.create(value_list=vl, number=number, based_on=based_on, created_by=actor)
    Code.objects.bulk_create(Code(version=draft, **r) for r in rows)
    audit.record(
        "value_list.draft_started",
        obj=draft,
        after={"list": vl.name, "number": number, "based_on": based_on, "codes": len(rows)},
        actor=actor,
    )
    return draft


def _check_years(start, end) -> None:
    if start is not None and end is not None and end < start:
        raise ValueListError(
            f"The end year ({end}) is before the start year ({start}): a code is accepted from its start year to its end year."
        )


CODE_FIELDS = ["code", "description", "start_year", "end_year"]


@transaction.atomic
def add_code(actor, draft: ValueListVersion, code: str, *, description: str = "", start_year=None, end_year=None, reason: str = "") -> Code:
    _require_admin(actor)
    _require_draft(draft)
    code = (code or "").strip()
    if not code:
        raise ValueListError("Give the code.")
    if len(code) > 200:
        raise ValueListError("A code is at most 200 characters.")
    if draft.codes.filter(code__iexact=code).exists():
        raise ValueListError(f"{code} is already in the list (codes are matched ignoring case): change it instead.")
    start, end = _year(start_year), _year(end_year)
    _check_years(start, end)
    c = Code.objects.create(
        version=draft, code=code, description=(description or "").strip(), start_year=start, end_year=end, added_in_draft=True
    )
    audit.record(
        "value_list.code_added",
        obj=draft,
        after={"list": draft.value_list.name, **audit.snapshot(c, CODE_FIELDS)},
        reason=reason,
        actor=actor,
    )
    return c


_UNCHANGED = object()


@transaction.atomic
def change_code(actor, c: Code, *, description=_UNCHANGED, start_year=_UNCHANGED, end_year=_UNCHANGED, reason: str = "") -> Code:
    """Correct a code's description, retire it (an end year) or reinstate it (no end year). A code carried from an earlier version
    keeps its start year: that is when CEDARS began to accept it."""
    _require_admin(actor)
    c = Code.objects.select_for_update().select_related("version__value_list").get(pk=c.pk)
    _require_draft(c.version)
    before = audit.snapshot(c, CODE_FIELDS)
    if description is not _UNCHANGED:
        c.description = (description or "").strip()
    if start_year is not _UNCHANGED:
        start = _year(start_year)
        if start != c.start_year and not c.added_in_draft:
            raise ValueListError(
                f"{c.code} came from an earlier version, so its start year stays {c.start_year or 'blank'}. "
                "Retire it and add a new code instead."
            )
        c.start_year = start
    if end_year is not _UNCHANGED:
        c.end_year = _year(end_year)
    _check_years(c.start_year, c.end_year)
    after = audit.snapshot(c, CODE_FIELDS)
    if after == before:
        return c
    c.save()
    action = (
        "value_list.code_retired"
        if before["end_year"] is None and c.end_year is not None
        else "value_list.code_reinstated"
        if before["end_year"] is not None and c.end_year is None
        else "value_list.code_changed"
    )
    audit.record(action, obj=c.version, before={"list": c.version.value_list.name, **before}, after=after, reason=reason, actor=actor)
    return c


@transaction.atomic
def drop_code(actor, c: Code, reason: str = "") -> None:
    """Take out a code this draft added. A code from an earlier version is never deleted: retire it instead."""
    _require_admin(actor)
    c = Code.objects.select_for_update().select_related("version__value_list").get(pk=c.pk)
    _require_draft(c.version)
    if not c.added_in_draft:
        raise ValueListError(f"{c.code} came from an earlier version, and codes are never deleted: retire it with an end year instead.")
    audit.record(
        "value_list.code_dropped",
        obj=c.version,
        before={"list": c.version.value_list.name, **audit.snapshot(c, CODE_FIELDS)},
        reason=reason,
        actor=actor,
    )
    c.delete()


def read_csv(content: bytes) -> list[dict]:
    """An uploaded list: a header with `code` and any of description, start_year, end_year (comma- or pipe-delimited)."""
    if len(content) > MAX_UPLOAD:
        raise ValueListError("That file is over 20 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    header = text.split("\n", 1)[0]
    reader = csv.DictReader(io.StringIO(text), delimiter="|" if "|" in header else ",")
    fields = [str(f).strip().lower() for f in reader.fieldnames or []]
    if "code" not in fields:
        raise ValueListError("The file needs a header row with a code column (and any of description, start_year, end_year).")
    rows = []
    for n, raw in enumerate(reader, start=2):
        row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        if not row.get("code"):
            continue
        try:
            rows.append(
                {
                    "code": row["code"],
                    "description": row.get("description"),
                    "start_year": _year(row.get("start_year")),
                    "end_year": _year(row.get("end_year")),
                    "has": {k for k in ("description", "start_year", "end_year") if k in fields},
                }
            )
        except ValueListError as e:
            raise ValueListError(f"Line {n}: {e}") from e
    return rows


@transaction.atomic
def upload_codes(actor, draft: ValueListVersion, content: bytes, reason: str = "") -> dict:
    """Add or change codes from a file: a code not yet in the list is added, a code in it takes the file's description and end year
    (and start year, for a code this draft added). Codes the file leaves out are left as they are: a file never deletes."""
    _require_admin(actor)
    _require_draft(draft)
    rows = read_csv(content)
    existing = {c.code.lower(): c for c in draft.codes.all()}
    added, changed, seen = [], [], set()
    for r in rows:
        key = r["code"].lower()
        if key in seen:
            raise ValueListError(f"{r['code']} is in the file twice.")
        seen.add(key)
        c = existing.get(key)
        if c is None:
            _check_years(r["start_year"], r["end_year"])
            added.append(
                Code(
                    version=draft,
                    code=r["code"],
                    description=r["description"] or "",
                    start_year=r["start_year"],
                    end_year=r["end_year"],
                    added_in_draft=True,
                )
            )
            continue
        before = audit.snapshot(c, CODE_FIELDS)
        if "description" in r["has"]:
            c.description = r["description"] or ""
        if "end_year" in r["has"]:
            c.end_year = r["end_year"]
        if "start_year" in r["has"] and r["start_year"] != c.start_year:
            if not c.added_in_draft:
                raise ValueListError(f"{c.code} came from an earlier version, so its start year stays {c.start_year or 'blank'}.")
            c.start_year = r["start_year"]
        _check_years(c.start_year, c.end_year)
        after = audit.snapshot(c, CODE_FIELDS)
        if after != before:
            changed.append((c, before, after))
    Code.objects.bulk_create(added)
    if changed:
        Code.objects.bulk_update([c for c, _, _ in changed], ["description", "start_year", "end_year"])
    summary = {
        "list": draft.value_list.name,
        "added": [c.code for c in added],
        "changed": [{"before": b, "after": a} for _, b, a in changed],
    }
    audit.record("value_list.codes_uploaded", obj=draft, after=summary, reason=reason, actor=actor)
    return {"added": len(added), "changed": len(changed), "unchanged": len(rows) - len(added) - len(changed)}


def differences(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list]:
    """What a version changes against the one it follows: codes added, retired, reinstated, otherwise changed."""
    out = {"added": [], "retired": [], "reinstated": [], "changed": []}
    for key, row in sorted(new.items()):
        before = old.get(key)
        if before is None:
            out["added"].append(row)
        elif before["end_year"] is None and row["end_year"] is not None:
            out["retired"].append(row)
        elif before["end_year"] is not None and row["end_year"] is None:
            out["reinstated"].append(row)
        elif before != row:
            out["changed"].append({"before": before, "after": row})
    return out


def base_rows(version: ValueListVersion) -> dict[str, dict]:
    """The codes of the version a draft or version was copied from (the previous version, or the engine's list for version 1)."""
    prev = version.value_list.versions.filter(status=Status.PUBLISHED, number__lt=version.number).order_by("-number").first()
    if prev is not None:
        return _rows(prev)
    try:
        return {r["code"].lower(): {k: r[k] for k in CODE_FIELDS} for r in shipped(version.value_list.name)}
    except (EngineMissing, ValueListError):
        return {}


def changes_in(version: ValueListVersion) -> dict[str, list]:
    return differences(base_rows(version), _rows(version))


@transaction.atomic
def publish(actor, draft: ValueListVersion, effective_from: date, reason: str) -> ValueListVersion:
    _require_admin(actor)
    draft = ValueListVersion.objects.select_for_update().select_related("value_list").get(pk=draft.pk)
    _require_draft(draft)
    if not (reason or "").strip():
        raise ValueListError("Say why the list changes: the reason is kept with the version.")
    if effective_from is None:
        raise ValueListError("Give the date the version takes effect.")
    latest = latest_published(draft.value_list)
    if latest is not None and effective_from <= latest.effective_from:
        raise ValueListError(
            f"Version {latest.number} takes effect {latest.effective_from:%Y-%m-%d}; a new version takes effect after that."
        )
    if latest is not None:
        kept = set(draft.codes.values_list("code", flat=True))
        lost = [c for c in latest.codes.values_list("code", flat=True) if c not in kept]
        if lost:  # pragma: no cover - drop_code refuses this; kept as the last guard
            raise ValueListError(f"Codes are never deleted, and the draft lacks {', '.join(lost[:5])}.")
    changes = changes_in(draft)
    Code.objects.filter(version=draft, added_in_draft=True).update(added_in_draft=False)  # before publishing: then nothing changes
    draft.status, draft.effective_from, draft.reason = Status.PUBLISHED, effective_from, reason.strip()
    draft.published_by, draft.published_at = actor, timezone.now()
    draft.save()
    audit.record(
        "value_list.published",
        obj=draft,
        after={
            "list": draft.value_list.name,
            "number": draft.number,
            "effective_from": effective_from.isoformat(),
            "codes": draft.codes.count(),
            **{k: len(v) for k, v in changes.items()},
        },
        reason=reason,
        actor=actor,
    )
    return draft


@transaction.atomic
def discard_draft(actor, draft: ValueListVersion, reason: str = "") -> None:
    _require_admin(actor)
    draft = ValueListVersion.objects.select_for_update().select_related("value_list").get(pk=draft.pk)
    _require_draft(draft)
    audit.record(
        "value_list.draft_discarded",
        obj=draft,
        before={"list": draft.value_list.name, "number": draft.number, "codes": draft.codes.count()},
        reason=reason,
        actor=actor,
    )
    draft.delete()


def codes_csv(rows) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        get = r.get if isinstance(r, dict) else lambda k, r=r: getattr(r, k)
        w.writerow([get("code"), get("description") or "", get("start_year") or "", get("end_year") or ""])
    return out.getvalue()


# --- handing the lists to the engine

_lock = threading.Lock()
_applied: tuple | None = None


def engine_changes(on: date | None = None) -> dict[str, dict[str, dict]]:
    """The versions in force on `on`, as the engine's changes over its shipped lists ({list: {code: change}}): a code the engine lacks
    is "added", one whose end year differs is "retired" or "reinstated". A code the engine ships that the version does not have (a newer
    engine's code, not yet added here) is retired before any year, so the rules and the editor accept exactly the version's codes."""
    out: dict[str, dict[str, dict]] = {}
    for vl in ValueList.objects.all():
        v = in_force(vl, on)
        if v is None:
            continue
        ours = {c.code.lower(): c for c in v.codes.all()}
        theirs = {r["code"].lower(): r for r in shipped(vl.name)}
        ch: dict[str, dict] = {}
        for key, c in ours.items():
            s = theirs.get(key)
            if s is None:
                ch[c.code] = {"kind": "added", "start_year": c.start_year, "end_year": c.end_year, "description": c.description}
            elif s["end_year"] != c.end_year:
                ch[c.code] = {"kind": "reinstated"} if c.end_year is None else {"kind": "retired", "end_year": c.end_year}
        for key, s in theirs.items():
            if key not in ours:
                ch[s["code"]] = {"kind": "retired", "end_year": 0}
        out[vl.name] = ch
    return out


def apply_to_engine(on: date | None = None) -> dict[str, int]:
    """Hand the engine the value lists in force (today unless `on` is given) before it checks a file or offers dropdowns. Cheap when
    nothing changed since the last call: one query for the versions in force. Returns {list: version number} for the lists handed over.
    Does nothing without the engine."""
    global _applied
    try:
        editor, _ = _engine()
    except EngineMissing:
        return {}
    forced = {vl.name: in_force(vl, on) for vl in ValueList.objects.all()}
    forced = {k: v for k, v in forced.items() if v is not None}
    key = (engine_release(), tuple(sorted((k, v.pk) for k, v in forced.items())))
    with _lock:
        if key != _applied:
            editor.set_value_list_changes(engine_changes(on))
            _applied = key
    return {k: v.number for k, v in forced.items()}


def reset_engine() -> None:
    """Forget what was handed to the engine (tests)."""
    global _applied
    with _lock:
        _applied = None
