"""Filing specifications: one per kind of filing, in versions the CPUC names, each holding the filing's tables and fields, the rule set
and built-in checks it is checked against, the engine release and cost-test set that price it, its file format and example records.

A version is held as rows (models.py) and travels as one JSON document (`as_document`, the format the starting versions are kept in and
the one the public download and API give). Filings name their version: each deadline of a filing set is pinned to one when it is put on
the calendar (submissions.add_deadline, `for_new_deadline`), and each validated filing version records the one it was checked against.

Filings read their version. The filing modules take a filing's tables, keys, fields and the columns an upload is refused without from
its version (`layout_for_filing`, a `VersionLayout`), its rule set (`rule_set_for_filing`), engine release and cost-test set. The
starting versions were generated from the modules' code before filings read their version; they are kept in `starting/<code>.json`,
which migration 0002 loaded, and a database with no version of a specification (one emptied and not loaded again) reads them from
there."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from django.db.models import Q
from django.utils.text import slugify

FORMAT = 1
STARTING = Path(__file__).parent / "starting"

TYPE_WORDS = {
    "text": "Text",
    "number": "Number",
    "integer": "Whole number",
    "flag": "0 or 1 (or True or False)",
    "date": "Date (YYYY-MM-DD or MM/DD/YYYY)",
    "year_quarter": "Year and quarter, such as 2027Q1",
    "year_month": "Year and month, as YYYYMM",
    "phone": "Phone number, digits only",
    "email": "Email address",
}
REQUIREMENT_WORDS = {"required": "Required", "optional": "Optional", "system": "Filled in by CEDARS (a value uploaded is replaced)"}


class SpecificationError(ValueError):
    """A document that is not a specification version, or a change the specification rules do not allow; the message says why."""


def type_words(f) -> str:
    """A field's type in words: "Text (up to 255 characters)", "Number (up to 4 decimal places)"."""
    get = f.get if isinstance(f, dict) else lambda k, d=None: getattr(f, k, d)
    words = TYPE_WORDS.get(get("type"), get("type"))
    if get("type") in ("text", "phone", "email") and get("max_length"):
        return f"{words} (up to {get('max_length')} characters)"
    if get("type") == "number" and get("decimals") is not None:
        return f"{words} (up to {get('decimals')} decimal places)"
    return words


_DATE = (re.compile(r"^\d{4}-\d{2}-\d{2}$"), re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"))


def value_problem(f, value) -> str:
    """Why `value` does not fit field `f`'s type and size ("" = it does): the field type's own check, as a filing's is."""
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    get = f.get if isinstance(f, dict) else lambda k, d=None: getattr(f, k, d)
    kind, text = get("type"), str(value).strip()
    if text == "":
        return ""
    if kind in ("text", "phone", "email") and get("max_length") and len(text) > get("max_length"):
        return f"it is longer than {get('max_length')} characters."
    if kind in ("number", "integer"):
        try:
            n = Decimal(text.replace(",", ""))
        except InvalidOperation:
            return "it is not a number."
        if not n.is_finite():
            return "it is not a number."
        if kind == "integer" and n != n.to_integral_value():
            return "it is not a whole number."
        decimals = get("decimals")
        if kind == "number" and decimals is not None and max(0, -n.normalize().as_tuple().exponent) > decimals:
            return f"it has more than {decimals} decimal places."
        return ""
    if kind == "flag":
        return "" if text.lower() in ("0", "1", "true", "false") else "it is not 0 or 1 (or True or False)."
    if kind == "date":
        for pattern, fmt in zip(_DATE, ("%Y-%m-%d", "%m/%d/%Y"), strict=True):
            if pattern.match(text):
                try:
                    datetime.strptime(text, fmt)
                    return ""
                except ValueError:
                    return "it is not a date."
        return "it is not a date (YYYY-MM-DD or MM/DD/YYYY)."
    if kind == "year_quarter":
        return "" if re.fullmatch(r"\d{4}Q[1-4]", text, re.I) else "it is not a year and quarter, such as 2027Q1."
    if kind == "year_month":
        return "" if re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", text) else "it is not a year and month, as YYYYMM."
    if kind == "phone":
        return "" if text.isdigit() else "a phone number is digits only."
    if kind == "email":
        return "" if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) else "it is not an email address."
    return ""


# --- version documents


def field_doc(name: str, **kw) -> dict:
    """A field of a version document, with every attribute (blank where not given)."""
    out = {
        "name": name,
        "other_names": [],
        "requirement": "optional",
        "type": "text",
        "max_length": None,
        "decimals": None,
        "value_list": "",
        "default": "",
        "example": "",
        "unit": "",
        "definition": "",
        "guidance": "",
    }
    unknown = set(kw) - set(out)
    if unknown:
        raise SpecificationError(f"{name}: unknown field attributes {sorted(unknown)}")
    out.update(kw)
    return out


def starting_path(code: str) -> Path:
    return STARTING / f"{code}.json"


def stored_starting(code: str) -> dict:
    return json.loads(starting_path(code).read_text(encoding="utf-8"))


def differences_between(old: dict, new: dict) -> list[str]:
    out = []
    for key in ("specification", "version"):
        for k in sorted(set(old[key]) | set(new[key])):
            if old[key].get(k) != new[key].get(k):
                out.append(f"{key} {k}: {old[key].get(k)!r} -> {new[key].get(k)!r}")
    ot, nt = {t["name"]: t for t in old["tables"]}, {t["name"]: t for t in new["tables"]}
    if list(ot) != list(nt):
        out.append(f"tables: {list(ot)} -> {list(nt)}")
    for name in ot.keys() & nt.keys():
        a, b = ot[name], nt[name]
        for k in sorted((set(a) | set(b)) - {"fields"}):
            if a.get(k) != b.get(k):
                out.append(f"{name} {k}: {a.get(k)!r} -> {b.get(k)!r}")
        af, bf = {f["name"]: f for f in a["fields"]}, {f["name"]: f for f in b["fields"]}
        if list(af) != list(bf):
            out.append(f"{name} fields: added {[f for f in bf if f not in af]}, removed {[f for f in af if f not in bf]}, or reordered")
        for fname in af.keys() & bf.keys():
            for k in sorted(set(af[fname]) | set(bf[fname])):
                if af[fname].get(k) != bf[fname].get(k):
                    out.append(f"{name}.{fname} {k}: {af[fname].get(k)!r} -> {bf[fname].get(k)!r}")
    oc, nc = [(c["group"], c["rule_id"]) for c in old["checks"]], [(c["group"], c["rule_id"]) for c in new["checks"]]
    if oc != nc:
        out.append(f"built-in checks: {oc} -> {nc}")
    else:
        for a, b in zip(old["checks"], new["checks"], strict=True):
            if a != b:
                out.append(f"built-in check {a['rule_id']} changed")
    return out


# --- loading a document as rows


def load(document: dict, *, models=None, actor=None, status: str = "published", rule_set=None, cost_test_set=None):
    """Make a version (and its specification, if new) from a document. `models` is a migration's app registry (the historical models)
    or None for the live ones. The version is made as a draft, given its rows, then given `status` (the database freezes a published
    version's rows, so they are added first). Returns the version."""
    from django.utils import timezone

    if document.get("format") != FORMAT:
        raise SpecificationError(f"This document's format is {document.get('format')!r}; this platform reads format {FORMAT}.")
    get = models.get_model if models is not None else _live_model
    Specification = get("specifications", "Specification")
    Version = get("specifications", "SpecificationVersion")
    Table = get("specifications", "SpecTable")
    Field = get("specifications", "SpecField")
    Check = get("specifications", "CheckSetting")
    Example = get("specifications", "ExampleSet")
    s = document["specification"]
    spec, _ = Specification.objects.get_or_create(
        code=s["code"], defaults={"module": s["module"], "name": s["name"], "about": s.get("about", ""), "position": s.get("position", 0)}
    )
    v = document["version"]
    name = (v.get("name") or "").strip()
    if not name:
        raise SpecificationError("A version needs a name.")
    if Version.objects.filter(specification=spec, name=name).exists():
        raise SpecificationError(f"{spec.name} already has a version called {name}.")
    version = Version.objects.create(
        specification=spec,
        name=name,
        slug=slugify(name),
        status="draft",
        summary=v.get("summary", ""),
        source=v.get("source", ""),
        rule_set_code=v.get("rule_set_code", ""),
        rule_set=rule_set,
        engine_release=v.get("engine_release", ""),
        cost_test_set=cost_test_set,
        file_format=v.get("file_format") or {},
        created_by=actor,
    )
    for i, t in enumerate(document["tables"]):
        table = Table.objects.create(
            version=version,
            position=i,
            name=t["name"],
            other_names=t.get("other_names", []),
            required=t.get("required", False),
            keys=t.get("keys", []),
            replace=t.get("replace", "key"),
            vault=t.get("vault", False),
            refuse_without=t.get("refuse_without", []),
            guidance=t.get("guidance", ""),
        )
        blank = field_doc("")
        Field.objects.bulk_create(
            Field(
                table=table,
                position=j,
                **{k: f.get(k, blank[k]) for k in FIELD_ATTRS},
                **({"renamed_from": f["renamed_from"]} if f.get("renamed_from") else {}),  # migration 0002's model has no renamed_from
            )
            for j, f in enumerate(t["fields"])
        )
    Check.objects.bulk_create(
        Check(version=version, position=i, **{k: c.get(k, {} if k == "settings" else "") for k in CHECK_ATTRS})
        for i, c in enumerate(document.get("checks", []))
    )
    Example.objects.bulk_create(
        Example(
            version=version, position=i, name=e["name"], slug=e.get("slug") or slugify(e["name"]), about=e.get("about", ""), rows=e["rows"]
        )
        for i, e in enumerate(document.get("examples", []))
    )
    if status != "draft":
        version.status = status
        if status in ("published", "withdrawn"):
            version.published_at = timezone.now()
            version.published_by = actor
            version.fingerprint = fingerprint(document)
        version.save()
    return version


FIELD_ATTRS = [
    "name",
    "other_names",
    "requirement",
    "type",
    "max_length",
    "decimals",
    "value_list",
    "default",
    "example",
    "unit",
    "definition",
    "guidance",
]
TABLE_ATTRS = ["name", "other_names", "required", "keys", "replace", "vault", "refuse_without", "guidance"]
CHECK_ATTRS = ["group", "rule_id", "severity", "tables", "fields", "finds", "fix", "settings"]


def _live_model(app, name):
    from django.apps import apps

    return apps.get_model(app, name)


def fingerprint(document: dict) -> str:
    """SHA-256 of what a version says (its tables, fields, checks, examples, format and what prices it), not of who made it or when:
    two environments' copies of one version have the same fingerprint."""
    content = {k: document[k] for k in ("tables", "checks", "examples")}
    content["version"] = {
        k: document["version"].get(k) for k in ("name", "rule_set_code", "engine_release", "cost_test_set", "file_format")
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# --- reading versions


def _models():
    from .models import Specification, SpecificationVersion

    return Specification, SpecificationVersion


def specifications():
    Specification, _ = _models()
    return Specification.objects.all()


def get(code: str):
    Specification, _ = _models()
    return Specification.objects.filter(code=code).first()


def _in_effect(qs, on=None):
    from django.utils import timezone

    on = on or timezone.localdate()
    return qs.filter(Q(effective_on__isnull=True) | Q(effective_on__lte=on))


def current(specification, on=None):
    """The specification's current version: the newest published one in effect (`effective_on` passed, or blank), which new filing
    sets are set up with; None before any is."""
    _, Version = _models()
    qs = Version.objects.filter(specification=specification, status=Version.Status.PUBLISHED)
    return _in_effect(qs, on).order_by("-published_at", "-pk").first()


def coming(specification, on=None) -> list:
    """Published versions that take effect after today (`on`), soonest first: announced, not yet used for new filing sets."""
    from django.utils import timezone

    _, Version = _models()
    on = on or timezone.localdate()
    return list(
        Version.objects.filter(specification=specification, status=Version.Status.PUBLISHED, effective_on__gt=on).order_by("effective_on")
    )


def for_module(module: str):
    """The current version of the specification the module's filings use, or None (a module without one)."""
    Specification, _ = _models()
    spec = Specification.objects.filter(module=module).first()
    return current(spec) if spec else None


def public_versions(specification):
    _, Version = _models()
    return Version.objects.filter(specification=specification).filter(
        Q(status=Version.Status.PUBLISHED) | Q(status=Version.Status.WITHDRAWN)
    )


def as_document(version) -> dict:
    """The version as one document (the format of `starting/*.json`), with the rule set it names."""
    tables = []
    for t in version.tables.prefetch_related("fields"):
        doc = {k: getattr(t, k) for k in TABLE_ATTRS}
        doc["fields"] = [
            {**{k: getattr(f, k) for k in FIELD_ATTRS}, **({"renamed_from": f.renamed_from} if f.renamed_from else {})}
            for f in t.fields.all()
        ]
        tables.append(doc)
    s = version.specification
    pack = version.cost_test_set
    return {
        "format": FORMAT,
        "specification": {"code": s.code, "module": s.module, "name": s.name, "about": s.about, "position": s.position},
        "version": {
            "name": version.name,
            "slug": version.slug,
            "summary": version.summary,
            "source": version.source,
            "rule_set_code": version.rule_set_code,
            "engine_release": version.engine_release,
            "cost_test_set": {"id": pack.label, "version": pack.version} if pack else None,
            "file_format": version.file_format,
        },
        "tables": tables,
        "checks": [{k: getattr(c, k) for k in CHECK_ATTRS} for c in version.checks.all()],
        "examples": [{"name": e.name, "slug": e.slug, "about": e.about, "rows": e.rows} for e in version.examples.all()],
    }


# --- what the modules read of a filing's version


@dataclass(frozen=True)
class FieldLayout:
    """A field as the filing modules read it (a `SpecField` row's attributes, or a document's field)."""

    name: str
    other_names: tuple[str, ...] = ()
    requirement: str = "optional"
    type: str = "text"
    max_length: int | None = None
    decimals: int | None = None
    value_list: str = ""
    default: str = ""
    example: str = ""
    unit: str = ""
    definition: str = ""
    guidance: str = ""

    @property
    def required(self) -> bool:
        return self.requirement == "required"

    @classmethod
    def of(cls, f) -> FieldLayout:
        get = f.get if isinstance(f, dict) else lambda k, d=None: getattr(f, k, d)
        blank = field_doc("")
        values = {k: get(k, blank[k]) for k in FIELD_ATTRS}
        values["other_names"] = tuple(values["other_names"] or ())
        return cls(**values)


@dataclass(frozen=True)
class TableLayout:
    name: str
    other_names: tuple[str, ...]
    keys: tuple[str, ...]
    replace: str
    required: bool
    vault: bool
    refuse_without: tuple[str, ...]
    fields: tuple[FieldLayout, ...]
    guidance: str = ""

    def columns(self) -> list[str]:
        return [f.name for f in self.fields]

    def upload_columns(self) -> list[str]:
        return [f.name for f in self.fields if f.requirement != "system"]

    def required_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.requirement == "required"]

    def system_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.requirement == "system"]

    def field(self, name: str) -> FieldLayout | None:
        return next((f for f in self.fields if f.name == name), None)

    def canonical(self, header: str) -> str | None:
        """The field an uploaded column names (its name or another accepted name, any case), or None."""
        return self._names().get(str(header).strip().lower())

    def _names(self) -> dict[str, str]:
        names = self.__dict__.get("_names_cache")
        if names is None:
            names = {}
            for f in self.fields:
                for n in (*f.other_names, f.name):  # a field's own name wins over another field's other name
                    names[n.lower()] = f.name
            object.__setattr__(self, "_names_cache", names)
        return names


@dataclass(frozen=True)
class VersionLayout:
    """A version as the filing modules read it: its tables in order with their keys, fields and requirements. `version` is the
    SpecificationVersion, or None for a layout read from a stored document (a database with no version of the specification)."""

    version: object
    tables: tuple[TableLayout, ...]
    code: str = ""

    def table(self, name: str) -> TableLayout | None:
        return next((t for t in self.tables if t.name == name), None)

    def names(self, *, vault: bool | None = None) -> list[str]:
        return [t.name for t in self.tables if vault is None or t.vault == vault]

    def table_name(self, name: str, *, vault: bool | None = None) -> str | None:
        """The table a file or sheet name holds (its name or another accepted name; case, spaces, dashes and underscores aside)."""
        n = _squash(name)
        for t in self.tables:
            if (vault is None or t.vault == vault) and (n == _squash(t.name) or n in (_squash(o) for o in t.other_names)):
                return t.name
        return None


def _squash(name: str) -> str:
    return re.sub(r"[\s_\-]", "", str(name)).lower()


_layouts_made: dict[tuple, VersionLayout] = {}


def layout(version) -> VersionLayout:
    """The layout of a version from its rows. A frozen version's is kept (it never changes), under its content's fingerprint."""
    key = (version.pk, version.fingerprint) if version.frozen and version.fingerprint else None
    if key is not None and key in _layouts_made:
        return _layouts_made[key]
    tables = tuple(
        TableLayout(
            t.name,
            tuple(t.other_names),
            tuple(t.keys),
            t.replace,
            t.required,
            t.vault,
            tuple(t.refuse_without),
            tuple(FieldLayout.of(f) for f in t.fields.all()),
            t.guidance,
        )
        for t in version.tables.prefetch_related("fields")
    )
    made = VersionLayout(version, tables, version.specification.code)
    if key is not None:
        _layouts_made[key] = made
    return made


def document_layout(document: dict) -> VersionLayout:
    """The layout of a version document (the format of `starting/*.json`)."""
    tables = tuple(
        TableLayout(
            t["name"],
            tuple(t.get("other_names", ())),
            tuple(t.get("keys", ())),
            t.get("replace", "key"),
            t.get("required", False),
            t.get("vault", False),
            tuple(t.get("refuse_without", ())),
            tuple(FieldLayout.of(f) for f in t["fields"]),
            t.get("guidance", ""),
        )
        for t in document["tables"]
    )
    return VersionLayout(None, tables, document["specification"]["code"])


@cache
def _starting_layout(module: str) -> VersionLayout | None:
    for path in sorted(STARTING.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["specification"]["module"] == module:
            return document_layout(document)
    return None


def layout_for_module(module: str) -> VersionLayout:
    """The layout of the module's current version; where the database has none (a database emptied and not loaded again), the
    starting version as the platform ships it. Raises SpecificationError for a module with no specification."""
    version = for_module(module)
    if version is not None:
        return layout(version)
    made = _starting_layout(module)
    if made is None:
        raise SpecificationError(f"The {module} module has no filing specification.")
    return made


def layout_for_filing(deadline, module: str = "") -> VersionLayout:
    """The layout a filing's uploads, checks and corrections follow, on any date: its version's (`of_filing`), else its module's."""
    version = of_filing(deadline)
    if version is not None:
        return layout(version)
    return layout_for_module(module or deadline.module)


# --- filings and their versions


def for_new_deadline(module: str, kind: str = ""):
    """The version a deadline put on the calendar now is pinned to: the current version of its module's specification (None for a module
    without one)."""
    return for_module(module)


def of_filing(deadline):
    """The version a filing (a deadline) is checked and priced against, on any date: the one it is pinned to. A deadline pinned to none
    (put on the calendar where its specification had no published version yet) takes its module's current version; None for a module
    without a specification."""
    if deadline is None:
        return None
    if deadline.specification_version_id is not None:
        return deadline.specification_version
    return for_module(deadline.module)


def rule_set_of(version, code: str = ""):
    """The rule set a filing on `version` is checked against: the one the version names. A version that takes rules (its rule_set_code)
    but names no rule set, because none of that code was published when it was made (a new environment), takes the one of that code in
    force today, as every filing did before filings read their version; so does a filing of a module with no version (`code`, the
    module's rule-set code). None: field and built-in checks only."""
    if version is not None:
        if version.rule_set_id is not None:
            return version.rule_set
        code = version.rule_set_code
    if not code:
        return None
    from cedars.services.rules import service as rules_service

    return rules_service.in_force(code)


def rule_set_for_filing(deadline, code: str = ""):
    """The rule set a filing is checked against: its version's (`rule_set_of`)."""
    return rule_set_of(of_filing(deadline), code)


def engine_release_of(version) -> str:
    """The engine release (its number, e.g. 0.1.94) that prices a filing on `version`; "" for the release marked default for new runs
    (a version that names none, or no version)."""
    return (version.engine_release or "").strip() if version is not None else ""


def naming_release(release: str) -> list:
    """The published versions that name engine release `release` (its number) and have filings on the calendar: the release prices
    those filings, so it cannot be retired while any does."""
    _, Version = _models()
    return [
        v
        for v in Version.objects.filter(status=Version.Status.PUBLISHED, engine_release=release).select_related("specification")
        if in_use(v)
    ]


def cost_test_set_of(version):
    """The published cost-test set (a reference data pack version) that prices a filing on `version`; None when it names none, and the
    filing set's own choice, else the set in force for the program year, applies."""
    return version.cost_test_set if version is not None and version.cost_test_set_id is not None else None


def template_url(module: str, filing=None, kind: str = "xlsx") -> str:
    """The upload template of the version a filing follows (`filing`: a deadline's id; the module's current version without one or for
    a deadline of another module), from the version's own downloads; "" when the module has no published version."""
    from django.urls import reverse

    from cedars.services.submissions.models import Deadline

    version = None
    if filing and str(filing).isdigit():
        d = Deadline.objects.filter(pk=int(filing), module=module).select_related("specification_version__specification").first()
        version = of_filing(d) if d is not None else None
    version = version or for_module(module)
    if version is None:
        return ""
    return reverse("specifications:template", args=[version.specification.code, version.slug, kind])


# --- reading a version's parts


def rules(version) -> list:
    """The rules of the version's rule set (none when it takes no rule set, or none is published yet)."""
    if version.rule_set_id is None:
        return []
    return list(version.rule_set.rules.order_by("table", "field", "rule_id"))


def _rule_fields(rule) -> list[str]:
    """The field names a rule is about: a two-column key names both."""
    from cedars.services.rules import service as rules_service

    out = []
    for part in (rule.field or "").split("+"):
        part = part.strip()
        if not part:
            continue
        out.append(part)
        try:
            out.append(rules_service.canonical_field(part))
        except rules_service.EngineMissing:
            pass
    return out


def rules_by_field(version) -> tuple[dict[tuple[str, str], list], dict[str, list]]:
    """({(table, field): [rule, ...]}, {table: [rule, ...]}): each rule under the field it is about (its name or another accepted name),
    else under its table (a key over two columns, a rule about a field the table does not have)."""
    names = {}
    for t in version.tables.prefetch_related("fields"):
        for f in t.fields.all():
            for n in (f.name, *f.other_names):
                names[(t.name, n.lower())] = f.name
    by_field: dict[tuple[str, str], list] = {}
    by_table: dict[str, list] = {}
    for r in rules(version):
        fields = _rule_fields(r)
        hit = next((names[(r.table, n.lower())] for n in fields if (r.table, n.lower()) in names), None)
        if hit and "+" not in (r.field or ""):
            by_field.setdefault((r.table, hit), []).append(r)
        else:
            by_table.setdefault(r.table, []).append(r)
    return by_field, by_table


def settings_rows(version) -> list[list[str]]:
    """The version's settings in words: what prices it, its rules and its file format."""
    rs = version.rule_set
    rows = [
        ["Specification", version.specification.name],
        ["Version", version.name],
        ["Status", version.get_status_display()],
        ["Published", version.published_at.date().isoformat() if version.published_at else ""],
        ["Rules", f"The {rs.label} rules effective {rs.effective_from:%Y-%m-%d}" if rs else "Field checks only (no rule set)"],
        ["Engine release", f"ReCET {version.engine_release}" if version.engine_release else "The release marked default for new runs"],
        [
            "Cost-test set",
            f"{version.cost_test_set.label} {version.cost_test_set.version}"
            if version.cost_test_set
            else "The set the filing set names, else the one in force for the program year",
        ],
        ["Fingerprint (SHA-256)", version.fingerprint],
    ]
    fmt = version.file_format or {}
    labels = {
        "files": "Files",
        "delimiters": "Delimiters",
        "encoding": "Encoding",
        "names": "Names",
        "column_order": "Column order",
        "unknown_columns": "Columns the table does not have",
        "unknown_files": "Other files",
    }
    for k, label in labels.items():
        if k in fmt:
            v = fmt[k]
            if k == "delimiters":
                v = ", ".join({"|": "pipe (|)", ",": "comma", "\t": "tab"}.get(d, d) for d in v)
            elif isinstance(v, list):
                v = "; or ".join(v)
            rows.append([label, v])
    return rows


# --- comparing two versions


@dataclass
class Comparison:
    tables_added: list
    tables_removed: list
    fields_added: list  # (table, SpecField)
    fields_removed: list
    fields_changed: list  # (table, field name, [(attribute words, old, new), ...])
    fields_renamed: list  # (table, old name, new name, [(attribute words, old, new), ...])
    rules_added: list
    rules_removed: list
    rules_changed: list  # (old rule, new rule): a changed test is a new ID that names the one it replaces
    rules_renamed: list  # (old rule, new rule) that differ only by a renamed field's name
    checks_added: list
    checks_removed: list
    checks_changed: list
    settings_changed: list  # (setting, old, new)

    @property
    def empty(self) -> bool:
        return not any(getattr(self, k) for k in self.__dataclass_fields__)


FIELD_WORDS = {
    "requirement": "Requirement",
    "type": "Type",
    "max_length": "Most characters",
    "decimals": "Most decimal places",
    "value_list": "Value list",
    "default": "Default",
    "example": "Example",
    "unit": "Unit",
    "other_names": "Also accepted as",
    "definition": "Definition",
    "guidance": "Guidance",
}


def _show(attr, value):
    if attr == "requirement":
        return REQUIREMENT_WORDS.get(value, value)
    if attr == "type":
        return TYPE_WORDS.get(value, value)
    if isinstance(value, list):
        return ", ".join(value)
    return "" if value is None else str(value)


def _without(names: list, name: str) -> list:
    """A renamed field's other names without the old name the rename added to them."""
    return [n for n in names if n.lower() != name.lower()]


def _only_renamed(old, new, renames: dict[tuple[str, str], str]) -> bool:
    """Whether rule `new` is rule `old` with only renamed fields' names changed (its test, severity and wording)."""
    from cedars.services.rules import service as rules_service

    field, definition, message, fix = old.field, old.definition, old.message, old.fix
    for (table, a), b in renames.items():
        field, definition = rules_service.renamed_test(old.table, field, definition, table, a, b)
        message, fix = rules_service.renamed_words(message, a, b), rules_service.renamed_words(fix, a, b)
    return (old.table, old.severity, old.kind, field, definition, message, fix) == (
        new.table, new.severity, new.kind, new.field, new.definition, new.message, new.fix)  # fmt: skip


def compare(a, b) -> Comparison:
    """What changes from version a to version b (of one specification, or across two: a table is matched by its name)."""
    at = {t.name: t for t in a.tables.prefetch_related("fields")}
    bt = {t.name: t for t in b.tables.prefetch_related("fields")}
    fields_added, fields_removed, fields_changed, fields_renamed = [], [], [], []
    renames: dict[tuple[str, str], str] = {}  # (table, old name): new name
    for name in at.keys() & bt.keys():
        af = {f.name: f for f in at[name].fields.all()}
        bf = {f.name: f for f in bt[name].fields.all()}
        moved = {n: f.renamed_from for n, f in bf.items() if f.renamed_from in af and f.renamed_from not in bf}  # new: old
        fields_added += [(name, bf[n]) for n in bf if n not in af and n not in moved]
        fields_removed += [(name, af[n]) for n in af if n not in bf and n not in moved.values()]
        for n in [n for n in bf if n in af or n in moved]:
            old = af[moved.get(n, n)]
            diffs = [
                (words, _show(k, getattr(old, k)), _show(k, getattr(bf[n], k)))
                for k, words in FIELD_WORDS.items()
                if getattr(old, k) != (_without(getattr(bf[n], k), old.name) if k == "other_names" and n in moved else getattr(bf[n], k))
            ]
            if n in moved:
                fields_renamed.append((name, old.name, n, diffs))
                renames[(name, old.name)] = n
            elif diffs:
                fields_changed.append((name, n, diffs))
    ar, br = {r.rule_id: r for r in rules(a)}, {r.rule_id: r for r in rules(b)}
    replaced = {r.replaces: r for r in br.values() if r.replaces and r.replaces in ar and r.rule_id not in ar}
    rules_changed = [(ar[old], new) for old, new in replaced.items()]
    rules_renamed = [(o, n) for o, n in rules_changed if renames and _only_renamed(o, n, renames)]
    rules_changed = [p for p in rules_changed if p not in rules_renamed]
    rules_changed += [
        (ar[i], br[i])
        for i in ar.keys() & br.keys()
        if (ar[i].message, ar[i].fix, ar[i].severity) != (br[i].message, br[i].fix, br[i].severity)
    ]
    ac, bc = {c.rule_id: c for c in a.checks.all()}, {c.rule_id: c for c in b.checks.all()}
    keys = ("severity", "tables", "fields", "finds", "fix", "settings")
    sa, sb = dict((r[0], r[1]) for r in settings_rows(a)), dict((r[0], r[1]) for r in settings_rows(b))
    skip = {"Specification", "Version", "Status", "Published", "Fingerprint (SHA-256)"}
    return Comparison(
        tables_added=[bt[n] for n in bt if n not in at],
        tables_removed=[at[n] for n in at if n not in bt],
        fields_added=fields_added,
        fields_removed=fields_removed,
        fields_changed=fields_changed,
        fields_renamed=fields_renamed,
        rules_added=[r for i, r in br.items() if i not in ar and r not in replaced.values()],
        rules_removed=[r for i, r in ar.items() if i not in br and i not in replaced],
        rules_changed=sorted(rules_changed, key=lambda p: (p[1].table, p[1].field, p[1].rule_id)),
        rules_renamed=sorted(rules_renamed, key=lambda p: (p[1].table, p[1].field, p[1].rule_id)),
        checks_added=[c for i, c in bc.items() if i not in ac],
        checks_removed=[c for i, c in ac.items() if i not in bc],
        checks_changed=[(ac[i], bc[i]) for i in ac.keys() & bc.keys() if any(getattr(ac[i], k) != getattr(bc[i], k) for k in keys)],
        settings_changed=[(k, sa.get(k, ""), sb.get(k, "")) for k in dict.fromkeys([*sa, *sb]) if k not in skip and sa.get(k) != sb.get(k)],
    )


# --- who may read a version


def can_read(user, version) -> bool:
    """Published and withdrawn versions: anyone. Proposed: anyone signed in. Drafts: CPUC system administrators."""
    if version.public:
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    if version.status == version.Status.PROPOSED:
        return True
    from cedars.services.accounts import service as access

    return access.is_system_admin(user)


def readable_versions(user, specification):
    _, Version = _models()
    qs = Version.objects.filter(specification=specification)
    return [v for v in qs if can_read(user, v)]


def in_use(version) -> list:
    """The deadlines (filings) pinned to the version that are on the calendar, by due date."""
    return list(version.deadlines.filter(retired_at__isnull=True).select_related("period", "cycle").order_by("due_at"))


def apply_checks(version, findings: list) -> list:
    """A filing's findings with its version's built-in checks (rules.coded): a finding of a check that a version may leave out (optional)
    is dropped when the version does not list it, and a check whose severity the version may choose takes the version's. Every other
    finding is kept as it is: CEDARS' own processing always runs, and so does a check added to CEDARS after the version was published.
    A filing with no version, or a version that lists no checks, keeps every finding. Findings are dataclasses (rules.Finding, a
    module's Problem) or dicts with `rule_id` and `severity`."""
    if version is None or not findings:
        return findings
    listed = {c.rule_id: c.severity for c in version.checks.all()}
    if not listed:
        return findings
    from cedars.services.rules import coded

    known = coded.by_id()
    out = []
    for f in findings:
        get = f.get if isinstance(f, dict) else lambda k, f=f: getattr(f, k, "")
        check = known.get(str(get("rule_id")))
        if check is not None and check.optional and check.rule_id not in listed:
            continue
        severity = listed.get(check.rule_id, "") if check is not None else ""
        if severity and severity in check.severities and get("severity") != severity:
            f = {**f, "severity": severity} if isinstance(f, dict) else replace(f, severity=severity)
        out.append(f)
    return out


def apply_checks_for_filing(deadline, findings: list) -> list:
    """`apply_checks` with the version a filing (a deadline) is checked against."""
    return apply_checks(of_filing(deadline), findings)


def check_group_labels() -> dict[str, str]:
    """{group: label} of the built-in check lists (rules.coded), e.g. claims: Claims."""
    from cedars.services.rules import coded

    return {g.module: g.label for g in coded.groups()}


# --- values checked against a value list made for a specification


def list_fields(lay: VersionLayout, table: str) -> list[tuple[FieldLayout, str]]:
    """The fields of `table` whose codes come from a value list made for a specification (in a draft, `editor.new_value_list`), with
    the list's name. The engine has no such list, so the filing modules check these fields themselves (`list_problems`)."""
    t = lay.table(table)
    if t is None:
        return []
    named = {f.value_list.lower() for f in t.fields if f.value_list}
    if not named:
        return []
    from cedars.services.dictionary import value_lists

    made = {n.lower(): n for n in value_lists.made_here_names()}
    return [(f, made[f.value_list.lower()]) for f in t.fields if f.value_list and f.value_list.lower() in made]


def list_problems(lay: VersionLayout, table: str, rows, *, year=None, year_of=None, get=None) -> list[dict]:
    """The values of `rows` (records of `table`, as dicts) that are not a code of their field's value list made for a specification,
    accepted in the record's year (`year`, or `year_of(row)`; any year when neither gives one). One dict per value: `index` (the row's
    place in `rows`, from 0), `field`, `value`, `list`, `message` and `fix`, which the module puts in its own finding under its own
    rule ID. A blank value passes (whether a field may be blank is another check). `get(row, name)` reads a value (default: the row's
    own key, then the field's other names)."""
    from cedars.services.dictionary import value_lists

    fields = list_fields(lay, table)
    if not fields:
        return []

    def read(row, f):
        if get is not None:
            return get(row, f.name) or next((get(row, n) for n in f.other_names if get(row, n)), "")
        return row.get(f.name) or next((row.get(n) for n in f.other_names if row.get(n)), "") or ""

    out = []
    for f, name in fields:
        codes = value_lists.codes_for_check(name)
        if codes is None:  # published with the version it was made for, so a filing on that version always has it
            continue
        for i, row in enumerate(rows):
            value = str(read(row, f) or "").strip()
            y = year_of(row) if year_of is not None else year
            if value and not value_lists.accepts(codes, value, y):
                out.append(
                    {
                        "index": i,
                        "field": f.name,
                        "value": value,
                        "list": name,
                        "message": f"{f.name} {value} is not a code of the {name} value list" + (f" accepted for {y}" if y else ""),
                        "fix": f"Use a code from the {name} value list that is accepted for the year (the list's page shows each code's "
                        "first and last year).",
                    }
                )
    return out
