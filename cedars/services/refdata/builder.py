"""The cost-test set builder (design "Cost-test set builder: scope and design", Jake 2026-11-03).

A set is a ReCET YAML file (ReCET's COST_TESTS.md) that the CPUC would otherwise write by hand. The builder keeps the set being built as
its YAML's mapping (`CostTestBuild.data`), lets people change it one part at a time, and on every save writes it out and checks it with the
engine's own validator (`recet.cost_test_sets.validate`, in the web process), filing each message under the step it belongs to. The engine
validates; the platform only arranges. Without the engine installed the builder still saves, checks the header itself, and says the engine
check could not run.

Who does what (Jake, 2026-11-03): every signed-in user builds sets and prices their own analysis runs on them; only the CPUC's cost-test
pack managers register a set as a pack (`cost_tests.register_set`, as an upload does), publish it and pin it to a filing. A registered build
is kept, read-only, and copied to start the next version: a published set is never edited.

Every function that changes a build takes the acting user and writes an audit event: cost_test_build.started (from what),
cost_test_build.changed (the step, and that part before and after), cost_test_build.registered (the pack), cost_test_build.discarded."""

from __future__ import annotations

import copy
import datetime as dt
import re

import yaml
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications

from . import cost_tests
from . import service as packs
from .models import CostTestBuild, PackVersion

Status = CostTestBuild.Status

# The builder's steps, in order. Each message of the engine's validator names the part it is about (`tests.TRC.costs: ...`), which
# `step_of` turns into the step that edits it.
STEPS = [
    ("set", "Set"),
    ("pricings", "Pricings"),
    ("engine", "Streams the engine prices"),
    ("inputs", "Streams from input columns"),
    ("aggregates", "Aggregates"),
    ("tests", "Tests"),
    ("review", "Review"),
]
STEP_LABELS = dict(STEPS)
PAGES = [k for k, _ in STEPS]  # every step has a page of its own

# the set file's keys in the order the builder writes them (ReCET's EE-2026 order)
TOP = ["schema_version", "id", "workstream", "version", "label", "description", "draft", "program_years", "pricings", "streams",
       "aggregates", "tests"]  # fmt: skip
PRICING_KEYS = ["label", "acc_rows", "discount_rate_quarterly", "discount_rate_annual", "suffix", "participant_suffix"]
STREAM_KEYS = ["source", "kind", "fuel", "columns", "columns_2nd", "basis", "ntg", "market_effects", "pricings", "naming", "gross",
               "internal", "unit", "definition"]  # fmt: skip
AGGREGATE_KEYS = ["pricing", "plus", "minus", "gross", "unit", "definition"]
TEST_KEYS = ["label", "pricing", "benefits", "numerator_adders", "costs", "program_costs", "variants", "lifecycle_net_benefit",
             "levelized", "headline", "filing", "definition"]  # fmt: skip
KEYS = {"pricings": PRICING_KEYS, "streams": STREAM_KEYS, "aggregates": AGGREGATE_KEYS, "tests": TEST_KEYS}
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")  # ReCET's cost_test_sets.NAME: a pricing id or a stream's column name
WORKSTREAM = re.compile(r"^[A-Z][A-Z0-9]{0,15}$")
TAIL = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LONG = 90  # text longer than this is written as a folded block (>-)

# the engine's closed vocabularies (ReCET cost_test_sets), kept here too so the forms work when the engine is not installed
ACC_ROWS = {"trc": "TRC avoided costs", "sct_base": "SCT Base avoided costs", "sct_high": "SCT High avoided costs"}
RATE_COLUMNS = {
    "DiscountRateQtr": "DiscountRateQtr (primary, quarterly)",
    "DiscountRateAnnual": "DiscountRateAnnual (primary, annual)",
    "DiscountRateQtr_SCT": "DiscountRateQtr_SCT (societal, quarterly)",
    "DiscountRateAnnual_SCT": "DiscountRateAnnual_SCT (societal, annual)",
}


class BuildError(ValueError):
    """A change the builder refuses (the message says why); nothing is saved."""


def engine():
    """The engine's cost_test_sets module, or None when the engine is not installed in this process."""
    try:
        from recet import cost_test_sets
    except ImportError:
        return None
    return cost_test_sets


def engine_release() -> str:
    try:
        import recet
    except ImportError:
        return ""
    return str(getattr(recet, "__version__", ""))


def catalog() -> dict | None:
    """The streams the engine prices ({name: EngineStream}; ReCET 0.1.92+), or None when the engine here has no catalog."""
    e = engine()
    return e.engine_streams() if e is not None and hasattr(e, "engine_streams") else None


# --- who sees and changes which


def can_submit(user, organisation) -> bool:
    """Whether the user may start runs in the organization (calculations.submit): who may build sets shared with it."""
    return bool(organisation is not None and access.has_permission(user, organisation, "calculations.submit"))


def is_manager(user) -> bool:
    """The CPUC's cost-test pack managers: they register sets as packs (and, later, take offered ones)."""
    return packs.can_manage(user, cost_tests.KIND)


def _mine(user) -> Q:
    orgs = [o for o in access.organisations_of(user) if can_submit(user, o)]
    return Q(owner=user) | Q(organisation__in=orgs)


def mine(user):
    """A user's own builds and those shared with an organization where they may start runs (My sets). Discarded ones are gone."""
    return (
        CostTestBuild.objects.exclude(status=Status.DISCARDED).filter(_mine(user)).select_related("owner", "organisation", "pack", "parent")
    )


def visible(user):
    """The builds a user may open: their own sets (`mine`) and, for the CPUC's cost-test pack managers, every set offered to the CPUC."""
    q = _mine(user) | Q(status=Status.OFFERED) if is_manager(user) else _mine(user)
    return CostTestBuild.objects.exclude(status=Status.DISCARDED).filter(q).select_related("owner", "organisation", "pack", "parent")


def offered():
    """The sets offered to the CPUC, oldest offer first (the managers' list)."""
    return CostTestBuild.objects.filter(status=Status.OFFERED).select_related("owner", "organisation", "offered_by").order_by("offered_at")


def can_change(user, build: CostTestBuild) -> bool:
    if build.status not in (Status.BUILDING, Status.OFFERED):
        return False
    return build.owner_id == user.pk or can_submit(user, build.organisation)


def _require(user, build: CostTestBuild) -> None:
    if build.status == Status.REGISTERED:
        raise AccessDenied(f"{build} is registered as a pack, so it no longer changes. Copy it to start the next version.")
    if not can_change(user, build):
        raise AccessDenied("You cannot change this set.")


# --- reading and writing the file


def _plain(v):
    """A YAML value as JSON can keep it: dates (an unquoted version such as 2026-10-31) become their ISO text."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_plain(x) for x in v]
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


def read_file(data: bytes) -> tuple[dict, dict]:
    """A set file's mapping and the keys set aside: those the set format does not have (ReCET's validator would refuse them), kept apart
    and shown so nothing is dropped silently. A file that is not a YAML mapping is refused; anything else is imported as it is and its
    problems shown on the steps."""
    try:
        d = yaml.safe_load(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise BuildError(f"Not a readable YAML file: {e}") from None
    if not isinstance(d, dict):
        raise BuildError("A cost-test set file is a YAML mapping (keys and values); this file is not.")
    d = _plain(d)
    aside = {k: d.pop(k) for k in [k for k in d if k not in TOP]}
    for part, keys in KEYS.items():
        entries = d.get(part)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            for k in [k for k in entry if k not in keys]:
                aside[f"{part}.{name}.{k}"] = entry.pop(k)
    return d, aside


class _Dumper(yaml.SafeDumper):
    pass


class _Flow(list):
    pass


class _FlowMap(dict):
    pass


class _Folded(str):
    pass


_Dumper.add_representer(_Flow, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))
_Dumper.add_representer(_FlowMap, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items(), flow_style=True))
_Dumper.add_representer(_Folded, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style=">"))


def _text(v):
    return _Folded(v) if isinstance(v, str) and len(v) > LONG and "\n" not in v and v == v.strip() and "  " not in v else v


def _ordered(entry: dict, keys: list[str]) -> dict:
    out = {}
    for k in [*[k for k in keys if k in entry], *[k for k in entry if k not in keys]]:
        v = entry[k]
        if isinstance(v, list):
            v = _Flow(v)
        elif isinstance(v, dict):
            v = _FlowMap(v)
        out[k] = _text(v)
    return out


def to_yaml(data: dict) -> bytes:
    """The set as its file: keys in ReCET's order, short lists and mappings on one line, long text folded, and a header comment saying
    what wrote it. Loading the file gives back exactly `data`."""
    out = {}
    for k in [*[k for k in TOP if k in data], *[k for k in data if k not in TOP]]:
        v = data[k]
        if k in KEYS and isinstance(v, dict):
            v = {name: (_ordered(e, KEYS[k]) if isinstance(e, dict) else e) for name, e in v.items()}
        elif k == "program_years" and isinstance(v, dict):
            v = _FlowMap(v)
        out[k] = _text(v)
    head = (
        f"# {data.get('id') or 'A cost-test set'}: {data.get('label') or ''}".rstrip(": ")
        + "\n# Written by the CEDARS cost-test set builder in ReCET's cost-test set format (ReCET recet/docs/COST_TESTS.md).\n\n"
    )
    body = yaml.dump(out, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=150, default_flow_style=False)
    return (head + body).encode("utf-8")


def file_name(build: CostTestBuild) -> str:
    d = build.data or {}
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{d.get('id') or 'cost-test-set'}_{d.get('version') or ''}".strip("_")) + ".yaml"


# --- checking


def step_of(message: str, data: dict) -> str:
    """The step a validator message belongs to, from the part it names."""
    head = message.split(":", 1)[0].strip()
    part, _, rest = head.partition(".")
    if part == "pricings":
        return "pricings"
    if part == "streams":
        name = rest.split(".", 1)[0]
        s = (data.get("streams") or {}).get(name)
        return "inputs" if isinstance(s, dict) and s.get("source") == "input" else "engine"
    if part in ("aggregates", "tests"):
        return part
    return "set"


def check(data: dict, *, exclude_pack: PackVersion | None = None) -> tuple[dict[str, list[str]], str]:
    """Every problem with the set, by step ({step: [message, ...]}, steps in order, only those with problems), and the engine release that
    checked it ("" when the engine is not installed: then only the platform's own header check ran)."""
    out: dict[str, list[str]] = {}
    e = engine()
    messages = e.validate(data) if e is not None else []
    if e is None:
        try:
            cost_tests.read(to_yaml(data))
        except cost_tests.SetError as err:
            messages = [m.strip() for m in str(err).split(";")]
    for m in messages:
        out.setdefault(step_of(m, data), []).append(m)
    sid, version = data.get("id"), data.get("version")
    if isinstance(sid, str) and isinstance(version, str):
        taken = PackVersion.objects.filter(kind=cost_tests.KIND, label=sid, version=version.strip())
        if exclude_pack is not None:
            taken = taken.exclude(pk=exclude_pack.pk)
        if taken.exists():
            out.setdefault("set", []).append(
                f"version: {sid} {version} is already registered; a set's id and version name one file for good, "
                "so give this one a new version"
            )
    return {k: out[k] for k, _ in STEPS if k in out}, engine_release() if e is not None else ""


def _save(actor, build: CostTestBuild, step: str, before, after) -> CostTestBuild:
    build.problems, build.checked_by = check(build.data, exclude_pack=build.pack)
    build.save()
    if before != after:
        audit.record(
            "cost_test_build.changed",
            obj=build,
            before={step: before},
            after={step: after},
            actor=actor,
            organisation=build.organisation,
        )
    return build


# --- starting a build


def _version_today() -> str:
    return dt.date.today().isoformat()


@transaction.atomic
def _start(actor, data: dict, *, source: str, aside=None, parent=None, parent_build=None, organisation=None) -> CostTestBuild:
    if organisation is not None and not can_submit(actor, organisation):
        raise AccessDenied(f"You need to be able to start runs in {organisation.code} to share a set with it.")
    data = copy.deepcopy(data)
    build = CostTestBuild(
        data=data,
        set_aside=aside or {},
        source=source[:300],
        parent=parent,
        parent_build=parent_build,
        parent_data=copy.deepcopy(data),
        owner=actor,
        organisation=organisation,
    )
    build.problems, build.checked_by = check(data)
    build.save()
    audit.record(
        "cost_test_build.started",
        obj=build,
        after={"from": source, "id": data.get("id"), "version": data.get("version"), "set_aside": sorted(build.set_aside)},
        actor=actor,
        organisation=organisation,
    )
    return build


def _next_version(data: dict) -> dict:
    """A copy's header: a new version (today), since a registered id and version name one file for good."""
    data = copy.deepcopy(data)
    data["version"] = _version_today()
    return data


def start_from_pack(actor, pack: PackVersion, *, organisation=None) -> CostTestBuild:
    if pack.kind != cost_tests.KIND:
        raise BuildError(f"{pack} is not a cost-test set.")
    data, aside = read_file(cost_tests.file_bytes(pack))
    return _start(
        actor, _next_version(data), source=f"a copy of {pack.label} {pack.version}", aside=aside, parent=pack, organisation=organisation
    )


def start_from_build(actor, other: CostTestBuild, *, organisation=None) -> CostTestBuild:
    if not visible(actor).filter(pk=other.pk).exists():
        raise AccessDenied("You cannot open that set.")
    return _start(
        actor,
        _next_version(other.data),
        source=f"a copy of {other} (set builder)",
        aside=other.set_aside,
        parent=other.pack or other.parent,
        parent_build=other,
        organisation=organisation,
    )


def engine_sets() -> list[tuple[str, str]]:
    """[(id, label)] of the sets the engine ships, or [] without the engine."""
    e = engine()
    return e.catalog() if e is not None else []


def start_from_engine(actor, sid: str, *, organisation=None) -> CostTestBuild:
    e = engine()
    if e is None or sid not in e.builtin():
        raise BuildError(f"The engine here ships no set {sid}.")
    s = e.builtin()[sid]
    data, aside = read_file(s.path.read_bytes())
    parent = PackVersion.objects.filter(kind=cost_tests.KIND, label=s.id, version=s.version).first()
    return _start(
        actor, _next_version(data), source=f"the engine's {s.id} {s.version} (ReCET {engine_release()})", aside=aside, parent=parent,
        organisation=organisation,
    )  # fmt: skip


def start_from_file(actor, data: bytes, name: str, *, organisation=None) -> CostTestBuild:
    """An uploaded set file, written by hand or downloaded from elsewhere (Jake, 2026-11-03): read as it is, keys the format does not have
    set aside, and its problems shown on the steps. Its version is kept: the file says what it is."""
    mapping, aside = read_file(data)
    return _start(actor, mapping, source=f"the file {name}"[:300], aside=aside, organisation=organisation)


def blank(year: int | None = None) -> dict:
    """A new set with nothing in it but one pricing (the TRC avoided costs at the primary rates) and the program costs."""
    year = year or dt.date.today().year + 1
    return {
        "schema_version": 1,
        "id": f"EE-{year}",
        "workstream": "EE",
        "version": _version_today(),
        "label": "",
        "description": "",
        "draft": False,
        "program_years": {"from": year, "to": None},
        "pricings": {
            "primary": {
                "label": "TRC avoided costs, primary discount rate",
                "acc_rows": "trc",
                "discount_rate_quarterly": "DiscountRateQtr",
                "discount_rate_annual": "DiscountRateAnnual",
                "suffix": "",
                "participant_suffix": "",
            }
        },
        "streams": {
            "ProgramCosts": {
                "source": "program",
                "kind": "cost",
                "unit": "$ (PV)",
                "definition": "The program's ProgramCost.csv rows, discounted annually from the First Year at the pricing's annual rate, "
                "and shared out to the program's measures by the test's share rule.",
            }
        },
        "aggregates": {},
        "tests": {},
    }


def start_blank(actor, *, organisation=None) -> CostTestBuild:
    return _start(actor, blank(), source="a blank set", organisation=organisation)


# --- step 1: the set


def id_parts(data: dict) -> tuple[str, int | None, str]:
    """The workstream, first program year and lower-case tail of the set's id ("EE-2026-neb" -> "EE", 2026, "neb")."""
    m = re.match(r"^([A-Z][A-Z0-9]*)-([0-9]{4})(?:-(.+))?$", str(data.get("id") or ""))
    if not m:
        py = data.get("program_years") or {}
        return str(data.get("workstream") or ""), py.get("from") if isinstance(py, dict) else None, ""
    return m.group(1), int(m.group(2)), m.group(3) or ""


def make_id(workstream: str, year: int, tail: str = "") -> str:
    return f"{workstream}-{year}" + (f"-{tail}" if tail else "")


HEADER = ["id", "workstream", "version", "label", "description", "draft", "program_years"]


def set_header(actor, build: CostTestBuild, *, workstream: str, first_year: int, tail: str, label: str, description: str, version: str,
               draft: bool, years_from: int | None, years_to: int | None, share: bool, organisation=None) -> CostTestBuild:  # fmt: skip
    """Step 1. The id is built from the workstream, the first program year and the tail, never typed."""
    _require(actor, build)
    workstream, tail = workstream.strip().upper(), tail.strip().lower()
    if not WORKSTREAM.match(workstream):
        raise BuildError("The workstream is upper-case letters and digits, starting with a letter, e.g. EE or IQP.")
    if tail and not TAIL.match(tail):
        raise BuildError("The id's ending is lower-case letters and digits, e.g. neb or draft.")
    before = {k: copy.deepcopy(build.data.get(k)) for k in HEADER}
    d = build.data
    d["schema_version"] = d.get("schema_version", 1)
    d["id"], d["workstream"] = make_id(workstream, first_year, tail), workstream
    d["version"], d["label"] = version.strip(), label.strip()
    d["description"], d["draft"] = description.strip(), bool(draft)
    d["program_years"] = {"from": years_from, "to": years_to}
    build.data = {k: d[k] for k in [*[k for k in TOP if k in d], *[k for k in d if k not in TOP]]}
    if share and organisation is not None and build.organisation_id != organisation.pk:
        if not can_submit(actor, organisation):
            raise AccessDenied(f"You need to be able to start runs in {organisation.code} to share a set with it.")
        build.organisation = organisation
    elif not share and build.organisation_id is not None:
        if build.owner_id != actor.pk:
            raise AccessDenied("Only the person who started the set stops sharing it.")
        build.organisation = None
    return _save(actor, build, "set", before, {k: copy.deepcopy(build.data.get(k)) for k in HEADER})


# --- step 2: pricings


def uses_of_pricing(data: dict, pid: str) -> list[str]:
    """Where the set reads a pricing: the streams priced in it, and the aggregates and tests on it."""
    out = [f"stream {n}" for n, s in (data.get("streams") or {}).items() if isinstance(s, dict) and pid in (s.get("pricings") or [])]
    out += [f"aggregate {n}" for n, a in (data.get("aggregates") or {}).items() if isinstance(a, dict) and a.get("pricing") == pid]
    out += [f"test {n}" for n, t in (data.get("tests") or {}).items() if isinstance(t, dict) and t.get("pricing") == pid]
    return out


def set_pricings(actor, build: CostTestBuild, rows: list[dict], new: dict | None = None) -> CostTestBuild:
    """Step 2. `rows` are the existing pricings in order, each {id, remove, label, acc_rows, discount_rate_quarterly,
    discount_rate_annual, suffix, participant_suffix}; `new` adds one (its id typed once). A pricing a stream, aggregate or test still
    uses is not removed."""
    _require(actor, build)
    before = copy.deepcopy(build.data.get("pricings") or {})
    out = {}
    for r in rows:
        pid = r["id"]
        if r.get("remove"):
            used = uses_of_pricing(build.data, pid)
            if used:
                raise BuildError(f"Pricing {pid} is still used by {', '.join(used)}: take it out of those first.")
            continue
        out[pid] = _pricing(r, before.get(pid))
    if new:
        pid = new["id"].strip()
        if not NAME.match(pid):
            raise BuildError("A pricing's id is a name: letters, digits and _, starting with a letter (e.g. sct_base).")
        if pid in out or pid in before:
            raise BuildError(f"The set already has a pricing {pid}.")
        out[pid] = _pricing(new, None)
    if not out:
        raise BuildError("A set needs at least one pricing.")
    build.data["pricings"] = out
    return _save(actor, build, "pricings", before, copy.deepcopy(out))


def _pricing(r: dict, old: dict | None) -> dict:
    p = dict(old or {})
    for k in PRICING_KEYS:
        p[k] = (r.get(k) or "").strip()
    return {k: p[k] for k in [*PRICING_KEYS, *[k for k in p if k not in PRICING_KEYS]]}


# --- step 3: the engine's streams


def engine_rows(data: dict) -> list[dict]:
    """The engine's catalog as the step shows it: per stream, whether the set includes it, the set's pricings it can be read in (and
    which it is), its unit and definition, and its fixed facts. The set's own order first, then the streams it leaves out."""
    cat = catalog() or {}
    e = engine()
    streams, pricings = data.get("streams") or {}, data.get("pricings") or {}
    order = [n for n, s in streams.items() if isinstance(s, dict) and s.get("source") == "engine" and n in cat]
    order += [n for n in cat if n not in order]
    out = []
    for n in order:
        c, s = cat[n], streams.get(n) if isinstance(streams.get(n), dict) else None
        included = s is not None and s.get("source") == "engine"
        can = e.engine_pricings(n, pricings)
        cannot = {
            p: e.engine_pricing_problem(n, p, v) or f"the engine does not price {n} in pricing {p}"
            for p, v in pricings.items()
            if p not in can and isinstance(v, dict)
        }
        out.append(
            {
                "name": n,
                "included": included,
                "pricings": list((s or {}).get("pricings") or []) if included else [],
                "can": can,
                "cannot": cannot,
                "unit": (s or {}).get("unit", c.unit) if included else c.unit,
                "definition": (s or {}).get("definition", c.definition) if included else c.definition,
                "facts": c,
            }
        )
    return out


def unknown_engine_streams(data: dict) -> list[str]:
    """Engine streams the set names that the engine's catalog does not have (shown on the step; the validator refuses them)."""
    cat = catalog() or {}
    return [n for n, s in (data.get("streams") or {}).items() if isinstance(s, dict) and s.get("source") == "engine" and n not in cat]


def set_engine_streams(actor, build: CostTestBuild, choices: dict[str, dict]) -> CostTestBuild:
    """Step 3. `choices` is {name: {include, pricings, unit, definition}} for the engine's streams. An included stream takes its fixed
    facts from the engine's catalog (Jake, 2026-11-03: a set chooses an engine stream's pricings, unit and definition only); one left out
    is dropped. The set's order is kept; a stream newly included goes after its last engine stream."""
    _require(actor, build)
    cat = catalog()
    if cat is None:
        raise BuildError("The engine installed here has no catalog of its streams, so they cannot be changed here.")
    streams = build.data.get("streams") or {}
    before = {n: copy.deepcopy(s) for n, s in streams.items() if isinstance(s, dict) and s.get("source") == "engine"}
    out, placed = {}, False
    last_engine = max((i for i, (n, s) in enumerate(streams.items()) if isinstance(s, dict) and s.get("source") == "engine"), default=-1)
    new = [n for n in cat if (choices.get(n) or {}).get("include") and n not in streams]

    def add_new():
        for n in new:
            out[n] = _engine_stream(cat[n], choices[n], None)

    for i, (n, s) in enumerate(streams.items()):
        if isinstance(s, dict) and s.get("source") == "engine" and n in cat:
            ch = choices.get(n) or {}
            if ch.get("include"):
                out[n] = _engine_stream(cat[n], ch, s)
        else:
            out[n] = s
        if i == last_engine:
            add_new()
            placed = True
    if not placed:
        out = {**{n: _engine_stream(cat[n], choices[n], None) for n in new}, **out}
    build.data["streams"] = out
    after = {n: copy.deepcopy(s) for n, s in out.items() if isinstance(s, dict) and s.get("source") == "engine"}
    return _save(actor, build, "engine", before, after)


def _engine_stream(c, choice: dict, old: dict | None) -> dict:
    s = {"source": "engine", **c.facts(), "pricings": list(choice.get("pricings") or []),
         "unit": (choice.get("unit") or "").strip(), "definition": (choice.get("definition") or "").strip()}  # fmt: skip
    for k in [k for k in (old or {}) if k not in s and k in STREAM_KEYS]:
        s[k] = old[k]
    return {k: s[k] for k in [*[k for k in STREAM_KEYS if k in s], *[k for k in s if k not in STREAM_KEYS]]}


# --- step 4: input streams


def input_streams(data: dict) -> list[tuple[str, dict]]:
    return [(n, s) for n, s in (data.get("streams") or {}).items() if isinstance(s, dict) and s.get("source") == "input"]


def _rename_everywhere(data: dict, old: str, new: str) -> None:
    for a in (data.get("aggregates") or {}).values():
        if isinstance(a, dict):
            for k in ("plus", "minus"):
                a[k] = [new if x == old else x for x in (a.get(k) or [])] if k in a else a.get(k)
    for t in (data.get("tests") or {}).values():
        if isinstance(t, dict):
            for k in ("benefits", "numerator_adders", "costs"):
                if k in t:
                    t[k] = [new if x == old else x for x in (t.get(k) or [])]


def put_input_stream(actor, build: CostTestBuild, old_name: str | None, fields: dict) -> CostTestBuild:
    """Step 4: add an input stream (old_name None) or change one. Renaming a stream renames it wherever an aggregate or test names it.
    `fields`: name, kind, columns, columns_2nd, basis, ntg, market_effects, pricings, naming, gross, unit, definition."""
    _require(actor, build)
    name = fields["name"].strip()
    if not NAME.match(name):
        raise BuildError("A stream's name is the column it writes: letters, digits and _, starting with a letter (e.g. HealthNEB).")
    streams = build.data.get("streams") or {}
    if old_name is not None and old_name not in streams:
        raise BuildError(f"The set has no stream {old_name}.")
    if name in streams and name != old_name:
        raise BuildError(f"The set already has a stream {name}.")
    before = copy.deepcopy(streams.get(old_name)) if old_name else None
    s = {"source": "input", "kind": fields["kind"], "columns": list(fields["columns"])}
    if fields.get("columns_2nd"):
        s["columns_2nd"] = list(fields["columns_2nd"])
    s.update(basis=fields["basis"], ntg=fields["ntg"], market_effects=fields["market_effects"], pricings=list(fields["pricings"]))
    s["naming"] = fields["naming"]
    if fields.get("gross"):
        s["gross"] = fields["gross"]
    s.update(unit=fields.get("unit", "").strip(), definition=fields.get("definition", "").strip())
    for k in [k for k in (before or {}) if k not in s and k not in ("gross", "columns_2nd") and k in STREAM_KEYS]:
        s[k] = before[k]
    if old_name is None:
        last_input = max(
            (i for i, (n, x) in enumerate(streams.items()) if isinstance(x, dict) and x.get("source") == "input"), default=None
        )
        items = list(streams.items())
        at = len(items) if last_input is None else last_input + 1
        streams = dict(items[:at] + [(name, s)] + items[at:])
    else:
        streams = {(name if n == old_name else n): (s if n == old_name else x) for n, x in streams.items()}
        if name != old_name:
            _rename_everywhere(build.data, old_name, name)
    build.data["streams"] = streams
    return _save(actor, build, "inputs", {old_name or name: before}, {name: copy.deepcopy(s)})


def remove_input_stream(actor, build: CostTestBuild, name: str) -> CostTestBuild:
    """Remove an input stream. An aggregate or test that still names it is shown as a problem on its step."""
    _require(actor, build)
    streams = build.data.get("streams") or {}
    s = streams.get(name)
    if not (isinstance(s, dict) and s.get("source") == "input"):
        raise BuildError(f"The set has no input stream {name}.")
    build.data["streams"] = {n: x for n, x in streams.items() if n != name}
    return _save(actor, build, "inputs", {name: copy.deepcopy(s)}, {name: None})


# --- steps 5 and 6: aggregates and tests

TEST_ID = re.compile(r"^[A-Z][A-Za-z0-9]{1,15}$")  # ReCET's rule: it names the columns <T>Ben, <T>Cost, <T>Ratio
TEST_DEFAULTS = {"numerator_adders": [], "program_costs": "none", "variants": [], "lifecycle_net_benefit": False, "levelized": [],
                 "headline": False, "filing": True, "definition": ""}  # fmt: skip
AGGREGATE_DEFAULTS = {"minus": [], "gross": False, "unit": "", "definition": ""}
ALWAYS = {"tests": ["label", "pricing", "benefits", "costs", "program_costs", "variants"], "aggregates": ["pricing", "plus"]}


def readable_streams(data: dict) -> list[tuple[str, dict]]:
    """The streams an aggregate or test can name (every one but the program costs, which a test takes through its share)."""
    return [(n, s) for n, s in (data.get("streams") or {}).items() if isinstance(s, dict) and s.get("source") != "program"]


def has_program_costs(data: dict) -> bool:
    return any(isinstance(s, dict) and s.get("source") == "program" for s in (data.get("streams") or {}).values())


def _entry(part: str, new: dict, old: dict | None) -> dict:
    """An aggregate or test as the set writes it: its keys in the format's order; a key left at its default is written only when the
    entry already had it (so saving a page unchanged gives back the same file) or the format always writes it."""
    keys, defaults = (TEST_KEYS, TEST_DEFAULTS) if part == "tests" else (AGGREGATE_KEYS, AGGREGATE_DEFAULTS)
    out = {}
    for k in keys:
        if k not in new:
            continue
        v = new[k]
        if k in defaults and v == defaults[k] and k not in (old or {}) and not (old is None and k in ALWAYS[part]):
            continue
        out[k] = v
    for k, v in (old or {}).items():  # keys this page does not edit stay as they were
        if k not in keys:
            out[k] = v
    return out


def _put(actor, build: CostTestBuild, part: str, old_name: str | None, name: str, entry: dict, *, step: str) -> CostTestBuild:
    entries = build.data.get(part) if isinstance(build.data.get(part), dict) else {}
    if old_name is not None and old_name not in entries:
        raise BuildError(f"The set has no {'test' if part == 'tests' else 'aggregate'} {old_name}.")
    if name in entries and name != old_name:
        raise BuildError(f"The set already has {'a test' if part == 'tests' else 'an aggregate'} {name}.")
    before = copy.deepcopy(entries.get(old_name)) if old_name else None
    e = _entry(part, entry, before if isinstance(before, dict) else None)
    if old_name is None:
        entries = {**entries, name: e}
    else:
        entries = {(name if n == old_name else n): (e if n == old_name else x) for n, x in entries.items()}
        if part == "tests" and name != old_name:  # a test that shares this one's program costs follows the rename
            for t in entries.values():
                if isinstance(t, dict) and isinstance(t.get("program_costs"), dict) and t["program_costs"].get("share") == old_name:
                    t["program_costs"] = {"share": name}
    build.data[part] = entries
    return _save(actor, build, step, {old_name or name: before}, {name: copy.deepcopy(e)})


def _remove(actor, build: CostTestBuild, part: str, name: str, *, step: str) -> CostTestBuild:
    _require(actor, build)
    entries = build.data.get(part) if isinstance(build.data.get(part), dict) else {}
    if name not in entries:
        raise BuildError(f"The set has no {'test' if part == 'tests' else 'aggregate'} {name}.")
    before = copy.deepcopy(entries[name])
    build.data[part] = {n: x for n, x in entries.items() if n != name}
    return _save(actor, build, step, {name: before}, {name: None})


def put_aggregate(actor, build: CostTestBuild, old_name: str | None, fields: dict) -> CostTestBuild:
    """Step 5: add an aggregate (old_name None) or change one. `fields`: name, pricing, plus, minus, gross, unit, definition."""
    _require(actor, build)
    name = fields["name"].strip()
    if not NAME.match(name):
        raise BuildError("An aggregate's name is the column it writes: letters, digits and _, starting with a letter.")
    entry = {
        "pricing": fields["pricing"],
        "plus": list(fields.get("plus") or []),
        "minus": list(fields.get("minus") or []),
        "gross": bool(fields.get("gross")),
        "unit": (fields.get("unit") or "").strip(),
        "definition": (fields.get("definition") or "").strip(),
    }
    return _put(actor, build, "aggregates", old_name, name, entry, step="aggregates")


def remove_aggregate(actor, build: CostTestBuild, name: str) -> CostTestBuild:
    return _remove(actor, build, "aggregates", name, step="aggregates")


def put_test(actor, build: CostTestBuild, old_id: str | None, fields: dict) -> CostTestBuild:
    """Step 6: add a test (old_id None) or change one. `fields`: id, label, pricing, benefits, numerator_adders, costs, share ("" none,
    "positive_benefits" or another test's id), variants, lifecycle_net_benefit, levelized, headline, filing, definition. A new test goes
    last; renaming one renames it in the share of every test that shares its program costs."""
    _require(actor, build)
    tid = fields["id"].strip()
    if not TEST_ID.match(tid):
        raise BuildError("A test's id starts with a capital letter and has 2 to 16 letters and digits (TRC, PAC, JST1): it names the "
                         "columns <T>Ben, <T>Cost and <T>Ratio.")  # fmt: skip
    share = fields.get("share") or ""
    entry = {
        "label": (fields.get("label") or "").strip(),
        "pricing": fields["pricing"],
        "benefits": list(fields.get("benefits") or []),
        "numerator_adders": list(fields.get("numerator_adders") or []),
        "costs": list(fields.get("costs") or []),
        "program_costs": {"share": share} if share else "none",
        "variants": [v for v in ("NoAdmin", "Gross") if v in (fields.get("variants") or [])],
        "lifecycle_net_benefit": bool(fields.get("lifecycle_net_benefit")),
        "levelized": [v for v in ("", "NoAdmin") if v in (fields.get("levelized") or [])],
        "headline": bool(fields.get("headline")),
        "filing": bool(fields.get("filing", True)),
        "definition": (fields.get("definition") or "").strip(),
    }
    old = (build.data.get("tests") or {}).get(old_id) if old_id else None
    if isinstance(old, dict) and old.get("program_costs") is None and "program_costs" in old and not share:
        entry["program_costs"] = None  # "none" written as null in the file stays null
    return _put(actor, build, "tests", old_id, tid, entry, step="tests")


def remove_test(actor, build: CostTestBuild, tid: str) -> CostTestBuild:
    """Remove a test. A test that shared its program costs is shown as a problem on the step."""
    return _remove(actor, build, "tests", tid, step="tests")


def move_test(actor, build: CostTestBuild, tid: str, by: int) -> CostTestBuild:
    """Move a test up (by -1) or down (by 1): the set's order is the order runs write, describe and show its tests."""
    _require(actor, build)
    tests = build.data.get("tests") if isinstance(build.data.get("tests"), dict) else {}
    order = list(tests)
    if tid not in order:
        raise BuildError(f"The set has no test {tid}.")
    i = order.index(tid)
    j = max(0, min(len(order) - 1, i + by))
    if i == j:
        return build
    order.insert(j, order.pop(i))
    build.data["tests"] = {t: tests[t] for t in order}
    return _save(actor, build, "tests", {"order": list(tests)}, {"order": order})


def share_choices(data: dict, tid: str | None) -> list[tuple[str, str]]:
    """What a test may share its program costs on: none, its own share of the positive benefits, or another test's share (one that
    computes its own)."""
    out = [("", "No program costs"), ("positive_benefits", "Its own share: the measure's share of the program's positive benefits")]
    for t, v in (data.get("tests") or {}).items():
        if (
            t != tid
            and isinstance(v, dict)
            and isinstance(v.get("program_costs"), dict)
            and v["program_costs"].get("share") == "positive_benefits"
        ):
            out.append((t, f"{t}'s share (the same program costs, shared as {t} shares them)"))
    return out


# --- what changed since the set it started from

PARTS = [("pricings", "Pricing"), ("streams", "Stream"), ("aggregates", "Aggregate"), ("tests", "Test")]


def changes(build: CostTestBuild) -> list[dict]:
    """The set against the one it was copied from (`parent_data`): each header field, pricing, stream, aggregate and test added, removed
    or changed (with the keys that changed), and the tests' order. Empty for a set started from nothing."""
    old, new = build.parent_data or {}, build.data or {}
    if not old:
        return []
    out = []
    for k in HEADER:
        if k in ("id", "version"):
            continue
        if old.get(k) != new.get(k):
            out.append({"part": "Set", "name": k.replace("_", " "), "what": "changed", "keys": []})
    if old.get("id") != new.get("id"):
        out.insert(0, {"part": "Set", "name": "id", "what": f"{old.get('id')} to {new.get('id')}", "keys": []})
    for part, label in PARTS:
        a = old.get(part) if isinstance(old.get(part), dict) else {}
        b = new.get(part) if isinstance(new.get(part), dict) else {}
        for n in b:
            if n not in a:
                out.append({"part": label, "name": n, "what": "added", "keys": []})
            elif a[n] != b[n]:
                x, y = a[n] if isinstance(a[n], dict) else {}, b[n] if isinstance(b[n], dict) else {}
                keys = [k for k in dict.fromkeys([*x, *y]) if x.get(k) != y.get(k)]
                out.append({"part": label, "name": n, "what": "changed", "keys": keys})
        out += [{"part": label, "name": n, "what": "removed", "keys": []} for n in a if n not in b]
        if part == "tests" and [t for t in a if t in b] != [t for t in b if t in a]:
            out.append({"part": "Test", "name": "order", "what": "changed", "keys": []})
    return out


# --- the end of a build


@transaction.atomic
def discard(actor, build: CostTestBuild) -> None:
    _require(actor, build)
    if build.owner_id != actor.pk and not access.is_org_admin(actor, build.organisation):
        raise AccessDenied("Only the person who started the set (or the organization's administrator) discards it.")
    build.status = Status.DISCARDED
    build.save(update_fields=["status", "updated_at"])
    audit.record("cost_test_build.discarded", obj=build, before={"id": build.data.get("id")}, actor=actor, organisation=build.organisation)


@transaction.atomic
def register(actor, build: CostTestBuild, *, notes: str = "") -> PackVersion:
    """CPUC only: the set as a draft pack, through `cost_tests.register_set` exactly as an uploaded file (the id-and-version rule and the
    engine-shipped-set rule apply unchanged). Refused while the set has problems."""
    if not is_manager(actor):
        raise AccessDenied("Only the CPUC's cost-test pack managers register a set for everyone.")
    build = CostTestBuild.objects.select_for_update().get(pk=build.pk)
    if build.status not in (Status.BUILDING, Status.OFFERED):
        raise AccessDenied(f"{build} is {build.get_status_display().lower()}.")
    build.problems, build.checked_by = check(build.data)
    if build.problems:
        build.save()
        raise BuildError("The set still has problems; register it once the Review step lists none.")
    try:
        pack = cost_tests.register_set(actor, to_yaml(build.data), source=f"set builder: {build.source}", notes=notes)
    except cost_tests.SetError as e:
        raise BuildError(str(e)) from None
    was_offered = build.status == Status.OFFERED
    build.status, build.pack = Status.REGISTERED, pack
    build.save()
    if was_offered:
        to = build.offered_by or build.owner
        transaction.on_commit(
            lambda: notifications.notify(to, f"The CPUC registered your cost-test set {build}", f"Registered as {pack}.", _link(build))
        )
    audit.record(
        "cost_test_build.registered",
        obj=build,
        after={"pack": pack.pk, "id": pack.label, "version": pack.version, "sha256": cost_tests.sha256(pack)},
        actor=actor,
        organisation=build.organisation,
    )
    return pack


# --- offering a set to the CPUC (Jake, 2026-11-03: a light mark with a note; the CPUC registers it or declines it with a reason)


def _link(build: CostTestBuild) -> str:
    from django.urls import reverse

    return reverse("refdata:build_step", args=[build.pk, "review"])


@transaction.atomic
def offer(actor, build: CostTestBuild, *, note: str = "") -> CostTestBuild:
    """Offer a set to the CPUC to register for everyone. It stays the offerer's: they may change it while it is offered (the managers
    see it as it is when they look) or withdraw the offer. Refused while it has problems."""
    _require(actor, build)
    build = CostTestBuild.objects.select_for_update().get(pk=build.pk)
    if build.status != Status.BUILDING:
        raise BuildError(f"{build} is already {build.get_status_display().lower()}.")
    build.problems, build.checked_by = check(build.data, exclude_pack=build.pack)
    if build.problems:
        build.save()
        raise BuildError("The set still has problems; offer it once the Review step lists none.")
    now = timezone.now()
    build.status, build.offered_at, build.offered_by, build.offer_note = Status.OFFERED, now, actor, note.strip()
    build.declined_at, build.declined_by, build.declined_reason = None, None, ""
    build.save()
    audit.record("cost_test_build.offered", obj=build, after={"id": build.data.get("id"), "note": build.offer_note}, actor=actor,
                 organisation=build.organisation)  # fmt: skip
    return build


@transaction.atomic
def withdraw_offer(actor, build: CostTestBuild) -> CostTestBuild:
    _require(actor, build)
    build = CostTestBuild.objects.select_for_update().get(pk=build.pk)
    if build.status != Status.OFFERED:
        raise BuildError(f"{build} is not offered to the CPUC.")
    build.status = Status.BUILDING
    build.save(update_fields=["status", "updated_at"])
    audit.record("cost_test_build.offer_withdrawn", obj=build, actor=actor, organisation=build.organisation)
    return build


@transaction.atomic
def decline(actor, build: CostTestBuild, *, reason: str) -> CostTestBuild:
    """CPUC only: decline an offered set, with the reason the offerer is sent. The set goes back to its builder, unchanged."""
    if not is_manager(actor):
        raise AccessDenied("Only the CPUC's cost-test pack managers decline an offered set.")
    if not reason.strip():
        raise BuildError("Say why the set is declined; the person who offered it is sent the reason.")
    build = CostTestBuild.objects.select_for_update().get(pk=build.pk)
    if build.status != Status.OFFERED:
        raise BuildError(f"{build} is not offered to the CPUC.")
    build.status, build.declined_at, build.declined_by, build.declined_reason = Status.BUILDING, timezone.now(), actor, reason.strip()
    build.save()
    audit.record(
        "cost_test_build.declined", obj=build, after={"reason": build.declined_reason}, actor=actor, organisation=build.organisation
    )
    to = build.offered_by or build.owner
    transaction.on_commit(
        lambda: notifications.notify(to, f"The CPUC declined your cost-test set {build}", build.declined_reason, _link(build))
    )
    return build


def words(build: CostTestBuild) -> str | None:
    """The set as the engine writes it out (`recet cost-tests show`), or None when the engine is missing or the set has problems."""
    e = engine()
    if e is None or build.problems:
        return None
    try:
        data = to_yaml(build.data)
        import hashlib

        return e.describe(e.parse(yaml.safe_load(data), hashlib.sha256(data).hexdigest(), file_name(build)))
    except e.SetError:
        return None
