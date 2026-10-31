"""Cost-test definition sets as data packs (kind `cost_tests`; ReCET's COST_TESTS.md, BRD BR-06.8 / BR-06.14, D14).

A set is one YAML file (ReCET's `data/cost_tests/<id>/definition.yaml`): which value streams each cost test adds up, how it shares the
program costs, and the program years it is in force for. Its pack holds that one file, so the pack's fingerprint names the set exactly.
CPUC stewards register a set (the Cost-test sets page or `manage.py register_cost_tests`), record the engine check (the engine release
in use loads the set and `recet cost-tests check` passes; a set ReCET ships must be byte for byte the engine's own), and publish it.

How a run uses it (calculations.service): a filing takes a published set only, by its id (`settings.cost_test_set`), which the engine
prices as its built-in set and records with the file's SHA-256 in the receipt; the platform refuses a receipt whose set differs from the
pinned pack. An analysis run may also use a draft, sent as the file itself (`inputs.cost_tests`). With no set named, a run uses the one
in force for its job's first program year (`for_year`)."""

from __future__ import annotations

import hashlib
import re
from datetime import date

import yaml

from cedars.core import storage

from .models import PackVersion

KIND = PackVersion.Kind.COST_TESTS
DEFINITION = "definition.yaml"
ID = re.compile(r"^[A-Z][A-Z0-9]{0,15}-[0-9]{4}(-[a-z0-9]+)*$")  # ReCET's cost_test_sets.ID: <workstream>-<first program year>


class SetError(ValueError):
    """A file that is not a cost-test set (the engine checks everything else when the set is checked)."""


def read(data: bytes) -> dict:
    """The set's header from its YAML: id, workstream, version, label, draft, program years, tests (id: label), headline tests. Only
    what the platform shows and selects on; the engine validates the rest."""
    try:
        d = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise SetError(f"Not a readable YAML file: {e}") from None
    if not isinstance(d, dict):
        raise SetError("A cost-test set is a YAML mapping (keys and values).")
    problems = []
    sid, ws = d.get("id"), d.get("workstream")
    if not (isinstance(sid, str) and ID.match(sid)):
        problems.append("id: <workstream>-<first program year>, e.g. EE-2026")
    if not (isinstance(ws, str) and isinstance(sid, str) and sid.startswith(f"{ws}-")):
        problems.append("workstream: the id's first part, e.g. EE")
    for k in ("version", "label"):
        if not (isinstance(d.get(k), str) and d[k].strip()):
            problems.append(f"{k}: text is required")
    py = d.get("program_years") or {}
    if not isinstance(py, dict) or any(v is not None and not isinstance(v, int) for v in (py.get("from"), py.get("to"))):
        problems.append("program_years: {from: <year or null>, to: <year or null>}")
        py = {}
    tests = d.get("tests")
    if not isinstance(tests, dict) or not tests or not all(isinstance(t, dict) for t in tests.values()):
        problems.append("tests: at least one test")
        tests = {}
    if problems:
        raise SetError("; ".join(problems))
    return {
        "id": sid,
        "workstream": ws,
        "version": d["version"].strip(),
        "label": d["label"].strip(),
        "draft": bool(d.get("draft", False)),
        "program_years": {"from": py.get("from"), "to": py.get("to")},
        "tests": {tid: str(t.get("label") or tid) for tid, t in tests.items()},
        "headline": [tid for tid, t in tests.items() if t.get("headline")],
    }


def file_key(set_id: str, version: str) -> str:
    """Where a set's file is kept in the reference-data store."""
    return f"cost_tests/{set_id}/{storage.clean_key(version)}/{DEFINITION}"


def header(pack: PackVersion) -> dict:
    """The set's header as registered (kept in its manifest entry)."""
    return (pack.manifest.get(DEFINITION) or {}).get("set") or {}


def sha256(pack: PackVersion) -> str:
    return (pack.manifest.get(DEFINITION) or {}).get("sha256", "")


def years_text(pack: PackVersion) -> str:
    py = header(pack).get("program_years") or {}
    a, b = py.get("from"), py.get("to")
    if a is None and b is None:
        return "every program year (until a later set starts)"
    if b is None:
        return f"{a} on (until a later set starts)"
    return f"{a or 'up'} to {b}"


def register_set(actor, data: bytes, *, source: str = "", notes: str = "") -> PackVersion:
    """A new draft pack for a set file: the file goes to the reference-data store and the pack's manifest is that one file, with the
    set's header. A set whose file says `draft: true` stays a draft pack (publish refuses it)."""
    from . import service

    h = read(data)
    key = file_key(h["id"], h["version"])
    store = storage.store("refdata")
    service._require(actor, KIND)
    if PackVersion.objects.filter(kind=KIND, label=h["id"], version=h["version"]).exists():
        raise service.AccessDenied(f"{h['id']} {h['version']} is already registered; a changed set is a new version (or a new set).")
    store.put_bytes(key, data)
    first = h["program_years"]["from"]
    manifest = {DEFINITION: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "set": h}}
    return service.register_pack(
        actor,
        kind=KIND,
        label=h["id"],
        version=h["version"],
        manifest=manifest,
        storage_uri=store.uri(key.rsplit("/", 1)[0] + "/"),
        valid_from=date(first, 1, 1) if first else None,
        source=source or "cost-test set file",
        notes=notes,
    )


def file_bytes(pack: PackVersion) -> bytes:
    """The set's file, checked against the pack's SHA-256."""
    data = storage.store("refdata").get_bytes(file_key(pack.label, pack.version))
    if hashlib.sha256(data).hexdigest() != sha256(pack):
        raise storage.StorageError(f"{pack}: the stored set file does not match the pack's SHA-256")
    return data


def for_year(year: int | None, *, include_drafts: bool = False, workstream: str = "EE") -> PackVersion | None:
    """The set in force for a program year: the newest published set of the workstream whose first program year is not after it (an
    open-ended set, from: null, counts for every year), else, when asked and nothing is published, the newest draft that way."""
    on = date(year, 1, 1) if year else date.today()
    for statuses in [[PackVersion.Status.PUBLISHED]] + ([[PackVersion.Status.DRAFT]] if include_drafts else []):
        packs = PackVersion.objects.filter(kind=KIND, status__in=statuses, label__startswith=f"{workstream}-")
        pick = packs.filter(valid_from__lte=on).order_by("-valid_from", "-registered_at").first()
        pick = pick or packs.filter(valid_from__isnull=True).order_by("-registered_at").first()
        if pick is not None:
            return pick
    return None
