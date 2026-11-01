"""Sandbox refresh (BR-11.2): production's published, non-PII data copied into the sandbox, so PAs test against real programs, packs,
rules and dashboards.

Two steps, each a management command, so the two environments never reach each other's database:
  production  `export_sandbox_bundle --to <folder or s3://...>` writes a bundle: manifest.json (when, from where, what, the SHA-256 of
              each file) and data.json (the rows below), plus each copied publication snapshot's results file;
  sandbox     `refresh_sandbox --from <same>` reads it and adds what the sandbox does not have yet.

What a bundle holds is the list SPECS, nothing else: organizations and their codes (PAs and others, not the CPUC), the program registry
(programs, versions, cycles, periods, confirmed allocation factors and goal sets), published and withdrawn reference-data packs with
their tie-outs and eTRM permutations (the files stay where production keeps them, read in place), the data dictionary's versions,
published value list versions and rule sets, and the publication snapshots in force with the published filing versions they belong to
(those versions without their uploaded files). Never people: no user, membership, grant, token, comment, notification or audit event,
and every "by" field is left blank. Never the PII vault, which the export cannot reach (it runs in the jobs task, given no vault URL)
and which no spec names (tests/test_sandbox_refresh.py checks both).

How a refresh adds rows. Each table's rows are found by a natural key (a program's PrgID, a rule set's code and effective date ...),
not by production's ids. A row the sandbox already has is matched and left as it is (versions never change), except the few fields
a spec lists as `refresh` (a program version's end date, a pack's withdrawal, an organization's name). A row it lacks is added
with its children (a rule set's rules, a pack's tie-outs), inside its own savepoint: if it clashes with something the sandbox made
itself (its own rule set for the same date, its own test filing for the same deadline) it is left out and listed, with the rows that
depend on it. Rows added are recorded (CopiedRecord) so the next refresh knows them, and the sandbox's 30-day clean-up of test filings
leaves copied filings alone. Published sets are added as drafts, given their rows, then published, as the database's guards require.
Then each dataset's release is rebuilt from the snapshots in force.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from django.apps import apps
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, models, transaction
from django.utils import timezone

FORMAT = 1


class BundleError(RuntimeError):
    """The bundle cannot be written or read; the message says why."""


# --- what is copied


@dataclass(frozen=True)
class Child:
    """Rows that belong to one parent row and are copied with it, and only with it (a parent the sandbox already has keeps its own)."""

    label: str
    parent: str  # the foreign key to the parent
    clear: tuple[str, ...] = ()
    children: tuple[Child, ...] = ()


@dataclass(frozen=True)
class Spec:
    label: str
    key: tuple[str, ...]  # natural key; a foreign key named here is matched through its target's key
    select: str = ""  # the name of a function below that gives production's rows; blank: all
    fks: dict[str, str] = field(default_factory=dict)  # foreign key -> target label (copied earlier in SPECS, or itself)
    optional: tuple[str, ...] = ()  # foreign keys left blank when their target was not copied (else the row is left out)
    clear: tuple[str, ...] = ()  # fields left blank (people, uploaded files)
    refresh: tuple[str, ...] = ()  # fields brought up to date on a row the sandbox already has
    draft: tuple[str, str] | None = None  # (status field, draft value): added as a draft, published once its children are in
    own_conflicts: bool = False  # a matching row the sandbox made itself is a clash, not a match (a tester's own filing)
    children: tuple[Child, ...] = ()
    files: bool = False  # the row's results file goes with it (publication snapshots)


SPECS = [
    Spec(
        "accounts.Organisation",
        ("code",),
        select="organisations",
        clear=("successor", "merged_on", "federated", "federation_provider"),
        refresh=("name", "pa_type"),
        children=(Child("accounts.OrganisationCode", "organisation", clear=("recorded_by",)),),
    ),
    Spec("registry.Program", ("prg_id",), fks={"administrator": "accounts.Organisation"}, refresh=("statewide", "portfolio_cost")),
    Spec(
        "registry.ProgramVersion",
        ("program", "valid_from"),
        fks={"program": "registry.Program"},
        clear=("created_by",),
        refresh=("valid_to",),
    ),
    Spec("registry.FilingCycle", ("first_year",)),
    Spec("registry.ReportingPeriod", ("kind", "year", "number")),
    Spec(
        "registry.AllocationVersion",
        ("label",),
        select="confirmed",
        fks={"filing_cycle": "registry.FilingCycle"},
        optional=("filing_cycle",),
        clear=("created_by", "confirmed_by"),
        children=(Child("registry.AllocationEntry", "version", children=(Child("registry.AllocationShare", "entry"),)),),
    ),
    Spec(
        "registry.GoalSet",
        ("decision",),
        select="confirmed",
        clear=("created_by", "confirmed_by"),
        children=(Child("registry.Goal", "goal_set"), Child("registry.GoalCountingRule", "goal_set")),
    ),
    Spec(
        "refdata.PackVersion",
        ("kind", "label", "version"),
        select="not_draft",
        clear=("registered_by", "published_by"),
        refresh=("status", "withdrawn_reason"),
        children=(
            Child("refdata.TieOut", "pack", clear=("recorded_by",)),
            Child("refdata.EtrmPermutation", "pack"),
            Child("refdata.EtrmPackage", "pack"),
        ),
    ),
    Spec(
        "dictionary.CatalogVersion",
        ("label",),
        fks={"based_on": "dictionary.CatalogVersion"},
        optional=("based_on",),
        clear=("loaded_by",),
        children=(Child("dictionary.Field", "version", children=(Child("dictionary.Alias", "field"), Child("dictionary.Usage", "field"))),),
    ),
    Spec("dictionary.ValueList", ("name",)),
    Spec(
        "dictionary.ValueListVersion",
        ("value_list", "number"),
        select="published",
        fks={"value_list": "dictionary.ValueList"},
        clear=("created_by", "published_by"),
        draft=("status", "draft"),
        children=(Child("dictionary.Code", "version"),),
    ),
    Spec(
        "rules.RuleSet",
        ("code", "effective_from"),
        select="published",
        fks={"based_on": "rules.RuleSet"},
        optional=("based_on",),
        clear=("created_by", "published_by"),
        draft=("status", "draft"),
        children=(Child("rules.Rule", "rule_set"),),
    ),
    Spec(
        "submissions.Deadline",
        ("module", "kind", "period", "cycle"),
        select="snapshot_deadlines",
        fks={"period": "registry.ReportingPeriod", "cycle": "registry.FilingCycle"},
    ),
    Spec(
        "submissions.Submission",
        ("deadline", "organisation"),
        select="snapshot_submissions",
        fks={"deadline": "submissions.Deadline", "organisation": "accounts.Organisation"},
        own_conflicts=True,
    ),
    Spec(
        "submissions.SubmissionVersion",
        ("submission", "number"),
        select="snapshot_versions",
        fks={"submission": "submissions.Submission", "rule_set": "rules.RuleSet"},
        optional=("rule_set",),
        clear=("files", "calculation", "created_by", "confirmed_by"),
    ),
    Spec(
        "snapshots.Snapshot",
        ("id",),
        select="snapshots_in_force",
        fks={"version": "submissions.SubmissionVersion", "organisation": "accounts.Organisation"},
        children=(Child("snapshots.PIIScan", "snapshot", clear=("released_by",)),),
        files=True,
    ),
]


# --- production's rows (Spec.select)


def _organisations(qs):
    return qs.exclude(kind="cpuc")


def _confirmed(qs):
    return qs.filter(status="confirmed")


def _published(qs):
    return qs.filter(status="published")


def _not_draft(qs):
    return qs.exclude(status="draft")


def _standing():
    from cedars.services.snapshots import service as snapshots
    from cedars.services.snapshots.models import Dataset

    return [s for d in Dataset.values for s in snapshots.in_force(d)]


def _snapshots_in_force(qs):
    return qs.filter(pk__in=[s.pk for s in _standing()])


def _snapshot_versions(qs):
    return qs.filter(snapshot__in=_snapshots_in_force(apps.get_model("snapshots.Snapshot").objects.all()))


def _snapshot_submissions(qs):
    return qs.filter(versions__in=_snapshot_versions(apps.get_model("submissions.SubmissionVersion").objects.all())).distinct()


def _snapshot_deadlines(qs):
    return qs.filter(submissions__in=_snapshot_submissions(apps.get_model("submissions.Submission").objects.all())).distinct()


SELECT = {
    "organisations": _organisations,
    "confirmed": _confirmed,
    "published": _published,
    "not_draft": _not_draft,
    "snapshots_in_force": _snapshots_in_force,
    "snapshot_versions": _snapshot_versions,
    "snapshot_submissions": _snapshot_submissions,
    "snapshot_deadlines": _snapshot_deadlines,
}


def all_specs():
    """Every spec and child, as (label, model, the spec or child)."""

    def walk(children):
        for c in children:
            yield c.label, apps.get_model(c.label), c
            yield from walk(c.children)

    for s in SPECS:
        yield s.label, apps.get_model(s.label), s
        yield from walk(s.children)


def check_specs() -> list[str]:
    """Problems in SPECS: a foreign key no spec says how to copy, or a model of the PII vault or of people. Empty when sound."""
    problems = []
    forbidden = {"vault", "audit", "notifications"}
    people = {"accounts.user", "accounts.membership", "accounts.grant", "accounts.apitoken", "accounts.accessrequest"}
    for label, model, spec in all_specs():
        if model._meta.app_label in forbidden or model._meta.label_lower in people:
            problems.append(f"{label} is never copied to the sandbox")
        parent = getattr(spec, "parent", None)
        handled = set(spec.clear) | set(getattr(spec, "fks", {})) | ({parent} if parent else set())
        for f in model._meta.concrete_fields:
            if f.is_relation and f.name not in handled:
                problems.append(f"{label}.{f.name} is a foreign key with no rule (fks, parent or clear)")
            if f.is_relation and f.related_model._meta.label == settings.AUTH_USER_MODEL and f.name not in spec.clear:
                problems.append(f"{label}.{f.name} names a person and must be cleared")
    return problems


# --- rows as JSON


def _value(f, obj):
    return f.value_from_object(obj)


def _row(model, obj, spec, *, parent: str = "", keys: dict) -> dict:
    """One row: its own fields, its foreign keys as their targets' natural keys, blanks where the spec clears."""
    fields, fks = {}, {}
    for f in model._meta.concrete_fields:
        if f.primary_key and f.auto_created or f.name == parent:
            continue
        if f.name in spec.clear:
            continue
        if f.is_relation:
            target = getattr(obj, f.attname)
            fks[f.name] = None if target is None else keys[spec.fks[f.name]].get(target)
            continue
        fields[f.name] = _value(f, obj)
    return {"fields": fields, "fks": fks}


def _natural_key(spec: Spec, row: dict) -> list:
    return [row["fks"][k] if k in spec.fks else row["fields"][k] for k in spec.key]


def key_text(key) -> str:
    return json.dumps(key, cls=DjangoJSONEncoder, sort_keys=True)


# --- where a bundle lives: a folder, or s3://bucket/prefix


class Place:
    def __init__(self, where: str):
        self.where = where
        u = urlparse(where)
        self.s3 = u.scheme == "s3"
        if self.s3:
            self.bucket, self.prefix = u.netloc, u.path.strip("/")
        else:
            self.root = Path(u.path if u.scheme == "file" else where)

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def _client(self):
        import boto3

        return boto3.client("s3")

    def write(self, name: str, src: Path) -> None:
        if self.s3:
            self._client().upload_file(str(src), self.bucket, self._key(name))
        else:
            dest = self.root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes()) if src.stat().st_size < (64 << 20) else _copy(src, dest)

    def read(self, name: str, dest: Path) -> None:
        try:
            if self.s3:
                self._client().download_file(self.bucket, self._key(name), str(dest))
            else:
                _copy(self.root / name, dest)
        except Exception as e:
            raise BundleError(f"{name} could not be read from {self.where}: {e}") from e


def _copy(src: Path, dest: Path) -> None:
    import shutil

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


# --- export (production)


def export(where: str) -> dict:
    """Write production's bundle to a folder or s3://bucket/prefix. Returns the manifest."""
    from cedars import __version__
    from cedars.core import storage

    if settings.ENVIRONMENT == "sandbox":
        raise BundleError("A bundle is made in production for the sandbox, not in the sandbox itself.")
    place = Place(where)
    keys: dict[str, dict] = {}  # label -> {production pk: natural key}
    data: dict[str, list] = {}
    files: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for spec in SPECS:
            model = apps.get_model(spec.label)
            qs = model.objects.all()
            if spec.select:
                qs = SELECT[spec.select](qs)
            keys.setdefault(spec.label, {})
            rows = []
            for obj in qs.order_by("pk"):
                row = _row(model, obj, spec, keys=keys)
                key = _natural_key(spec, row)
                keys[spec.label][obj.pk] = key
                row["key"] = key
                row["children"] = _children(spec.children, obj)
                if spec.files:
                    name = f"files/{obj.pk}.parquet"
                    local = tmp / name
                    local.parent.mkdir(parents=True, exist_ok=True)
                    with storage.store("snapshots").open(obj.key) as src, open(local, "wb") as dst:
                        while block := src.read(1 << 22):
                            dst.write(block)
                    place.write(name, local)
                    files[name] = {"sha256": _sha256(local), "bytes": local.stat().st_size}
                    local.unlink()
                rows.append(row)
            data[spec.label] = rows
        body = tmp / "data.json.gz"
        with gzip.open(body, "wt", encoding="utf-8") as f:
            json.dump(data, f, cls=DjangoJSONEncoder)
        place.write("data.json.gz", body)
        files["data.json.gz"] = {"sha256": _sha256(body), "bytes": body.stat().st_size}
        manifest = {
            "format": FORMAT,
            "made_at": timezone.now().isoformat(),
            "environment": settings.ENVIRONMENT,
            "platform": __version__,
            "tables": {label: len(rows) for label, rows in data.items()},
            "files": files,
        }
        m = tmp / "manifest.json"
        m.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        place.write("manifest.json", m)
    return manifest


def _children(children: tuple[Child, ...], parent_obj) -> dict:
    out = {}
    for c in children:
        model = apps.get_model(c.label)
        rows = []
        for obj in model.objects.filter(**{c.parent: parent_obj}).order_by("pk"):
            row = _row(model, obj, c, parent=c.parent, keys={})
            row.pop("fks")
            row["children"] = _children(c.children, obj)
            rows.append(row)
        out[c.label] = rows
    return out


# --- refresh (the sandbox)


@dataclass
class Tally:
    counts: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)

    def add(self, label: str, what: str, n: int = 1) -> None:
        c = self.counts.setdefault(label, {"added": 0, "matched": 0, "updated": 0, "skipped": 0})
        c[what] += n

    def skip(self, label: str, key, why: str) -> None:
        self.add(label, "skipped")
        if len(self.skipped) < 500:
            self.skipped.append({"table": label, "key": key_text(key), "why": why})


@contextmanager
def _keep_timestamps(model):
    """Add rows with production's own dates: auto_now / auto_now_add would stamp them with the refresh's."""
    changed = []
    for f in model._meta.concrete_fields:
        if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False):
            changed.append((f, f.auto_now, f.auto_now_add))
            f.auto_now = f.auto_now_add = False
    try:
        yield
    finally:
        for f, now, add in changed:
            f.auto_now, f.auto_now_add = now, add


def _python(model, fields: dict) -> dict:
    out = {}
    for name, value in fields.items():
        f = model._meta.get_field(name)
        if value is not None and isinstance(f, (models.DateTimeField, models.DateField, models.DecimalField, models.UUIDField)):
            value = f.to_python(value)
            if isinstance(value, datetime) and timezone.is_naive(value):
                value = timezone.make_aware(value)
        out[name] = value
    return out


def _cleared(model, spec) -> dict:
    """Blank values for the fields a spec clears (a person, an uploaded file list, a merge)."""
    out = {}
    for name in spec.clear:
        f = model._meta.get_field(name)
        if f.is_relation:
            out[f.attname] = None
        elif isinstance(f, models.JSONField):
            out[name] = f.get_default()
        elif isinstance(f, models.BooleanField):
            out[name] = False
        else:
            out[name] = None if f.null else ""
    return out


def _add_children(children: tuple[Child, ...], parent_pk, rows_by_label: dict, tally: Tally) -> None:
    for c in children:
        model = apps.get_model(c.label)
        rows = rows_by_label.get(c.label, [])
        if not rows:
            continue
        parent_attname = model._meta.get_field(c.parent).attname
        objs = [model(**{**_python(model, r["fields"]), **_cleared(model, c), parent_attname: parent_pk}) for r in rows]
        with _keep_timestamps(model):
            made = model.objects.bulk_create(objs, batch_size=2000)
        tally.add(c.label, "added", len(made))
        if c.children:
            for obj, r in zip(made, rows, strict=True):
                _add_children(c.children, obj.pk, r["children"], tally)


def _find(model, spec: Spec, lookup: dict, row: dict):
    if spec.label == "accounts.Organisation":
        codes = [row["fields"]["code"], *[c["fields"]["code"] for c in row["children"].get("accounts.OrganisationCode", [])]]
        return model.objects.filter(models.Q(code=row["fields"]["code"]) | models.Q(codes__code__in=codes)).distinct().first()
    return model.objects.filter(**lookup).first()


def load(where: str, *, actor=None):
    """Refresh the sandbox from a bundle. Returns the Refresh record."""
    from cedars.services import audit

    from .models import Refresh

    if settings.ENVIRONMENT not in ("sandbox", "dev", "test"):
        raise BundleError(f"A refresh loads production's data into the sandbox; this is {settings.ENVIRONMENT}, so nothing was loaded.")
    place = Place(where)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        place.read("manifest.json", tmp / "manifest.json")
        raw = (tmp / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if manifest.get("format") != FORMAT:
            raise BundleError(f"The bundle is format {manifest.get('format')}; this platform reads format {FORMAT}.")
        if manifest.get("environment") == "sandbox":
            raise BundleError("The bundle was made in a sandbox; a refresh takes production's.")
        place.read("data.json.gz", tmp / "data.json.gz")
        _verify(tmp / "data.json.gz", manifest, "data.json.gz")
        with gzip.open(tmp / "data.json.gz", "rt", encoding="utf-8") as f:
            data = json.load(f)
        made_at = manifest.get("made_at")
        refresh = Refresh.objects.create(
            source=where[:500],
            bundle_made_at=datetime.fromisoformat(made_at) if made_at else None,
            bundle_environment=str(manifest.get("environment", ""))[:20],
            bundle_platform=str(manifest.get("platform", ""))[:40],
            bundle_sha256=hashlib.sha256(raw).hexdigest(),
        )
        tally = Tally()
        try:
            datasets = _load(data, place, manifest, tmp, refresh, tally)
        except Exception as e:
            refresh.status, refresh.error, refresh.finished_at = Refresh.Status.FAILED, str(e)[:5000], timezone.now()
            refresh.counts, refresh.skipped = tally.counts, tally.skipped
            refresh.save()
            audit.record("sandbox.refresh_failed", obj=refresh, reason=str(e)[:500], actor=actor)
            raise
    from cedars.services.snapshots import service as snapshots

    for dataset in sorted(datasets):
        snapshots.rebuild(dataset, "Sandbox refreshed from production")
    refresh.status, refresh.finished_at = Refresh.Status.DONE, timezone.now()
    refresh.counts, refresh.skipped = tally.counts, tally.skipped
    refresh.save()
    added = sum(c["added"] for c in tally.counts.values())
    audit.record(
        "sandbox.refreshed",
        obj=refresh,
        after={"bundle_made_at": made_at, "added": added, "skipped": sum(c["skipped"] for c in tally.counts.values())},
        actor=actor,
    )
    return refresh


def _verify(path: Path, manifest: dict, name: str) -> None:
    want = (manifest.get("files") or {}).get(name, {}).get("sha256")
    if not want or _sha256(path) != want:
        raise BundleError(f"{name} does not match the bundle's manifest; the bundle is incomplete or was changed.")


@transaction.atomic
def _load(data: dict, place: Place, manifest: dict, tmp: Path, refresh, tally: Tally) -> set[str]:
    from cedars.core import storage

    from .models import CopiedRecord

    copied = {(c.table, c.key): c.local_id for c in CopiedRecord.objects.all()}
    local: dict[str, dict[str, object]] = {}  # label -> {key text: sandbox pk}
    datasets = set()
    for spec in SPECS:
        model = apps.get_model(spec.label)
        table = model._meta.label_lower
        local.setdefault(spec.label, {})
        for row in data.get(spec.label, []):
            key = row["key"]
            ktext = key_text(key)
            fk_values, missing = {}, ""
            for name, target_key in row["fks"].items():
                f = model._meta.get_field(name)
                if target_key is None:
                    fk_values[f.attname] = None
                    continue
                pk = local[spec.fks[name]].get(key_text(target_key))
                if pk is None and name not in spec.optional:
                    missing = f"its {f.verbose_name} ({key_text(target_key)}) was not copied"
                    break
                fk_values[f.attname] = pk
            if missing:
                tally.skip(spec.label, key, missing)
                continue
            lookup = {}
            for k in spec.key:
                f = model._meta.get_field(k)
                if f.is_relation:
                    lookup[f.attname] = fk_values[f.attname]
                else:
                    lookup[k] = _python(model, {k: row["fields"][k]})[k]
            existing = _find(model, spec, lookup, row)
            if existing is not None:
                if spec.own_conflicts and (table, ktext) not in copied:
                    tally.skip(spec.label, key, "the sandbox has its own record for it (a tester's), which is kept")
                    continue
                status = row["fields"].get("status")
                if status is not None and "status" not in spec.refresh and getattr(existing, "status", status) != status:
                    tally.skip(spec.label, key, f"the sandbox has its own {existing.status} version under the same key, which is kept")
                    continue
                local[spec.label][ktext] = existing.pk
                wanted = _python(model, {k: row["fields"][k] for k in spec.refresh})
                changes = {k: v for k, v in wanted.items() if getattr(existing, k) != v}
                if changes:
                    try:
                        with transaction.atomic():
                            model.objects.filter(pk=existing.pk).update(**changes)
                        tally.add(spec.label, "updated")
                    except DatabaseError as e:
                        tally.skip(spec.label, key, f"could not be brought up to date: {_short(e)}")
                else:
                    tally.add(spec.label, "matched")
                continue
            values = {**_python(model, row["fields"]), **fk_values, **_cleared(model, spec)}
            final = None
            if spec.draft:
                status_field, draft_value = spec.draft
                final, values[status_field] = values[status_field], draft_value
            try:
                with transaction.atomic(), _keep_timestamps(model):
                    obj = model.objects.create(**values)
                    _add_children(spec.children, obj.pk, row["children"], tally)
                    if spec.draft:
                        model.objects.filter(pk=obj.pk).update(**{spec.draft[0]: final})
                    if spec.files:
                        name = f"files/{key[0]}.parquet"
                        path = tmp / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        place.read(name, path)
                        _verify(path, manifest, name)
                        storage.store("snapshots").put_file(obj.key, path)
                        path.unlink()
                        datasets.add(obj.dataset)
            except DatabaseError as e:
                tally.skip(spec.label, key, f"clashes with the sandbox's own records: {_short(e)}")
                continue
            local[spec.label][ktext] = obj.pk
            CopiedRecord.objects.update_or_create(table=table, key=ktext, defaults={"local_id": str(obj.pk), "refresh": refresh})
            copied[(table, ktext)] = str(obj.pk)
            tally.add(spec.label, "added")
    return datasets


def _short(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__


def is_copied(table: str, local_ids) -> set[str]:
    """Which of these sandbox rows a refresh copied from production (the clean-up of test filings leaves them alone)."""
    from .models import CopiedRecord

    return set(CopiedRecord.objects.filter(table=table, local_id__in=[str(i) for i in local_ids]).values_list("local_id", flat=True))
