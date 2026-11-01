"""Correcting a filing's records in place (BR-02.4; the architecture's Figure 2: "a cell corrected in place: that row re-validated").

An authorized submitter (`<module>.submit` in the filing organization) corrects single records of an open draft (a version not yet
confirmed, or the new draft an un-confirm or a reopen leaves) without uploading the whole file again: finds a record, changes it on a
form, adds one or deletes one, or uploads a partial correction (only the changed and new records, with a Delete table naming the records
to remove), previewed before it is applied. Versions, not edits: a confirmed version never changes, so every path here refuses one.

What this service does for every module:

- reads the version's records through the module's editor (`Editor`, registered with `register`): each table's key columns and fields;
- finds records by key or any value, in the version's own records and, where the module has them, in the records filed before it that a
  correction may carry in (Claims: the year so far, `Editor.earlier`; a correction of one of them adds the corrected record to this
  version, which then stands over it, and for a table whose rows for a claim are one set, `TableDef.group`, the claim's other rows too);
- applies the changes to the rows (`apply`), keeping each row's place, so the module knows exactly which lines changed and where every
  other line moved (`Scope`): it re-checks only the changed records and those whose checks they can affect (the same key, a reference to
  them, a program-year total), and carries every other record's findings over;
- writes one audit event per record (`submission.record_corrected`: the table, the key, what changed, the record before and after, and
  how: the record editor or a partial correction with its file's SHA-256), before the module writes the new file set (with its SHA-256)
  through `attach_files` and records the check (`record_validation`), so the version's calculation is void and it is priced again;
- keeps a partial correction's file under the version until it is applied or replaced (`preview`, `apply_preview`), and applies it only
  to the records it was previewed against (`state`).

The change letter and the refile notice of a published correction summarize what was corrected in place (`summary`)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from django.conf import settings
from django.db import transaction

from cedars.core import storage
from cedars.core import uploads as core_uploads
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import SubmissionVersion

ACTION = "submission.record_corrected"
DELETE_TABLE = "Delete"
DELETE_NAMES = {"delete", "deletes", "deleted", "deleterecords", "delete_records", "deletions", "remove", "removed"}
KINDS = {"edit": "Changed", "add": "Added", "delete": "Deleted", "carry": "Carried in unchanged"}


class CorrectionRefused(ValueError):
    """A correction that cannot be made as given; str(e) is the sentence the page shows."""


def max_rows() -> int:
    """Records one partial correction may change (CEDARS_CORRECTION_MAX_ROWS); a larger correction is a new upload."""
    return settings.CEDARS_CORRECTION_MAX_ROWS


def list_rows() -> int:
    """Records a correction page lists at once (found records, a preview's changes): CEDARS_CORRECTION_LIST_ROWS."""
    return settings.CEDARS_CORRECTION_LIST_ROWS


# --- what a module tells the service


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str = "Text"  # as the form says it: "Number", "Whole number", "0 or 1", "Date (YYYY-MM-DD)" ...
    required: bool = False
    description: str = ""
    choices: tuple[str, ...] = ()  # a value list's codes, when the field takes one


@dataclass(frozen=True)
class TableDef:
    name: str
    key: tuple[str, ...]
    fields: tuple[FieldDef, ...]
    group: tuple[str, ...] = ()  # rows sharing these columns are one set (a later set replaces an earlier one whole)
    note: str = ""

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> FieldDef | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass
class Rows:
    """One table of a version, as text: rows keyed by column name, in file order (row i is line i + 2 of the file)."""

    name: str
    columns: list[str]
    rows: list[dict[str, str]]


class Editor:
    """What a module supplies (subclass it and `register` an instance). Every method gets an open, filed version."""

    module = ""
    label = ""
    earlier_delete = "It was filed before this version, so it is not one of this version's records to delete."

    def tables(self, v: SubmissionVersion) -> list[TableDef]:
        raise NotImplementedError

    def rows(self, v: SubmissionVersion) -> dict[str, Rows]:
        """The version's own records, by table."""
        raise NotImplementedError

    def earlier(self, v: SubmissionVersion) -> tuple[str, dict[str, Rows]]:
        """(where they come from, their tables): records filed before this version that a correction may carry into it. None by
        default."""
        return "", {}

    def prepare(self, v: SubmissionVersion) -> None:
        """Bring an open version up to date before its records are shown (e.g. check a reopened version's inherited upload)."""

    def refusal(self, v: SubmissionVersion) -> str:
        """Why the module will not take a correction on the version now ("" = it will)."""
        return ""

    def read_upload(self, data: bytes, filename: str) -> dict[str, Rows]:
        """A partial correction's tables, read as the module reads an upload (canonical column names)."""
        raise NotImplementedError

    def table_of(self, name: str) -> str | None:
        """The table a file or sheet name holds."""
        raise NotImplementedError

    def save(self, actor, v: SubmissionVersion, tables: dict[str, Rows], scope: Scope) -> SubmissionVersion:
        """Write the corrected records as the version's new file set (attach_files), re-check what `scope` says changed, carrying the
        rest of the findings over, and record the check (record_validation). Returns the version."""
        raise NotImplementedError

    def findings(self, v: SubmissionVersion) -> list[dict]:
        """The version's findings: dicts with table, row (the line), field, rule_id, severity, message, fix."""
        return []

    def version_page(self, v: SubmissionVersion) -> str:
        return ""


TYPE_WORDS = {"number": "Number", "boolean": "0 or 1", "year_quarter": "Year and quarter (e.g. 2028Q1)", "text": "Text"}


def file_fields(columns: list[str], *, required=(), numbers=(), descriptions: dict | None = None) -> tuple[FieldDef, ...]:
    """The fields of a table whose columns are the filed file's own (Budgets, Monthly reports: CET's job tables, whatever columns the
    file has): the required columns first, then the file's in its order, each described from the data dictionary in force when it names
    the column, else from `descriptions`."""
    from django.db.models.functions import Lower

    from cedars.services.dictionary import service as dictionary
    from cedars.services.dictionary.models import Field

    names: list[str] = []
    for c in [*required, *columns]:
        if c and c.lower() not in {n.lower() for n in names}:
            names.append(c)
    known: dict = {}
    catalog = dictionary.current()
    if catalog is not None:
        low = [n.lower() for n in names]
        for f in Field.objects.filter(version=catalog).annotate(low=Lower("name")).filter(low__in=low):
            known[f.low] = f
    need = {r.lower() for r in required}
    num = {n.lower() for n in numbers}
    out = []
    for n in names:
        d = known.get(n.lower())
        kind = "Number" if n.lower() in num else TYPE_WORDS.get(d.data_type, "Text") if d else "Text"
        text = (d.definition if d else "") or (descriptions or {}).get(n, "")
        out.append(FieldDef(n, kind, n.lower() in need, text))
    return tuple(out)


def csv_bytes(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    """A table written back as a comma-delimited CSV (UTF-8), the columns in order: how a corrected table is kept."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") for c in columns])
    return out.getvalue().encode("utf-8")


def canonical(columns: list[str], names) -> dict[str, str]:
    """{a file's column: its canonical name} for the columns that are one of `names` in another case (CET's files vary)."""
    by_low = {n.lower(): n for n in names}
    return {c: by_low[c.lower()] for c in columns if c.lower() in by_low and by_low[c.lower()] != c}


class FileTables(Editor):
    """An editor for a module whose records are CSV tables kept whole with each version (Budgets, Monthly reports): the version's own
    records are its file's, with no earlier records to carry in, and the fields are the file's own columns (CET's job tables carry
    whatever columns the PA's file has). The module supplies its tables, keys, required and number columns, how it reads its files, and
    `store`, which keeps the corrected tables and checks them."""

    table_names: tuple[str, ...] = ()
    keys: dict[str, tuple[str, ...]] = {}
    required: dict[str, list[str]] = {}
    numbers: dict[str, list[str]] = {}
    descriptions: dict[str, dict[str, str]] = {}

    def files(self, v: SubmissionVersion) -> tuple[str, dict[str, bytes]]:
        """(the filed file's SHA-256, its tables as bytes); ("", {}) when there is none."""
        raise NotImplementedError

    def parse(self, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
        """One table's header and rows, as the module reads them."""
        raise NotImplementedError

    def read_tables(self, data: bytes, filename: str) -> dict[str, bytes]:
        """The tables a partial correction's file holds (not all need be there)."""
        raise NotImplementedError

    def store(self, actor, v: SubmissionVersion, tables: dict[str, bytes], scope: Scope) -> SubmissionVersion:
        raise NotImplementedError

    def _table(self, name: str, content: bytes) -> Rows:
        header, rows = self.parse(content)
        known = [*self.keys.get(name, ()), *self.required.get(name, ()), *self.numbers.get(name, ())]
        ren = canonical(header, known)
        return Rows(name, [ren.get(c, c) for c in header], [{ren.get(k, k): x for k, x in r.items()} for r in rows])

    def rows(self, v):
        sha, tables = self.files(v)
        cached = _parsed.get((self.module, sha)) if sha else None
        if cached is None:
            cached = {t: self._table(t, c) for t, c in tables.items()}
            if sha:
                _parsed[(self.module, sha)] = cached
                while len(_parsed) > 4:
                    _parsed.pop(next(iter(_parsed)))
        return {t: Rows(t, list(r.columns), [dict(x) for x in r.rows]) for t, r in cached.items()}

    def tables(self, v):
        own = self.rows(v)
        return [
            TableDef(
                t,
                self.keys[t],
                file_fields(
                    own[t].columns if t in own else [],
                    required=self.required.get(t, ()),
                    numbers=self.numbers.get(t, ()),
                    descriptions=self.descriptions.get(t),
                ),
            )
            for t in self.table_names
        ]

    def read_upload(self, data, filename):
        return {t: self._table(t, c) for t, c in self.read_tables(data, filename).items()}

    def save(self, actor, v, tables, scope):
        own = self.rows(v)
        out = {}
        for t in self.table_names:
            r = tables.get(t) or own.get(t)
            if r is not None:
                out[t] = csv_bytes(r.columns, r.rows)
        return self.store(actor, v, out, scope)


_parsed: dict[tuple[str, str], dict[str, Rows]] = {}  # the last few files' tables, by module and SHA-256 (a file never changes)

_editors: dict[str, Editor] = {}


def register(editor: Editor) -> None:
    _editors[editor.module] = editor


def editor_for(module: str) -> Editor | None:
    return _editors.get(module)


def editor(v: SubmissionVersion) -> Editor:
    e = _editors.get(v.submission.deadline.module)
    if e is None:
        raise AccessDenied("This kind of filing is not corrected record by record; upload a corrected file instead.")
    return e


# --- keys


def norm(value: str) -> str:
    """Keys are compared as CEDARS compares them: without regard to case or surrounding spaces."""
    return (value or "").strip().lower()


def key_of(t: TableDef, row: dict) -> tuple[str, ...]:
    return tuple(norm(row.get(c, "")) for c in t.key)


def shown_key(t: TableDef, row: dict) -> str:
    return " / ".join(row.get(c, "") for c in t.key)


def group_of(t: TableDef, row: dict) -> tuple[str, ...]:
    return tuple(norm(row.get(c, "")) for c in t.group)


# --- who may correct what


def refusal(user, v: SubmissionVersion) -> str:
    """Why `user` may not correct the version's records now ("" = they may)."""
    sub = v.submission
    if v.imported or sub.versions.filter(origin=SubmissionVersion.Origin.IMPORTED).exists():
        return "This filing was imported from legacy CEDARS. It is read-only."
    if v.locked:
        return (
            "A confirmed version never changes. Un-confirm it before the due date, or ask to reopen it after; the new draft can be "
            "corrected."
        )
    latest = sub.versions.order_by("-number").first()
    if latest is not None and latest.pk != v.pk:
        return f"Version {latest.number} is the latest; correct that one."
    if user is not None and not access.has_permission(user, sub.organisation, f"{sub.deadline.module}.submit"):
        return f"You need {sub.deadline.module}.submit in {sub.organisation.code} to correct its records."
    e = editor_for(sub.deadline.module)
    if e is None:
        return "This kind of filing is not corrected record by record; upload a corrected file instead."
    if not v.files:
        return "Upload the filing first; then its records can be corrected one by one."
    return e.refusal(v)


def _require(user, v: SubmissionVersion) -> Editor:
    why = refusal(user, v)
    if why:
        raise AccessDenied(why)
    return editor(v)


def state(v: SubmissionVersion) -> str:
    """The version's records as they stand, as a short hash of its files: a form or preview is applied only to what it was made on."""
    files = [(f.get("key", ""), f.get("sha256", "")) for f in v.files or []]
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()[:16]


def _require_state(v: SubmissionVersion, expected: str) -> None:
    if expected and expected != state(v):
        raise CorrectionRefused(
            "The records changed after you opened this page (another correction or upload). Open the record again and make the "
            "change on what stands now."
        )


# --- finding records


@dataclass
class Found:
    table: str
    key: tuple[str, ...]  # as filed, one value per key column
    row: dict[str, str]
    line: int | None  # its line in the version's own table; None for an earlier record
    earlier: str = ""  # where an earlier record comes from

    @property
    def shown(self) -> str:
        return " / ".join(self.key)


def tables_of(v: SubmissionVersion) -> dict[str, TableDef]:
    return {t.name: t for t in editor(v).tables(v)}


def find(v: SubmissionVersion, table: str = "", query: str = "", limit: int | None = None) -> tuple[list[Found], int]:
    """Records whose key or any value contains `query` (without regard to case), in `table` or every table: the version's own first,
    then earlier records not overridden by one of its own. Returns (the first `limit`, how many match)."""
    e = editor(v)
    defs = {t.name: t for t in e.tables(v)}
    own = e.rows(v)
    where, earlier = e.earlier(v)
    q = norm(query)
    limit = list_rows() if limit is None else limit
    out, n = [], 0

    def hit(t: TableDef, r: dict) -> bool:
        return not q or any(q in norm(r.get(c, "")) for c in [*t.key, *t.columns])

    for name, t in defs.items():
        if table and name != table:
            continue
        mine = own.get(name)
        keys = set()
        for i, r in enumerate(mine.rows if mine else [], start=2):
            keys.add(key_of(t, r))
            if t.group:
                keys.add(("group",) + group_of(t, r))
            if hit(t, r):
                n += 1
                if len(out) < limit:
                    out.append(Found(name, tuple(r.get(c, "") for c in t.key), r, i))
        for r in earlier.get(name, Rows(name, [], [])).rows:
            if key_of(t, r) in keys or (t.group and ("group",) + group_of(t, r) in keys):
                continue  # this version's own record stands over it
            if hit(t, r):
                n += 1
                if len(out) < limit:
                    out.append(Found(name, tuple(r.get(c, "") for c in t.key), r, None, where))
    return out, n


def record(v: SubmissionVersion, table: str, key: tuple[str, ...] | list[str]) -> Found | None:
    """The record with this key: the version's own, else an earlier one it may carry in; None when there is none."""
    e = editor(v)
    t = tables_of(v).get(table)
    if t is None:
        return None
    want = tuple(norm(k) for k in key)
    own = e.rows(v).get(table)
    for i, r in enumerate(own.rows if own else [], start=2):
        if key_of(t, r) == want:
            return Found(table, tuple(r.get(c, "") for c in t.key), r, i)
    where, earlier = e.earlier(v)
    if t.group and own and any(group_of(t, r) == group_of(t, dict(zip(t.key, key, strict=False))) for r in own.rows):
        return None  # this version's own set for the group stands over the earlier rows
    for r in earlier.get(table, Rows(table, [], [])).rows:
        if key_of(t, r) == want:
            return Found(table, tuple(r.get(c, "") for c in t.key), r, None, where)
    return None


def row_findings(v: SubmissionVersion, table: str, line: int | None) -> list[dict]:
    """The findings of one of the version's own records (by its line), and the table-wide ones (line 1)."""
    if line is None:
        return []
    return [f for f in editor(v).findings(v) if f.get("table") == table and f.get("row") in (line, 1)]


# --- applying changes


@dataclass
class Change:
    kind: str  # edit, add, delete, carry (an earlier record carried in unchanged with the record a correction changes)
    table: str
    key: str  # as shown
    before: dict | None
    after: dict | None
    earlier: str = ""  # where an earlier record it corrects was filed
    line: int | None = None  # its line in the corrected table (None for a deleted record)

    @property
    def fields(self) -> list[str]:
        """The fields whose value changed (for an edit or a carried-in correction)."""
        if self.before is None or self.after is None:
            return []
        names = list(dict.fromkeys([*self.before, *self.after]))
        return [c for c in names if (self.before.get(c) or "") != (self.after.get(c) or "")]


@dataclass
class Scope:
    """What a correction changed, for the module's check: per table, each old line's new line (None = deleted) and the new lines of the
    records changed, added or carried in; and the changes themselves."""

    line_map: dict[str, dict[int, int | None]] = field(default_factory=dict)
    changed: dict[str, set[int]] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)

    def lines(self, table: str) -> set[int]:
        return self.changed.get(table, set())

    @property
    def tables(self) -> set[str]:
        return {c.table for c in self.changes}


@dataclass
class Request:
    """One change asked for: an edit of the record with `key` (its new values; columns left out keep theirs), a new record, or a
    deletion."""

    kind: str  # edit, add, delete
    table: str
    key: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)


def _clean(t: TableDef, values: dict) -> dict[str, str]:
    return {c: str(values.get(c, "") or "").strip() for c in t.columns if c in values}


def apply(
    defs: dict[str, TableDef],
    own: dict[str, Rows],
    earlier: tuple[str, dict[str, Rows]],
    requests: list[Request],
    earlier_delete: str = Editor.earlier_delete,
) -> tuple[dict[str, Rows], Scope]:
    """The version's tables with the requests applied, and what changed. Edited records keep their place; new and carried-in records
    go at the end of their table. Raises CorrectionRefused naming the first request that cannot be applied."""
    where, before_tables = earlier
    work: dict[str, list[tuple[int | None, dict]]] = {}
    columns: dict[str, list[str]] = {}
    for name in defs:
        r = own.get(name)
        work[name] = [(i, dict(row)) for i, row in enumerate(r.rows if r else [], start=2)]
        columns[name] = list(r.columns) if r else []
    changes: list[Change] = []
    touched: dict[str, set[int]] = {}  # ids of the rows changed: id(row dict)
    done: set[tuple[str, tuple]] = set()

    def find_own(t: TableDef, k: tuple) -> int | None:
        return next((n for n, (_, r) in enumerate(work[t.name]) if key_of(t, r) == k), None)

    def add_columns(name: str, values: dict) -> None:
        have = set(columns[name])
        for c in defs[name].columns:
            if c in values and c not in have:
                columns[name].append(c)
                have.add(c)

    def carry_set(t: TableDef, earlier_rows: list[dict], g: tuple, but: tuple) -> int:
        """A set's earlier rows (all but `but`) carried into the version, when it has none of the set's rows of its own: its set then
        replaces the earlier one whole. Returns how many (-1: the version has its own set already, or the table has no sets)."""
        if not t.group or any(group_of(t, r) == g for _, r in work[t.name]):
            return -1
        n = 0
        for sibling in earlier_rows:
            if group_of(t, sibling) == g and key_of(t, sibling) != but:
                copy = dict(sibling)
                add_columns(t.name, copy)
                work[t.name].append((None, copy))
                touched.setdefault(t.name, set()).add(id(copy))
                changes.append(Change("carry", t.name, shown_key(t, copy), sibling, copy, where))
                n += 1
        return n

    for req in requests:
        t = defs.get(req.table)
        if t is None:
            raise CorrectionRefused(f"There is no {req.table} table in this filing.")
        values = _clean(t, req.values)
        k = tuple(norm(x) for x in req.key) if req.key else key_of(t, values)
        shown = " / ".join(req.key) if req.key else shown_key(t, values)
        if (t.name, k) in done:
            raise CorrectionRefused(f"{t.name} {shown} is changed twice in one correction; keep one change for it.")
        done.add((t.name, k))
        if req.kind != "delete" and any(not values.get(c, "") for c in t.key) and (req.kind == "add" or any(c in values for c in t.key)):
            raise CorrectionRefused(f"A {t.name} record needs its key: {', '.join(t.key)}.")
        n = find_own(t, k)
        earlier_rows = before_tables.get(t.name, Rows(t.name, [], [])).rows
        filed = next((r for r in earlier_rows if key_of(t, r) == k), None)

        if req.kind == "delete":
            if n is None:
                if filed is None:
                    raise CorrectionRefused(f"{t.name} {shown} is not one of this version's records.")
                if not t.group:
                    raise CorrectionRefused(f"{t.name} {shown}: {earlier_delete}")
                carried = carry_set(t, earlier_rows, group_of(t, filed), k)
                if carried < 0:
                    raise CorrectionRefused(
                        f"{t.name} {shown} is not one of this version's records: its set here replaces the earlier one."
                    )
                if carried == 0:
                    raise CorrectionRefused(
                        f"{t.name} {shown} is the only record of its set filed in {where}, and a set cannot be left empty by a "
                        f"correction. {earlier_delete}"
                    )
                changes.append(Change("delete", t.name, shown_key(t, filed), filed, None, where))
                continue
            _, row = work[t.name].pop(n)
            changes.append(Change("delete", t.name, shown_key(t, row), row, None))
            continue
        if req.kind == "add" and n is not None:
            raise CorrectionRefused(f"{t.name} {shown} is already one of this version's records; change it instead of adding it.")
        if n is not None:  # an edit of the version's own record
            old_line, row = work[t.name][n]
            new = {**row, **values}
            if all((row.get(c) or "") == (new.get(c) or "") for c in new):
                continue  # it already holds these values
            new_key = key_of(t, new)
            if new_key != k and find_own(t, new_key) is not None:
                raise CorrectionRefused(f"{t.name} {shown_key(t, new)} is already one of this version's records; a key is used once.")
            add_columns(t.name, values)
            work[t.name][n] = (old_line, new)
            touched.setdefault(t.name, set()).add(id(new))
            changes.append(Change("edit", t.name, shown_key(t, new), row, new))
            continue
        if req.kind == "add" and filed is not None:
            raise CorrectionRefused(f"{t.name} {shown} was filed in {where}; find it and correct it instead of adding it again.")
        if req.kind == "edit" and filed is None:
            raise CorrectionRefused(f"{t.name} {shown} is not one of this filing's records.")
        new = {**(filed or {}), **values}
        if filed is not None and key_of(t, new) != k:
            raise CorrectionRefused(
                f"{t.name} {shown} was filed in {where}: its key stays. To replace it, withdraw or delete it and add the new record."
            )
        if filed is None and find_own(t, key_of(t, new)) is not None:
            raise CorrectionRefused(f"{t.name} {shown_key(t, new)} is already one of this version's records; change it instead.")
        # the set's other rows come too: this version's set replaces the earlier one whole
        carry_set(t, earlier_rows, group_of(t, new), key_of(t, new))
        add_columns(t.name, new)
        work[t.name].append((None, new))
        touched.setdefault(t.name, set()).add(id(new))
        changes.append(Change("edit" if filed is not None else "add", t.name, shown_key(t, new), filed, new, where if filed else ""))

    out: dict[str, Rows] = {}
    scope = Scope(changes=changes)
    for name, rows in work.items():
        line_map: dict[int, int | None] = {}
        r = own.get(name)
        for i in range(2, 2 + len(r.rows if r else [])):
            line_map[i] = None
        for new_line, (old_line, row) in enumerate(rows, start=2):
            if old_line is not None:
                line_map[old_line] = new_line
            if id(row) in touched.get(name, set()):
                scope.changed.setdefault(name, set()).add(new_line)
        scope.line_map[name] = line_map
        if rows or name in own:
            out[name] = Rows(name, columns[name] or defs[name].columns, [row for _, row in rows])
    for c in changes:
        if c.after is not None:
            rows = out[c.table].rows
            c.line = next((i for i, row in enumerate(rows, start=2) if row is c.after), None)
    return out, scope


@transaction.atomic
def _commit(
    actor, v: SubmissionVersion, requests: list[Request], *, via: dict, reason: str = "", expected: str = ""
) -> tuple[SubmissionVersion, Scope]:
    v = SubmissionVersion.objects.select_for_update().select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    e = _require(actor, v)
    _require_state(v, expected)
    defs = {t.name: t for t in e.tables(v)}
    tables, scope = apply(defs, e.rows(v), e.earlier(v), requests, e.earlier_delete)
    if not scope.changes:
        raise CorrectionRefused("Nothing changed: the records already hold these values.")
    if not any(t.rows for t in tables.values()):
        raise CorrectionRefused("A correction cannot remove every record. Upload a file instead.")
    org = v.submission.organisation
    for c in scope.changes:
        audit.record(
            ACTION,
            obj=v,
            before={"values": c.before} if c.before is not None else None,
            after={
                "table": c.table,
                "key": c.key,
                "change": c.kind,
                "fields": c.fields,
                "line": c.line,
                "earlier": c.earlier,
                "via": via,
                "values": c.after,
            },
            reason=reason,
            actor=actor,
            organisation=org if actor is None else None,
        )
    return e.save(actor, v, tables, scope), scope


def correct(actor, v: SubmissionVersion, table: str, key, values: dict, *, reason: str = "", expected: str = ""):
    """Change one record (the version's own, or an earlier one carried in corrected). Returns (the version, the scope)."""
    return _commit(actor, v, [Request("edit", table, tuple(key), values)], via={"how": "record editor"}, reason=reason, expected=expected)


def add(actor, v: SubmissionVersion, table: str, values: dict, *, reason: str = "", expected: str = ""):
    return _commit(actor, v, [Request("add", table, (), values)], via={"how": "record editor"}, reason=reason, expected=expected)


def delete(actor, v: SubmissionVersion, table: str, key, *, reason: str = "", expected: str = ""):
    return _commit(actor, v, [Request("delete", table, tuple(key))], via={"how": "record editor"}, reason=reason, expected=expected)


# --- partial corrections


@dataclass
class Preview:
    token: str  # the stored file's SHA-256
    filename: str
    state: str  # the records it was read against
    changes: list[Change]
    unchanged: int  # records in the file that already hold its values
    counts: dict[str, dict[str, int]]  # {table: {kind: n}}

    @property
    def total(self) -> int:
        return sum(1 for c in self.changes if c.kind != "carry")


def _preview_key(v: SubmissionVersion, token: str) -> str:
    return f"submissions/versions/{v.pk}/corrections/{token}"


def _split_deletes(data: bytes, filename: str, table_of) -> tuple[bytes | None, str, list[dict]]:
    """A partial correction's file without its Delete table, and the Delete table's rows. A lone CSV is one table, named by the file."""
    lower = filename.lower()
    if lower.endswith((".csv", ".txt")):
        stem = PurePosixPath(filename).stem
        if norm(stem).replace(" ", "") in DELETE_NAMES:
            return None, filename, _csv_dicts(data)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{core_uploads.table_for(stem, table_of) or stem}.csv", data)
        return out.getvalue(), f"{stem}.zip", []
    if lower.endswith(".xlsx"):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(data))
        except Exception:  # openpyxl raises several kinds for a file that is not a workbook
            raise CorrectionRefused("The file is not a valid Excel workbook (.xlsx).") from None
        deletes: list[dict] = []
        for ws in list(wb.worksheets):
            if norm(ws.title).replace(" ", "") in DELETE_NAMES:
                rows = [[core_uploads._cell(c) if c is not None else "" for c in r] for r in ws.iter_rows(values_only=True)]
                deletes += _dicts([r for r in rows if any(str(c).strip() for c in r)])
                wb.remove(ws)
        if not deletes:
            return data, filename, []
        others = [ws for ws in wb.worksheets if table_of(ws.title)]
        if not others:
            return None, filename, deletes
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue(), filename, deletes
    if lower.endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise CorrectionRefused("The file is not a valid .zip, .xlsx or .csv.") from None
        deletes, kept = [], []
        for info in z.infolist():
            stem = PurePosixPath(info.filename).stem
            if not info.is_dir() and norm(stem).replace(" ", "") in DELETE_NAMES:
                deletes += _csv_dicts(z.read(info))
            else:
                kept.append(info)
        if not deletes:
            return data, filename, []
        if not [i for i in kept if not i.is_dir()]:
            return None, filename, deletes
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            for info in kept:
                w.writestr(info, z.read(info))
        return out.getvalue(), filename, deletes
    raise CorrectionRefused(f"{filename} is not a .zip, .xlsx or .csv file.")


def _csv_dicts(content: bytes) -> list[dict]:
    return _dicts(core_uploads._csv_rows(content))


def _dicts(raw: list[list]) -> list[dict]:
    if not raw:
        return []
    head = [str(h).strip() for h in raw[0]]
    return [{h: str(r[i]).strip() if i < len(r) else "" for i, h in enumerate(head) if h} for r in raw[1:]]


def _delete_requests(defs: dict[str, TableDef], rows: list[dict], table_of) -> list[Request]:
    out = []
    for n, r in enumerate(rows, start=2):
        lower = {k.lower(): val for k, val in r.items()}
        name = table_of(lower.get("table", "")) if lower.get("table") else None
        t = defs.get(name or "")
        if t is None:
            raise CorrectionRefused(
                f"Line {n} of the Delete table names no table of this filing ({lower.get('table', '') or 'blank'}): give the table "
                "in its Table column."
            )
        key = tuple(lower.get(c.lower(), "") for c in t.key)
        if not all(key):
            raise CorrectionRefused(f"Line {n} of the Delete table: a {t.name} record is named by {', '.join(t.key)}; fill in each.")
        out.append(Request("delete", t.name, key))
    return out


def _requests(
    defs: dict[str, TableDef], tables: dict[str, Rows], deletes: list[Request], own: dict[str, Rows]
) -> tuple[list[Request], int]:
    """The partial correction's records as requests (matched on key: a record already in the version is an edit, any other an add or a
    corrected earlier record), less those that already hold the file's values; and how many those were."""
    out, unchanged = [], 0
    seen: dict[tuple, int] = {}
    for name, rows in tables.items():
        t = defs.get(name)
        if t is None:
            continue
        mine = {key_of(t, r): r for r in (own.get(name).rows if own.get(name) else [])}
        for i, r in enumerate(rows.rows, start=2):
            k = key_of(t, r)
            if not all(k):
                raise CorrectionRefused(f"{name} line {i} has no key: a correction's records are matched on {', '.join(t.key)}.")
            if (name, k) in seen:
                raise CorrectionRefused(f"{name} has {shown_key(t, r)} on lines {seen[(name, k)]} and {i}; keep one.")
            seen[(name, k)] = i
            values = {c: r.get(c, "") for c in rows.columns if c in t.columns}
            current = mine.get(k)
            if current is not None and all((current.get(c) or "") == (values.get(c) or "") for c in values):
                unchanged += 1
                continue
            out.append(Request("edit" if current is not None else "upsert", name, tuple(r.get(c, "") for c in t.key), values))
    return out + deletes, unchanged


def _resolve(defs, own, earlier, requests: list[Request]) -> list[Request]:
    """Upserts (a partial correction's records not in the version) become a corrected earlier record when one was filed, else an add."""
    _, before = earlier
    out = []
    for r in requests:
        if r.kind != "upsert":
            out.append(r)
            continue
        t = defs[r.table]
        k = tuple(norm(x) for x in r.key)
        filed = any(key_of(t, row) == k for row in before.get(r.table, Rows(r.table, [], [])).rows)
        out.append(Request("edit" if filed else "add", r.table, r.key if filed else (), r.values))
    return out


def _read(e: Editor, v: SubmissionVersion, data: bytes, filename: str) -> list[Request]:
    defs = {t.name: t for t in e.tables(v)}

    def table_of(name: str) -> str | None:
        if norm(name).replace(" ", "") in DELETE_NAMES:
            return DELETE_TABLE
        return e.table_of(name)

    rest, rest_name, delete_rows = _split_deletes(data, filename, table_of)
    tables = {}
    if rest is not None:
        try:
            tables = e.read_upload(rest, rest_name)
        except ValueError as err:  # the module's UploadError
            raise CorrectionRefused(str(err)) from None
    deletes = _delete_requests(defs, delete_rows, table_of)
    own = e.rows(v)
    requests, unchanged = _requests(defs, tables, deletes, own)
    if not requests and not unchanged:
        raise CorrectionRefused("The file holds no records to change, add or delete.")
    if len(requests) > max_rows():
        raise CorrectionRefused(
            f"The file changes {len(requests):,} records; a partial correction may change at most {max_rows():,} (see Limits, /limits/). "
            "Upload the whole corrected file instead."
        )
    return _resolve(defs, own, e.earlier(v), requests), unchanged


def preview(actor, v: SubmissionVersion, data: bytes, filename: str) -> Preview:
    """Read a partial correction against the version's records as they stand and say what applying it would change, without changing
    anything. The file is kept under the version (its SHA-256 is the preview's token) until it is applied or another is previewed."""
    v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    e = _require(actor, v)
    if problem := core_uploads.too_large(filename, len(data)):
        raise CorrectionRefused(problem)
    requests, unchanged = _read(e, v, data, filename)
    defs = {t.name: t for t in e.tables(v)}
    _, scope = apply(defs, e.rows(v), e.earlier(v), requests, e.earlier_delete)
    token = hashlib.sha256(data).hexdigest()
    store = storage.store("uploads")
    store.delete_prefix(f"submissions/versions/{v.pk}/corrections/")
    store.put_bytes(f"{_preview_key(v, token)}/{_safe_name(filename)}", data)
    counts: dict[str, dict[str, int]] = {}
    for c in scope.changes:
        counts.setdefault(c.table, {}).setdefault(c.kind, 0)
        counts[c.table][c.kind] += 1
    return Preview(token, filename, state(v), scope.changes, unchanged, counts)


def _safe_name(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name or "correction"
    return "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)[:120]


def pending(v: SubmissionVersion, token: str) -> tuple[bytes, str] | None:
    """The previewed file with this token, if it is still kept: (content, its name)."""
    store = storage.store("uploads")
    prefix = f"{_preview_key(v, token)}/"
    for f in store.list(prefix):
        return store.get_bytes(f.key), f.key.rsplit("/", 1)[1]
    return None


def apply_preview(actor, v: SubmissionVersion, token: str, expected: str, *, reason: str = "") -> tuple[SubmissionVersion, Scope]:
    """Apply a previewed partial correction, if the records still stand as they were when it was previewed."""
    v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    e = _require(actor, v)
    _require_state(v, expected)
    got = pending(v, token)
    if got is None:
        raise CorrectionRefused("That preview is no longer kept. Upload the correction again to preview it.")
    data, filename = got
    if hashlib.sha256(data).hexdigest() != token:
        raise CorrectionRefused("The kept file does not match its preview. Upload the correction again.")
    requests, _ = _read(e, v, data, filename)
    out = _commit(
        actor, v, requests, via={"how": "partial correction", "file": filename, "sha256": token}, reason=reason, expected=expected
    )
    transaction.on_commit(lambda: storage.store("uploads").delete_prefix(f"submissions/versions/{v.pk}/corrections/"))
    return out


def changes_csv(p: Preview, defs: dict[str, TableDef]) -> str:
    """A preview's every change: table, key, what, the fields changed with their values before and after."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Table", "Key", "Change", "Field", "Before", "After"])
    for c in p.changes:
        if c.kind in ("edit",) and c.before is not None:
            for f in c.fields:
                w.writerow([c.table, c.key, KINDS[c.kind], f, (c.before or {}).get(f, ""), (c.after or {}).get(f, "")])
        else:
            w.writerow([c.table, c.key, KINDS[c.kind], "", "", ""])
    return out.getvalue()


# --- what was corrected in place on a version


def history(v: SubmissionVersion):
    """The version's corrections, oldest first (its audit events)."""
    from cedars.services.audit.models import AuditEvent

    return AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk), action=ACTION).order_by("at", "id")


def summary(v: SubmissionVersion) -> dict:
    """What was corrected record by record on the version, for its change letter and refile notice: {"records", "edit", "add",
    "delete", "tables": {table: {kind: n}}, "partial": number of partial corrections applied}; {} when nothing was."""
    out: dict = {"records": 0, "edit": 0, "add": 0, "delete": 0, "tables": {}, "partial": 0}
    files = set()
    for ev in history(v):
        a = ev.after or {}
        kind = a.get("change", "")
        if kind not in ("edit", "add", "delete"):
            continue
        out[kind] += 1
        out["records"] += 1
        t = out["tables"].setdefault(a.get("table", ""), {"edit": 0, "add": 0, "delete": 0})
        t[kind] += 1
        via = a.get("via") or {}
        if via.get("how") == "partial correction":
            files.add(via.get("sha256"))
    out["partial"] = len(files)
    return out if out["records"] else {}


def summary_text(s: dict) -> str:
    """One sentence for a notice: "Corrected in place: 3 records changed, 1 added, 1 deleted." ("" for none)."""
    if not s:
        return ""
    parts = [f"{s['edit']:,} changed" if s.get("edit") else "", f"{s['add']:,} added" if s.get("add") else "",
             f"{s['delete']:,} deleted" if s.get("delete") else ""]  # fmt: skip
    n = s["records"]
    return f"Corrected record by record: {n:,} record{'s' if n != 1 else ''} ({', '.join(p for p in parts if p)})."
