"""Cost-test definition sets as data packs (kind `cost_tests`; ReCET's COST_TESTS.md, BRD BR-06.8 / BR-06.14, D14).

A set is one YAML file (ReCET's `data/cost_tests/<id>/definition.yaml`): which value streams each cost test adds up, how it shares the
program costs, and the program years it is in force for. Its pack holds that one file, so the pack's fingerprint names the set exactly.
CPUC stewards register a set (the Cost-test sets page, the set builder or `manage.py register_cost_tests`), the engine check is
recorded (at once by the engine in the web process, `engine_check`; by hand where it is not installed: the engine release in use loads
the set; a set ReCET ships must be byte for byte the engine's own), and publish it.

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
    digest = hashlib.sha256(data).hexdigest()
    existing = PackVersion.objects.filter(kind=KIND, label=h["id"], version=h["version"]).first()
    if existing is not None:
        if sha256(existing) == digest:
            raise service.AccessDenied(f"This file is already registered as {existing} ({existing.get_status_display().lower()}).")
        raise service.AccessDenied(
            f"{h['id']} {h['version']} is already registered as {existing} with a different file (SHA-256 {sha256(existing)[:12]}; "
            f"this file {digest[:12]}). A set's id and version name one file for good: give the changed file a new version (or a new "
            "id) and register that."
        )
    shipped = engine_copy(h["id"], h["version"])
    if shipped is not None and shipped != data:
        raise service.AccessDenied(
            f"The engine ships {h['id']} {h['version']} with a different file (SHA-256 {hashlib.sha256(shipped).hexdigest()[:12]}; "
            f"this file {digest[:12]}). A built-in set is registered from the engine's own file; a changed set takes a new version."
        )
    store.put_bytes(key, data)
    first = h["program_years"]["from"]
    manifest = {DEFINITION: {"sha256": digest, "bytes": len(data), "set": h}}
    pack = service.register_pack(
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
    engine_check(actor, pack)  # recorded at once with the engine here; without it the check is recorded by hand
    return pack


def engine_copy(sid: str, version: str) -> bytes | None:
    """The engine's own file for a built-in set (ReCET ships its published sets as recet/data/cost_tests/<id>/definition.yaml), or
    None when the engine is not installed here or ships no set of that id and version."""
    try:
        from importlib import resources

        path = resources.files("recet").joinpath("data", "cost_tests", sid, "definition.yaml")
        data = path.read_bytes()
    except (ImportError, FileNotFoundError, OSError, TypeError):
        return None
    try:
        return data if read(data).get("version") == version else None
    except SetError:
        return None


def file_bytes(pack: PackVersion) -> bytes:
    """The set's file, checked against the pack's SHA-256."""
    data = storage.store("refdata").get_bytes(file_key(pack.label, pack.version))
    if hashlib.sha256(data).hexdigest() != sha256(pack):
        raise storage.StorageError(f"{pack}: the stored set file does not match the pack's SHA-256")
    return data


def download_name(pack: PackVersion) -> str:
    """The file name a set downloads under: its id and version, e.g. EE-2026_2026-10-31.yaml."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{pack.label}_{pack.version}".strip("_")) + ".yaml"


def in_words(pack: PackVersion, data: bytes | None = None) -> tuple[str | None, str]:
    """The set as the engine writes it out (ReCET's `cost_test_sets.describe`, what `recet cost-tests show` prints), and what is wrong
    when the engine cannot load it. (None, "") when the engine is not installed here."""
    try:
        from recet import cost_test_sets as engine
    except ImportError:
        return None, ""
    try:
        data = file_bytes(pack) if data is None else data
        parsed = yaml.safe_load(data.decode("utf-8-sig"))
        source = "built-in" if engine_copy(pack.label, pack.version) == data else download_name(pack)
        return engine.describe(engine.parse(parsed, hashlib.sha256(data).hexdigest(), source)), ""
    except (engine.SetError, storage.StorageError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, str(e)


def grid(pack: PackVersion) -> dict | None:
    """The set's tests as a review grid (`formula.grid`); None when its file cannot be read."""
    from .formula import grid as test_grid

    try:
        parsed = yaml.safe_load(file_bytes(pack).decode("utf-8-sig"))
    except (storage.StorageError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return test_grid(parsed) if isinstance(parsed, dict) else None


def engine_installed() -> bool:
    try:
        from recet import cost_test_sets  # noqa: F401
    except ImportError:
        return False
    return True


def engine_check(actor, pack: PackVersion):
    """Record the pack's engine_check tie-out with the engine installed in the web process (Jake, 2026-11-03: recorded automatically,
    not by hand): it passes when the engine loads the set, i.e. its validator finds nothing (every engine stream one it prices, at the
    rows, rates and suffix it prices it at; every aggregate and test over streams of its pricing), and a set the engine ships is byte for
    byte the engine's own file. None (nothing recorded) when the engine is not installed here."""
    from . import service

    try:
        import recet
        from recet import cost_test_sets as engine
    except ImportError:
        return None
    release = recet.__version__
    data = file_bytes(pack)
    try:
        parsed = yaml.safe_load(data.decode("utf-8-sig"))
        errors = engine.validate(parsed)
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        errors = [f"not readable: {e}"]
    shipped = engine_copy(pack.label, pack.version)
    if shipped is not None and shipped != data:
        errors.append(f"the engine ships {pack.label} {pack.version} with a different file")
    if errors:
        summary = (
            f"ReCET {release} refuses the set: " + "; ".join(errors[:3]) + (f"; and {len(errors) - 3} more" if len(errors) > 3 else "")
        )
    else:
        s = engine.parse(parsed, hashlib.sha256(data).hexdigest(), "built-in" if shipped == data else download_name(pack))
        summary = f"ReCET {release} loads the set: {len(s.tests)} test{'s' if len(s.tests) != 1 else ''} over {len(s.streams)} streams" + (
            ", byte for byte the engine's own file" if shipped == data else ""
        )
    return service.record_tie_out(
        actor, pack, check="engine_check", passed=not errors, summary=summary, engine_release=release, report={"errors": errors}
    )


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
