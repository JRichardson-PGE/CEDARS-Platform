"""Moving a specification version between CEDARS environments: built and tested in the sandbox, exported as one file and imported
into production as a draft, with no developer, code change or release.

The package (`export`) is a .zip holding the whole version as data: the version document (its settings, tables, fields with their
definitions, built-in check settings, file format, guidance, example files), its rules, the value lists made for it with their codes,
the cost-test set's own file, and what it changes from the version it was copied from. A manifest lists each part with its SHA-256,
the environment it came from, who exported it and when, and the version's fingerprint. It holds no filings and nothing about a person
beyond the name of whoever exported it.

An import always makes a draft (`import_package`), never a published version, so nothing a filing is checked against changes until an
administrator here publishes it, through the same checks as a draft typed in by hand (examples checked, the impact test on this
environment's real filings). Before it is made, `preview` shows what the package would create, how it differs from the version
published here, and what this environment lacks: an engine release or cost-test set not registered here stops the import; a value list
it names that is missing is made in the draft from the package's codes, and one with other codes here is reported and kept as it is;
a field definition that differs from the data dictionary is reported (the dictionary is never changed by an import). The draft keeps
where it came from (`SpecificationVersion.imported`), so its page shows whether it is still exactly what was exported (`provenance`).
Each export and import is audited with the file's name and SHA-256."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cedars.services import audit

from . import editor, service
from .editor import EditError
from .models import Specification, SpecificationVersion

FORMAT = "cedars-specification-package"
PACKAGE_VERSION = 1
MANIFEST = "manifest.json"
DOCUMENT, RULES, LISTS, COST_TESTS, CHANGES = "version.json", "rules.json", "value_lists.json", "cost_test_set.yaml", "changes.json"
MAX_BYTES = 20 * 1024 * 1024
RULE_FIELDS = [
    "rule_id",
    "severity",
    "table",
    "field",
    "kind",
    "definition",
    "message",
    "fix",
    "modules",
    "difference",
    "source",
    "replaces",
]


class PackageError(EditError):
    """A file that is not a package this environment can import, or an import it refuses; the message says why."""


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def environment() -> str:
    return getattr(settings, "ENVIRONMENT", "") or "unnamed"


def file_name(v: SpecificationVersion) -> str:
    return f"{v.specification.code}_{v.slug}_package.zip"


def can_export(user) -> bool:
    return editor.can_manage(user)


# --- export


def _lists(v: SpecificationVersion) -> list[dict]:
    """The value lists made for a specification (not the engine's or the platform's own) that the version's fields name, with codes."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    named = sorted({f.value_list for t in v.tables.prefetch_related("fields") for f in t.fields.all() if f.value_list}, key=str.lower)
    out = []
    for name in named:
        vl = ValueList.objects.filter(name__iexact=name).exclude(made_for="").first()
        if vl is None:
            continue
        shown = value_lists.draft_of(vl) if v.status == SpecificationVersion.Status.DRAFT else None
        shown = shown or value_lists.latest_published(vl) or value_lists.draft_of(vl)
        codes = [] if shown is None else [
            {"code": c.code, "description": c.description, "start_year": c.start_year, "end_year": c.end_year}
            for c in shown.codes.order_by("code")
        ]  # fmt: skip
        out.append({"name": vl.name, "label": vl.label, "codes": codes})
    return out


def _rules(v: SpecificationVersion) -> dict | None:
    if v.rule_set_id is None:
        return None
    rs = v.rule_set
    return {
        "code": rs.code,
        "label": rs.label,
        "rules": [{k: getattr(r, k) for k in RULE_FIELDS} for r in rs.rules.order_by("table", "field", "rule_id")],
    }


def _cost_test_file(v: SpecificationVersion) -> bytes | None:
    if v.cost_test_set_id is None:
        return None
    from cedars.services.refdata import cost_tests

    try:
        return cost_tests.file_bytes(v.cost_test_set)
    except Exception:  # noqa: BLE001 - a set whose file cannot be read is named in the package without its file
        return None


def _json(value) -> bytes:
    return json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def package(v: SpecificationVersion, actor=None) -> bytes:
    """The version as one .zip package (see the module's notes). Nothing changes."""
    document = service.as_document(v)
    parts = {DOCUMENT: _json(document), LISTS: _json(_lists(v))}
    rules = _rules(v)
    if rules is not None:
        parts[RULES] = _json(rules)
    cost = _cost_test_file(v)
    if cost is not None:
        parts[COST_TESTS] = cost
    if v.based_on_id is not None:
        parts[CHANGES] = _json({"based_on": v.based_on.name, "changes": editor.change_lines(v) if v.status == v.Status.DRAFT else []})
    manifest = {
        "format": FORMAT,
        "package_version": PACKAGE_VERSION,
        "specification": v.specification.code,
        "version": v.name,
        "status": v.status,
        "fingerprint": service.fingerprint(document),
        "environment": environment(),
        "exported_by": (getattr(actor, "name", "") or "a CPUC system administrator") if actor is not None else "CEDARS",
        "exported_at": timezone.now().isoformat(timespec="seconds"),
        "parts": {name: sha256(data) for name, data in sorted(parts.items())},
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, _json(manifest))
        for name, data in sorted(parts.items()):
            z.writestr(name, data)
    return out.getvalue()


def export(actor, v: SpecificationVersion) -> bytes:
    """Export a version (a draft, proposed, published or withdrawn one) as its package, audited with the file's name and SHA-256."""
    editor._require(actor)
    if v.historic:
        raise editor.EditError(f"{v.name} is a legacy CEDARS layout, kept read-only here: it is not exported.")
    content = package(v, actor)
    audit.record("specification.exported", obj=v, after={"file": file_name(v), "sha256": sha256(content), "environment": environment()},
                 actor=actor)  # fmt: skip
    return content


# --- reading a package


@dataclass
class Package:
    manifest: dict
    document: dict
    rules: dict | None
    lists: list[dict]
    cost_test_file: bytes | None
    changes: dict | None
    sha256: str


def read(content: bytes) -> Package:
    """A package's parts, each checked against its SHA-256 in the manifest."""
    if len(content) > MAX_BYTES:
        raise PackageError("That file is larger than a specification package.")
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        names = set(z.namelist())
    except zipfile.BadZipFile:
        raise PackageError("That file is not a specification package (.zip).") from None
    if MANIFEST not in names:
        raise PackageError("That .zip has no manifest: it is not a specification package exported from CEDARS.")
    try:
        manifest = json.loads(z.read(MANIFEST))
    except ValueError:
        raise PackageError("The package's manifest is not readable.") from None
    if manifest.get("format") != FORMAT:
        raise PackageError("That .zip is not a specification package exported from CEDARS.")
    if manifest.get("package_version") != PACKAGE_VERSION:
        raise PackageError(f"The package is in format {manifest.get('package_version')}; this environment reads format {PACKAGE_VERSION}.")
    data = {}
    for name, digest in (manifest.get("parts") or {}).items():
        if name not in names:
            raise PackageError(f"The package lacks {name}, which its manifest lists.")
        b = z.read(name)
        if sha256(b) != digest:
            raise PackageError(f"{name} does not match the SHA-256 its manifest gives: the package was changed after it was exported.")
        data[name] = b
    if DOCUMENT not in data:
        raise PackageError("The package has no version document.")
    try:
        document = json.loads(data[DOCUMENT])
        rules = json.loads(data[RULES]) if RULES in data else None
        lists = json.loads(data.get(LISTS, b"[]"))
        changes = json.loads(data[CHANGES]) if CHANGES in data else None
    except ValueError:
        raise PackageError("A part of the package is not readable JSON.") from None
    if service.fingerprint(document) != manifest.get("fingerprint"):
        raise PackageError("The version in the package does not match the fingerprint its manifest gives.")
    return Package(manifest, document, rules, lists, data.get(COST_TESTS), changes, sha256(content))


# --- importing


@dataclass
class Preview:
    """What an import would make: the specification, the version's name, the version published here it is compared with, what stops
    the import, what is only reported, and what it changes from the version published here."""

    package: Package
    specification: Specification | None
    name: str
    current: SpecificationVersion | None
    blocking: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    lists_made: list[str] = field(default_factory=list)
    to_settle: list[str] = field(default_factory=list)


def _cost_test_set(ref):
    if not ref:
        return None
    return editor.cost_test_sets().filter(label=ref.get("id", ""), version=ref.get("version", "")).first()


def _blocking(pkg: Package, spec) -> list[str]:
    out = []
    doc = pkg.document
    name = (doc["version"].get("name") or "").strip()
    if spec is None:
        return [f"This environment has no {doc['specification'].get('name') or doc['specification'].get('code')} specification."]
    draft = editor.draft_of(spec)
    if draft is not None:
        out.append(f"{spec.name} already has a draft, {draft.name}: publish or discard it first, since a specification has one draft.")
    if spec.versions.filter(name__iexact=name).exists():
        out.append(f"{spec.name} already has a version called {name}.")
    problem = editor.release_problem(doc["version"].get("engine_release", ""))
    if problem:
        out.append(problem)
    ref = doc["version"].get("cost_test_set")
    if ref and _cost_test_set(ref) is None:
        held = " The package holds the set's file, to register it." if pkg.cost_test_file else ""
        out.append(f"The cost-test set {ref.get('id')} {ref.get('version')} is not published here: register and publish it on "
                   f"Cost-test sets first.{held}")  # fmt: skip
    return out


def _list_notes(pkg: Package) -> tuple[list[str], list[str]]:
    """(the lists to make in the draft, notes on lists that differ here)."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    make, notes = [], []
    for item in pkg.lists:
        vl = ValueList.objects.filter(name__iexact=item["name"]).first()
        if vl is None:
            make.append(item["name"])
            continue
        shown = value_lists.latest_published(vl) or value_lists.draft_of(vl)
        here = {(c.code.lower(), c.start_year, c.end_year) for c in (shown.codes.all() if shown else [])}
        there = {(c["code"].lower(), c.get("start_year"), c.get("end_year")) for c in item["codes"]}
        if here != there:
            notes.append(f"The value list {vl.name} has other codes here than in the package: the list here is kept as it is "
                         f"({len(here)} codes here, {len(there)} in the package). Settle it on Value lists.")  # fmt: skip
    return make, notes


def _definition_notes(pkg: Package) -> list[str]:
    from cedars.services.dictionary import service as dictionary

    out = []
    for t in pkg.document["tables"]:
        for f in t["fields"]:
            known = dictionary.resolve(f["name"])
            if known is not None and f.get("definition") and known.definition and f["definition"].strip() != known.definition.strip():
                out.append(f"{t['name']}.{f['name']}: its definition differs from the data dictionary's; the dictionary is not changed.")
    return out


class _DryRun(Exception):
    pass


def preview(actor, content: bytes) -> Preview:
    """What importing the package would make, and what stops it or needs settling. Nothing changes: the draft is made in a transaction
    that is rolled back, to compare it with the version published here and run the checks before publishing on it."""
    editor._require(actor)
    pkg = read(content)
    spec = Specification.objects.filter(code=pkg.document["specification"]["code"]).first()
    p = Preview(pkg, spec, (pkg.document["version"].get("name") or "").strip(), service.current(spec) if spec else None)
    p.blocking = _blocking(pkg, spec)
    p.lists_made, list_notes = _list_notes(pkg)
    p.notes = list_notes + _definition_notes(pkg)
    if p.blocking:
        return p
    try:
        with transaction.atomic():
            v = _make(actor, pkg, "Checking the package before it is imported", content_name="")
            p.differences = editor.change_lines(v)
            p.to_settle = [x.text for x in editor.problems(v)]
            raise _DryRun
    except _DryRun:
        pass
    return p


def _make(actor, pkg: Package, reason: str, content_name: str) -> SpecificationVersion:
    """The draft a package makes (inside the caller's transaction)."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList
    from cedars.services.rules import service as rules_service
    from cedars.services.rules.models import Rule, RuleSet

    doc = pkg.document
    spec = Specification.objects.get(code=doc["specification"]["code"])
    current = service.current(spec)
    rule_set = None
    if pkg.rules is not None:
        rules_service._require_admin(actor)
        rule_set = RuleSet.objects.create(
            code=pkg.rules["code"],
            label=pkg.rules["label"],
            effective_from=editor._rule_set_date(pkg.rules["code"]),
            created_by=actor,
            reason=f"The rules of {spec.name} version {doc['version']['name']}, imported from {pkg.manifest['environment']}: {reason}",
            source=f"Imported from the {pkg.manifest['environment']} environment",
        )
        blank = {"definition": {}, "modules": []}
        Rule.objects.bulk_create(
            Rule(rule_set=rule_set, **{k: r.get(k, blank.get(k, "")) for k in RULE_FIELDS}) for r in pkg.rules["rules"]
        )
        audit.record("rule_set.imported", obj=rule_set, after={"code": rule_set.code, "rules": len(pkg.rules["rules"])}, reason=reason,
                     actor=actor)  # fmt: skip
    pack = _cost_test_set(doc["version"].get("cost_test_set"))
    v = service.load(doc, actor=actor, status="draft", rule_set=rule_set, cost_test_set=pack)
    imported = {k: pkg.manifest.get(k) for k in ("environment", "exported_by", "exported_at", "fingerprint", "status")}
    v.based_on = current
    v.source = f"Imported from the {pkg.manifest['environment']} environment"
    v.reason = reason
    v.imported = {**imported, "sha256": pkg.sha256, "file": content_name, "document": doc}
    v.save()
    for item in pkg.lists:
        if ValueList.objects.filter(name__iexact=item["name"]).exists():
            continue
        draft = value_lists.create_list(actor, item["name"], label=item.get("label") or item["name"], made_for=editor.made_for(v),
                                        made_in=editor.made_in(v), reason=reason)  # fmt: skip
        for c in item["codes"]:
            value_lists.add_code(actor, draft, c["code"], description=c.get("description") or "", start_year=c.get("start_year"),
                                 end_year=c.get("end_year"), reason=reason)  # fmt: skip
    return v


@transaction.atomic
def import_package(actor, content: bytes, reason: str, file_name: str = "") -> SpecificationVersion:
    """Import a package as a draft of its specification, after the same checks `preview` shows; audited with the file's SHA-256."""
    editor._require(actor)
    if not (reason or "").strip():
        raise PackageError("Say why the version is imported: the reason is kept with the draft.")
    pkg = read(content)
    spec = Specification.objects.filter(code=pkg.document["specification"]["code"]).first()
    blocking = _blocking(pkg, spec)
    if blocking:
        raise PackageError(blocking[0])
    v = _make(actor, pkg, reason.strip(), content_name=file_name)
    audit.record(
        "specification.imported",
        obj=v,
        after={
            "file": file_name,
            "sha256": pkg.sha256,
            "from": pkg.manifest.get("environment"),
            "fingerprint": pkg.manifest.get("fingerprint"),
        },
        reason=reason,
        actor=actor,
    )
    return v


# --- proof it is what was exported


@dataclass(frozen=True)
class Provenance:
    environment: str
    exported_by: str
    exported_at: object
    sha256: str
    identical: bool
    changes: list[str]


def provenance(v: SpecificationVersion) -> Provenance | None:
    """For an imported version: where it came from, and whether it is still exactly the version that was exported (else what changed
    after it was imported)."""
    info = v.imported or {}
    if not info:
        return None
    document = service.as_document(v)
    identical = service.fingerprint(document) == info.get("fingerprint")
    changes = [] if identical else [line.replace(" -> ", " to ") for line in service.differences_between(info["document"], document)]
    from datetime import datetime

    try:
        when = datetime.fromisoformat(info.get("exported_at") or "")
    except ValueError:
        when = None
    return Provenance(info.get("environment", ""), info.get("exported_by", ""), when, info.get("sha256", ""),
                      identical, changes)  # fmt: skip
