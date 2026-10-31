"""Versioned data packs (the reference-data service). Modules and workers register packs through these functions, runs and submissions
record `pin()` of the packs they used, and a filing never uses a draft or withdrawn pack."""

import hashlib
import json
from datetime import date
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import PackVersion, TieOut, TieOutRequirement

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


def outstanding(pack: PackVersion) -> list[TieOutRequirement]:
    """The required checks whose latest result for this pack is missing or failed."""
    latest = {}
    for t in pack.tie_outs.order_by("recorded_at", "id"):
        latest[t.name] = t.passed
    return [r for r in TieOutRequirement.objects.filter(kind=pack.kind).order_by("name") if not latest.get(r.name)]


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
        raise AccessDenied("Publishing needs a passed tie-out for: " + ", ".join(r.name for r in missing) + ".")
    pack.status, pack.published_by, pack.published_at = Status.PUBLISHED, actor, timezone.now()
    pack.save()
    audit.record("pack.published", obj=pack, after={"fingerprint": pack.fingerprint}, actor=actor)
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
def discard_draft(actor, pack: PackVersion, reason: str = "") -> None:
    pack = PackVersion.objects.select_for_update().get(pk=pack.pk)
    _require(actor, pack.kind)
    if pack.status != Status.DRAFT:
        raise AccessDenied("A published pack is kept for good; withdraw it instead.")
    audit.record("pack.discarded", obj=pack, before=audit.snapshot(pack, IDENTITY[:3] + ["fingerprint"]), reason=reason, actor=actor)
    pack.tie_outs.all().delete()
    pack.delete()


# --- choosing and pinning packs


def usable(pack: PackVersion, purpose: str) -> tuple[bool, str]:
    """Whether a new run may use the pack: "filing" takes only published packs, "analysis" drafts too (BR-11.8)."""
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
