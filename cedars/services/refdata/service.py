"""Versioned data packs (the reference-data service). Modules and workers register packs through these functions, runs and submissions
record `pin()` of the packs they used, and a filing never uses a draft or withdrawn pack."""

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import PackVersion, TieOut, TieOutRequirement, check_words

Kind, Status = PackVersion.Kind, PackVersion.Status

# kinds a module produces: that module's CPUC administrators manage them as well as the system administrators
MODULE_OF = {Kind.IMPACT_PROFILES: "impact_profiles", Kind.ETRM_SNAPSHOT: "impact_profiles"}
IDENTITY = ["kind", "label", "version", "manifest", "fingerprint", "storage_uri"]


def can_manage(actor, kind: str) -> bool:
    """None is a system job (a sync or a worker), recorded as "system" in the audit trail."""
    if actor is None or access.is_system_admin(actor):
        return True
    module, cpuc = MODULE_OF.get(kind), access.cpuc()
    return bool(module and cpuc and access.has_permission(actor, cpuc, f"{module}.admin"))


def _require(actor, kind: str) -> None:
    if not can_manage(actor, kind):
        raise AccessDenied("Only a CPUC system administrator (or the producing module's CPUC administrator) manages these data packs.")


def fingerprint(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def manifest_for(root: Path) -> dict:
    """{relative path: {sha256, bytes}} for every file under a folder (or one file), read in 1 MB blocks."""
    root = Path(root)
    paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    out = {}
    for p in paths:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        out[p.name if root.is_file() else p.relative_to(root).as_posix()] = {"sha256": h.hexdigest(), "bytes": p.stat().st_size}
    return out


def verify(pack: PackVersion, root: Path) -> list[str]:
    """Problems between a pack's manifest and the files under `root` (a mounted copy of storage_uri): missing, changed or extra."""
    found = manifest_for(root)
    problems = [f"{p} is missing" for p in pack.manifest if p not in found]
    problems += [f"{p} has changed (SHA-256 differs)" for p, m in pack.manifest.items() if p in found and found[p]["sha256"] != m["sha256"]]
    problems += [f"{p} is not in the manifest" for p in found if p not in pack.manifest]
    return problems


# --- registering and accepting packs


@transaction.atomic
def register_pack(
    actor,
    *,
    kind: str,
    label: str,
    manifest: dict,
    storage_uri: str,
    version: str = "",
    acc_year: int | None = None,
    valid_from: date | None = None,
    source: str = "",
    notes: str = "",
) -> PackVersion:
    """A new draft pack version. Drafts are usable for analysis runs at once and for filings once published."""
    _require(actor, kind)
    if kind not in Kind.values:
        raise AccessDenied(f"{kind} is not a kind of data pack.")
    if not manifest or any("sha256" not in m for m in manifest.values()):
        raise AccessDenied("A pack needs a manifest giving every file's SHA-256.")
    if PackVersion.objects.filter(kind=kind, label=label, version=version).exists():
        raise AccessDenied(f"{label} {version} is already registered; give the new data its own version.")
    pack = PackVersion.objects.create(
        kind=kind,
        label=label,
        version=version,
        acc_year=acc_year,
        valid_from=valid_from,
        manifest=manifest,
        fingerprint=fingerprint(manifest),
        storage_uri=storage_uri,
        source=source,
        notes=notes,
        registered_by=actor,
    )
    audit.record(
        "pack.registered",
        obj=pack,
        after={**audit.snapshot(pack, ["kind", "label", "version", "acc_year", "fingerprint", "storage_uri"]), "files": pack.files},
        actor=actor,
    )
    return pack


@transaction.atomic
def record_tie_out(
    actor,
    pack: PackVersion,
    *,
    check: str,
    passed: bool,
    summary: str = "",
    precision: float | None = None,
    largest_difference: float | None = None,
    engine_release: str = "",
    report: dict | None = None,
    report_uri: str = "",
) -> TieOut:
    """Keep the result of an acceptance check (a reconciliation script or the reference calculation writes it). A difference above the
    stated precision is a failure whatever `passed` says."""
    _require(actor, pack.kind)
    if precision is not None and largest_difference is not None and abs(largest_difference) > precision:
        passed = False
    t = TieOut.objects.create(
        pack=pack,
        name=check,
        passed=passed,
        summary=summary,
        precision=precision,
        largest_difference=largest_difference,
        engine_release=engine_release,
        report=report or {},
        report_uri=report_uri,
        recorded_by=actor,
    )
    audit.record(
        "pack.tie_out",
        obj=pack,
        after={"check": check, "passed": passed, "precision": precision, "largest_difference": largest_difference},
        actor=actor,
    )
    return t


def required_checks(pack: PackVersion) -> list[TieOutRequirement]:
    """The checks a pack must pass: those in force for its kind while it is a draft; once published, the ones it was published under
    (a check added or retired later does not change a published pack)."""
    qs = TieOutRequirement.objects.filter(kind=pack.kind).order_by("name")
    if pack.checks_at_publication is not None:
        return list(qs.filter(name__in=pack.checks_at_publication))
    return list(qs.filter(retired_at__isnull=True))


def outstanding(pack: PackVersion) -> list[TieOutRequirement]:
    """The required checks whose latest result for this pack is missing or failed."""
    latest = {}
    for t in pack.tie_outs.order_by("recorded_at", "id"):
        latest[t.name] = t.passed
    return [r for r in required_checks(pack) if not latest.get(r.name)]


# --- the acceptance checks each kind of pack needs (CPUC system administrators)

CHECK_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def can_manage_checks(actor) -> bool:
    return actor is None or access.is_system_admin(actor)


def _require_checks_admin(actor) -> None:
    if not can_manage_checks(actor):
        raise AccessDenied("Only a CPUC system administrator adds or retires acceptance checks.")


@transaction.atomic
def add_requirement(actor, *, kind: str, name: str, description: str, reason: str) -> TieOutRequirement:
    """A new check drafts of that kind must pass before they are published. Packs already published keep the checks they had."""
    _require_checks_admin(actor)
    name, description, reason = name.strip(), description.strip(), reason.strip()
    if kind not in Kind.values:
        raise AccessDenied(f"{kind} is not a kind of data pack.")
    if not CHECK_NAME.match(name):
        raise AccessDenied(
            "A check's name is 2 to 40 lower-case letters, digits and underscores, starting with a letter (e.g. source_workbook)."
        )
    if not description:
        raise AccessDenied("Say what the check shows.")
    if not reason:
        raise AccessDenied("Give the reason for adding the check.")
    existing = TieOutRequirement.objects.filter(kind=kind, name=name).first()
    if existing is not None:
        raise AccessDenied(
            f"{Kind(kind).label} already has a check named {name}"
            + (" (retired: a retired check is never brought back, so give the new one its own name)." if existing.retired_at else ".")
        )
    r = TieOutRequirement.objects.create(
        kind=kind, name=name, description=description, reason=reason, added_at=timezone.now(), added_by=actor
    )
    audit.record("pack_check.added", obj=r, reason=reason, after={"kind": kind, "name": name, "description": description}, actor=actor)
    return r


@transaction.atomic
def retire_requirement(actor, requirement: TieOutRequirement, reason: str) -> TieOutRequirement:
    """Drafts stop needing the check; published packs keep it among the checks they were published under, with their results."""
    _require_checks_admin(actor)
    requirement = TieOutRequirement.objects.select_for_update().get(pk=requirement.pk)
    if requirement.retired_at is not None:
        raise AccessDenied("That check is already retired.")
    if not reason.strip():
        raise AccessDenied("Give the reason for retiring the check.")
    requirement.retired_at, requirement.retired_by, requirement.retired_reason = timezone.now(), actor, reason.strip()
    requirement.save(update_fields=["retired_at", "retired_by", "retired_reason"])
    audit.record("pack_check.retired", obj=requirement, reason=reason.strip(), actor=actor)
    return requirement


# --- recording a check's result from its report (the pack's page)

REPORT_MAX_BYTES = 5 << 20  # a report is a few kilobytes to a few hundred; the upload limit still applies first


def _number(value) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def read_report(data: bytes) -> dict:
    """What a check's JSON report says, for record_tie_out: passed, summary, precision, largest difference, engine release.

    The report says whether it passed as `passed` (true or false) or `status` ("PASS" or "FAIL", as the engine's `recet data tie-out`
    writes it). Optional: `summary`, `precision`, `largest_difference`, `engine_release`; a report made of `parts` (each with a name and
    a status) is summed up part by part, and its largest `relative_difference` among the parts' values is the largest difference."""
    if len(data) > REPORT_MAX_BYTES:
        raise AccessDenied(f"A check's report is a JSON file of a few hundred kilobytes at most; this one is {len(data) / 1048576:.1f} MB.")
    try:
        report = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AccessDenied(f"The report is not a JSON file ({e}).") from None
    if not isinstance(report, dict):
        raise AccessDenied("The report must be a JSON object.")
    if isinstance(report.get("passed"), bool):
        passed = report["passed"]
    elif str(report.get("status", "")).upper() in ("PASS", "FAIL"):
        passed = str(report["status"]).upper() == "PASS"
    else:
        raise AccessDenied('The report does not say whether the check passed: give "passed": true or false, or "status": "PASS" or "FAIL".')
    parts = [p for p in report.get("parts") or [] if isinstance(p, dict)]
    summary = str(report.get("summary") or "").strip()
    if not summary and parts:
        summary = "; ".join(f"{p.get('name', 'part')}: {p.get('status', '?')}" for p in parts)
    largest = _number(report.get("largest_difference"))
    if largest is None:
        diffs = [_number(v.get("relative_difference")) for p in parts for v in p.get("values") or [] if isinstance(v, dict)]
        diffs = [abs(d) for d in diffs if d is not None]
        largest = max(diffs) if diffs else None
    return {
        "passed": passed,
        "summary": summary[:2000],
        "precision": _number(report.get("precision")),
        "largest_difference": largest,
        "engine_release": str(report.get("engine_release") or "")[:60],
        "report": report,
    }


def record_report(actor, pack: PackVersion, *, check: str, data: bytes, file_name: str = "") -> TieOut:
    """Record a check's result from its JSON report: the check must be one this pack needs, and a report naming a file's SHA-256
    must name one of the pack's files."""
    _require(actor, pack.kind)
    if check not in {r.name for r in required_checks(pack)}:
        raise AccessDenied(f"{check_words(check)} is not a check this pack needs.")
    found = read_report(data)
    sha = found["report"].get("sha256")
    if isinstance(sha, str) and sha and sha not in {m.get("sha256") for m in pack.manifest.values()}:
        raise AccessDenied("The report is about a file that is not in this pack (its SHA-256 matches none of the pack's files).")
    found["report"] = {**found["report"], "_file": file_name} if file_name else found["report"]
    return record_tie_out(actor, pack, check=check, **found)


@transaction.atomic
def publish_pack(actor, pack: PackVersion) -> PackVersion:
    pack = PackVersion.objects.select_for_update().get(pk=pack.pk)
    _require(actor, pack.kind)
    if pack.status != Status.DRAFT:
        raise AccessDenied("Only a draft is published.")
    if pack.kind == Kind.COST_TESTS and (pack.manifest.get("definition.yaml") or {}).get("set", {}).get("draft"):
        raise AccessDenied("The set's own file says draft: true. A published set is a final one: register the final file as a new version.")
    missing = outstanding(pack)
    if missing:
        raise AccessDenied("Publishing needs a passed result of these checks: " + ", ".join(r.words for r in missing) + ".")
    checks = [r.name for r in required_checks(pack)]
    pack.status, pack.published_by, pack.published_at, pack.checks_at_publication = Status.PUBLISHED, actor, timezone.now(), checks
    pack.save()
    audit.record("pack.published", obj=pack, after={"fingerprint": pack.fingerprint, "checks": checks}, actor=actor)
    return pack


@transaction.atomic
def withdraw_pack(actor, pack: PackVersion, reason: str) -> PackVersion:
    """No new run uses a withdrawn pack; runs and filings that used it still name it, and it stays for reproducing them."""
    pack = PackVersion.objects.select_for_update().get(pk=pack.pk)
    _require(actor, pack.kind)
    if pack.status != Status.PUBLISHED:
        raise AccessDenied("Only a published pack is withdrawn (discard a draft instead).")
    if not reason.strip():
        raise AccessDenied("Give the reason for withdrawing it.")
    pack.status, pack.withdrawn_reason = Status.WITHDRAWN, reason
    pack.save()
    audit.record("pack.withdrawn", obj=pack, reason=reason, actor=actor)
    return pack


@transaction.atomic
def discard_draft(actor, pack: PackVersion, reason: str) -> None:
    pack = PackVersion.objects.select_for_update().get(pk=pack.pk)
    _require(actor, pack.kind)
    if pack.status != Status.DRAFT:
        raise AccessDenied("A published pack is kept for good; withdraw it instead.")
    if not reason.strip():
        raise AccessDenied("Give the reason for discarding the draft.")
    audit.record("pack.discarded", obj=pack, before=audit.snapshot(pack, IDENTITY[:3] + ["fingerprint"]), reason=reason, actor=actor)
    pack.tie_outs.all().delete()
    pack.delete()


# --- choosing and pinning packs


def usable(pack: PackVersion, purpose: str) -> tuple[bool, str]:
    """Whether a new run may use the pack: "filing" takes only published packs, "analysis" drafts too."""
    if pack.status == Status.WITHDRAWN:
        return False, f"{pack} was withdrawn: {pack.withdrawn_reason}"
    if pack.status == Status.DRAFT and purpose == "filing":
        return False, f"{pack} is a draft: it can be used for analysis runs, not filings."
    return True, ""


def in_force(kind: str, on: date | None = None, *, include_drafts: bool = False) -> PackVersion | None:
    """The newest published pack of a kind in force on a date (drafts too if asked; an undated pack counts from its registration)."""
    on = on or timezone.localdate()
    statuses = [Status.PUBLISHED, Status.DRAFT] if include_drafts else [Status.PUBLISHED]
    packs = PackVersion.objects.filter(kind=kind, status__in=statuses)
    dated = packs.filter(valid_from__lte=on).order_by("-valid_from", "-registered_at").first()
    return dated or packs.filter(valid_from__isnull=True).order_by("-registered_at").first()


def pin(packs) -> dict:
    """What a run or a submission records about the packs it used, keyed by kind: enough to fetch exactly the same files again."""
    return {p.kind: {"id": p.pk, "label": p.label, "version": p.version, "fingerprint": p.fingerprint, "status": p.status} for p in packs}
