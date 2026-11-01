"""The document accessibility check of uploads (BRD v0.5 BR-10.3: published documents meet WCAG 2.2 AA).

Every PDF and workbook stored as a document's file, or attached to a comment on a document or on a filing, is checked as it is stored
(checks.check): an untagged or image-only PDF, a PDF with no title or no language, a sheet with no marked header row, merged cells,
pictures or charts with no alternative text, default sheet names. What it finds is kept (FileCheck), the uploader is warned on the
page they land on, and the file is listed on the Document accessibility page (CPUC system administrators), where it can be marked
reviewed with a note. Nothing is refused unless the CPUC's settings say so (CheckSettings.block, with the findings that block); a file
brought over from a legacy site is checked and listed but never refused.

Callers: `inspect(path, name)` with the file on local disk before it is stored (raises Refused when the settings refuse it), then
`record(...)` once the stored file has its id; `upload_warning(source, refs)` is the sentence for the page."""

from __future__ import annotations

from pathlib import Path

from django.db import transaction
from django.utils import timezone

from cedars.core.files import FileRefused
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import checks
from .models import CheckSettings, FileCheck

# what refuses an upload when the CPUC turns blocking on, unless it chooses otherwise: the findings BR-10.3's audit treats as critical
DEFAULT_BLOCKING = ["pdf_untagged", "pdf_image_only", "pdf_no_title", "pdf_no_language", "workbook_no_headers"]


class Refused(FileRefused):
    """The settings refuse this file; str(e) is the sentence the page shows."""


def settings_row() -> CheckSettings:
    return CheckSettings.objects.get_or_create(pk=1, defaults={"block": False, "blocking": list(DEFAULT_BLOCKING)})[0]


@transaction.atomic
def set_settings(actor, *, block: bool, blocking: list[str], reason: str) -> CheckSettings:
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes the document accessibility check.")
    reason = (reason or "").strip()
    if not reason:
        raise AccessDenied("Say why the settings change.")
    codes = [c for c in checks.CODES if c in set(blocking)]
    if block and not codes:
        raise AccessDenied("Choose at least one finding that refuses an upload, or leave blocking off.")
    cfg = CheckSettings.objects.select_for_update().get(pk=settings_row().pk)
    before = {"block": cfg.block, "blocking": cfg.blocking}
    cfg.block, cfg.blocking, cfg.changed_by = bool(block), codes, actor
    cfg.save()
    audit.record("accessibility.settings_changed", obj=cfg, before=before, after={"block": cfg.block, "blocking": codes}, reason=reason)
    return cfg


def refusal(result: checks.Result, name: str, cfg: CheckSettings | None = None) -> str:
    """The sentence refusing the file, or "" when the settings let it be stored."""
    cfg = cfg or settings_row()
    if not cfg.block:
        return ""
    found = [f for f in result.findings if f.code in set(cfg.blocking)]
    if not found:
        return ""
    what = "; ".join(f"{f.describe()}: {f.fix}" for f in found)
    return f"{name} is not accessible enough to be filed: {what} Fix it and upload it again (see Help, Accessibility)."


def inspect(path: Path, name: str, *, enforce: bool = True) -> checks.Result:
    """Check a file on local disk before it is stored; Refused when `enforce` and the settings refuse what was found."""
    result = checks.check(Path(path), name)
    if enforce:
        sentence = refusal(result, name)
        if sentence:
            raise Refused(sentence)
    return result


def record(
    source: str,
    ref,
    result: checks.Result,
    *,
    file_name: str,
    sha256: str,
    url: str = "",
    title: str = "",
    organisation=None,
    uploaded_by=None,
    imported: bool = False,
) -> FileCheck | None:
    """Keep the check of a stored file (None for a type the check does not read). One check per file: storing it again does nothing."""
    if not result.kind:
        return None
    fc, created = FileCheck.objects.get_or_create(
        source=source,
        ref=str(ref),
        defaults={
            "url": url[:500],
            "title": title[:300],
            "file_name": file_name[:255],
            "sha256": sha256,
            "kind": result.kind,
            "findings": [f.as_dict() for f in result.findings],
            "note": result.note[:300],
            "organisation": organisation,
            "uploaded_by": uploaded_by if getattr(uploaded_by, "is_authenticated", False) else None,
            "imported": imported,
        },
    )
    if created:
        audit.record(
            "accessibility.checked",
            obj=fc,
            after={"source": source, "file": file_name, "sha256": sha256, "findings": fc.findings, "note": fc.note},
            actor=fc.uploaded_by,
            organisation=organisation,
        )
    return fc


def for_refs(source: str, refs) -> dict[str, FileCheck]:
    return {fc.ref: fc for fc in FileCheck.objects.filter(source=source, ref__in=[str(r) for r in refs])}


def upload_warning(source: str, refs) -> str:
    """The warning for the page an uploader lands on, naming each file the check flagged and what to fix; "" when none was."""
    flagged = [fc for fc in for_refs(source, refs).values() if fc.findings]
    if not flagged:
        return ""
    parts = []
    for fc in sorted(flagged, key=lambda f: f.file_name.lower()):
        parts.append(f"{fc.file_name}: " + "; ".join(f.describe() for f in fc.finding_list))
    return (
        "The accessibility check found problems that may keep some people from reading "
        + ("this file" if len(flagged) == 1 else "these files")
        + ": "
        + " · ".join(parts)
        + ". The file is kept; please file an accessible version. Help, Accessibility says how to fix each problem."
    )


@transaction.atomic
def review(actor, fc: FileCheck, note: str) -> FileCheck:
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator reviews the accessibility check.")
    note = (note or "").strip()
    if not note:
        raise AccessDenied("Say what was done, or why the file stands as it is.")
    fc = FileCheck.objects.select_for_update().get(pk=fc.pk)
    before = {"reviewed": fc.reviewed, "note": fc.review_note}
    fc.reviewed_by, fc.reviewed_at, fc.review_note = actor, timezone.now(), note
    fc.save(update_fields=["reviewed_by", "reviewed_at", "review_note"])
    audit.record("accessibility.reviewed", obj=fc, before=before, after={"reviewed": True, "note": note}, reason=note)
    return fc


@transaction.atomic
def reopen(actor, fc: FileCheck, reason: str) -> FileCheck:
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator reviews the accessibility check.")
    reason = (reason or "").strip()
    if not reason:
        raise AccessDenied("Say why it needs another look.")
    fc = FileCheck.objects.select_for_update().get(pk=fc.pk)
    before = {"reviewed": fc.reviewed, "note": fc.review_note}
    fc.reviewed_by, fc.reviewed_at, fc.review_note = None, None, ""
    fc.save(update_fields=["reviewed_by", "reviewed_at", "review_note"])
    audit.record("accessibility.reopened", obj=fc, before=before, after={"reviewed": False}, reason=reason)
    return fc


def listing(*, state: str = "open", kind: str = "", code: str = "", source: str = ""):
    """The checks the admin page lists: "open" (findings or not checked, not reviewed), "reviewed", "clear" or "all"."""
    qs = FileCheck.objects.select_related("organisation", "uploaded_by", "reviewed_by")
    if state == "open":
        qs = qs.filter(reviewed_at__isnull=True).exclude(findings=[], note="")
    elif state == "reviewed":
        qs = qs.filter(reviewed_at__isnull=False)
    elif state == "clear":
        qs = qs.filter(findings=[], note="")
    if kind in ("pdf", "workbook"):
        qs = qs.filter(kind=kind)
    if code in checks.CODES:
        qs = qs.filter(findings__contains=[{"code": code}])
    if source in FileCheck.Source.values:
        qs = qs.filter(source=source)
    return qs


def counts() -> dict[str, int]:
    open_ = FileCheck.objects.filter(reviewed_at__isnull=True).exclude(findings=[], note="").count()
    return {
        "open": open_,
        "reviewed": FileCheck.objects.filter(reviewed_at__isnull=False).count(),
        "clear": FileCheck.objects.filter(findings=[], note="").count(),
        "all": FileCheck.objects.count(),
    }
