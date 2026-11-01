"""Custom (non-deemed) impact profiles and NR/NC mapping rows.

A custom profile belongs to the PA that uploaded it and is visible to everyone signed in (the CPUC included). Its file is a CSV, zipped or
not, with the columns Climate Zone (1 to 16, with or without CZ, or System for a statewide profile), Hour of Year and Impact: one
8,760 per zone, checked with the same 8,760 rules as load shapes (LS-S01 to LS-S04). Replacing it adds a version (runs and claims that
named the old one keep it, including a NMEC true-up's revised profile, BR-05.6); retiring keeps every version but leaves it out of the
library the engine reads from then on. A file identical to a profile already in the library (every zone's 8,760 and the calendar) is
attached to it as another name (identical.py, BR-06.18): the library stores the 8,760s once.

A mapping row lets a permutation's profile ID stand for an existing profile, e.g. a measure's new-construction permutations (BldgVint
New) using the existing-vintage profile, instead of hard-coding the vintage to Ex in eTRM (architecture: NR and NC sharing one shape).
"""

import csv
import io
import re

import numpy as np
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts.service import AccessDenied

from . import access, identical, shapes
from .loadshapes import SUM_TOLERANCE, Finding, check_header, hour_problem, open_csv, zone
from .models import HOURS, ImpactProfile, MeasurePackage, ProfileMapping, ProfileVersion, ProfileZone

COLUMNS = ["Climate Zone", "Hour of Year", "Impact"]


class ProfileRejected(Exception):
    """The file broke one or more rules; `findings` says where. Nothing was saved."""

    def __init__(self, findings: list[Finding]):
        super().__init__(f"{len(findings)} problems")
        self.findings = findings


def read_profile_file(data: bytes, name: str) -> dict[str, np.ndarray]:
    """{climate zone: 8,760} from a custom profile file, or ProfileRejected with every finding."""
    max_mb = settings.IMPACT_PROFILES_CUSTOM_MAX_MB  # on the Limits page (/limits/)
    if len(data) > max_mb * 1024 * 1024:
        raise ProfileRejected(
            [Finding("LS-F02", message=f"The file is {len(data) / 1024 / 1024:.1f} MB; a custom profile file is at most {max_mb} MB.")]
        )
    text, findings = open_csv(io.BytesIO(data), name, len(data))
    if text is None:
        raise ProfileRejected(findings)
    try:
        reader = csv.reader(text)
        header = next(reader, None)
        if header is None:
            raise ProfileRejected([Finding("LS-F05")])
        cols, findings = check_header(header, COLUMNS)
        if findings:
            raise ProfileRejected(findings)
        hours: dict[str, list] = {}
        values: dict[str, list] = {}
        first: dict[str, int] = {}
        for line, row in enumerate(reader, start=2):
            if not row or not any(c.strip() for c in row):
                continue
            row = row + [""] * (max(cols.values()) + 1 - len(row))
            raw_zone = row[cols["Climate Zone"]].strip()
            z = "System" if raw_zone.lower() in ("system", "statewide") else zone(raw_zone)
            if z != "System" and not re.fullmatch(r"([1-9]|1[0-6])", z):
                findings.append(
                    Finding(
                        "LS-S08",
                        row=line,
                        field="Climate Zone",
                        value=raw_zone[:20],
                        message="Climate Zone must be 1 to 16, or System for statewide.",
                    )
                )
                if len(findings) > 50:
                    break
                continue
            first.setdefault(z, line)
            try:
                hours.setdefault(z, []).append(int(row[cols["Hour of Year"]]))
            except ValueError:
                findings.append(
                    Finding("LS-S01", row=line, record_id=f"CZ {z}", field="Hour of Year", value=row[cols["Hour of Year"]][:40])
                )
                hours.setdefault(z, []).append(-1)
            try:
                v = float(row[cols["Impact"]])
                if not np.isfinite(v):
                    raise ValueError
                values.setdefault(z, []).append(v)
            except ValueError:
                findings.append(Finding("LS-S03", row=line, record_id=f"CZ {z}", field="Impact", value=row[cols["Impact"]][:40]))
                values.setdefault(z, []).append(0.0)
            if len(findings) > 50:
                break
    except (UnicodeDecodeError, csv.Error) as e:
        raise ProfileRejected([Finding("LS-F04", message=f"The file cannot be read: {e}")]) from e
    if not hours and not findings:
        raise ProfileRejected([Finding("LS-F05")])
    out = {}
    for z, hs in hours.items():
        problem = hour_problem(np.array(hs))
        if problem:
            findings.append(Finding("LS-S02", row=first[z], record_id=f"CZ {z}", field="Hour of Year", message=problem))
            continue
        a = np.array(values[z], dtype=float)
        if abs(a.sum() - 1) > SUM_TOLERANCE:
            findings.append(
                Finding(
                    "LS-S04",
                    row=first[z],
                    record_id=f"CZ {z}",
                    field="Impact",
                    value=f"{a.sum():.6f}",
                    message=f"Climate zone {z}'s impacts add up to {a.sum():.6f}, not 1.",
                    fix="Normalize the zone's 8,760 values: divide each hour by the zone's annual total.",
                )
            )
            continue
        out[z] = a
    if findings:
        raise ProfileRejected(findings)
    return out


def _check_key(profile_key: str, target_sector: str, territory: str) -> tuple[str, str, str]:
    profile_key, target_sector, territory = profile_key.strip(), target_sector.strip(), territory.strip().upper()
    if not profile_key or not target_sector:
        raise AccessDenied("Give the profile ID and target sector.")
    if not profile_key.isascii() or len(profile_key) > 300:
        raise AccessDenied("A profile ID is plain text (ASCII), up to 300 characters.")
    if territory and territory not in settings.IMPACT_PROFILE_TERRITORIES:
        raise AccessDenied(f"IOU territory must be one of {', '.join(settings.IMPACT_PROFILE_TERRITORIES)}, or blank for every territory.")
    return profile_key, target_sector, territory


def _new_version(actor, profile: ImpactProfile, zones: dict, *, represents_year, reason: str, source: dict) -> ProfileVersion:
    current = profile.current_version()
    if current is not None:
        ProfileVersion.objects.filter(pk=current.pk).update(superseded_at=timezone.now())
    version = ProfileVersion.objects.create(
        profile=profile,
        number=(current.number + 1) if current else 1,
        represents_year=represents_year,
        source=source,
        reason=reason,
        created_by=actor,
    )
    ProfileZone.objects.bulk_create(ProfileZone(version=version, climate_zone=z, shape=shapes.store(a)) for z, a in zones.items())
    identical.stamp(version)
    return version


@transaction.atomic
def create_custom(
    actor,
    organisation,
    *,
    profile_key: str,
    target_sector: str,
    territory: str = "",
    description: str = "",
    represents_year: int | None = None,
    data: bytes,
    file_name: str,
) -> ImpactProfile:
    access.require_submit(actor, organisation, "add a custom impact profile")
    profile_key, target_sector, territory = _check_key(profile_key, target_sector, territory)
    if ImpactProfile.objects.filter(profile_key=profile_key, target_sector=target_sector, iou_ac_territory=territory).exists():
        raise AccessDenied(f"{profile_key} / {target_sector} already exists. Replace it from its page if it is yours.")
    zones = read_profile_file(data, file_name)
    profile = ImpactProfile.objects.create(
        profile_key=profile_key,
        target_sector=target_sector,
        iou_ac_territory=territory,
        family=ImpactProfile.Family.CUSTOM,
        owner=organisation,
        description=description[:500],
        created_by=actor,
    )
    version = _new_version(
        actor, profile, zones, represents_year=represents_year, reason="added", source={"kind": "uploaded", "file": file_name}
    )
    audit.record(
        "impact_profile.created",
        obj=profile,
        after={
            "family": "custom",
            "owner": organisation.code,
            "version": version.number,
            "zones": sorted(zones),
            "file": file_name,
            "identical_to": identical.describe(identical.same_profile(version.content_sha256, exclude=profile)),
        },
        actor=actor,
        organisation=organisation,
    )
    return profile


def _require_owner(actor, organisation, profile: ImpactProfile, what: str) -> None:
    if access.is_admin(actor):
        return
    if profile.family != ImpactProfile.Family.CUSTOM:
        raise AccessDenied(f"Only custom profiles can be {what} here; deemed profiles change when their load shapes do.")
    access.require_submit(actor, organisation, f"{what.rstrip('d')} a custom profile")
    if profile.owner_id != organisation.pk:
        raise AccessDenied(f"{profile.profile_key} belongs to {profile.owner.code if profile.owner else 'the CPUC'}.")


@transaction.atomic
def replace_custom(
    actor, organisation, profile: ImpactProfile, *, data: bytes, file_name: str, reason: str, represents_year=None
) -> ProfileVersion:
    profile = ImpactProfile.objects.select_for_update().get(pk=profile.pk)
    if profile.family != ImpactProfile.Family.CUSTOM:
        raise AccessDenied("Only custom profiles are replaced by upload; deemed profiles are regenerated from their load shapes.")
    _require_owner(actor, organisation, profile, "replaced")
    if profile.retired:
        raise AccessDenied("A retired profile is not replaced.")
    if not reason.strip():
        raise AccessDenied("Give a reason for the replacement.")
    zones = read_profile_file(data, file_name)
    before = profile.current_version()
    version = _new_version(
        actor, profile, zones, represents_year=represents_year, reason=reason, source={"kind": "uploaded", "file": file_name}
    )
    audit.record(
        "impact_profile.replaced",
        obj=profile,
        before={"version": before.number if before else None},
        after={
            "version": version.number,
            "zones": sorted(zones),
            "file": file_name,
            "identical_to": identical.describe(identical.same_profile(version.content_sha256, exclude=profile)),
        },
        reason=reason,
        actor=actor,
        organisation=organisation,
    )
    return version


@transaction.atomic
def retire(actor, organisation, profile: ImpactProfile, reason: str) -> ImpactProfile:
    profile = ImpactProfile.objects.select_for_update().get(pk=profile.pk)
    _require_owner(actor, organisation, profile, "retired")
    if profile.retired:
        raise AccessDenied("That profile is already retired.")
    if not reason.strip():
        raise AccessDenied("Give a reason for retiring the profile.")
    profile.retired_at, profile.retired_by, profile.retire_reason = timezone.now(), actor, reason
    profile.save(update_fields=["retired_at", "retired_by", "retire_reason"])
    audit.record("impact_profile.retired", obj=profile, reason=reason, actor=actor, organisation=organisation)
    return profile


# --- mapping rows


@transaction.atomic
def create_mapping(
    actor, organisation, package: MeasurePackage, *, alias_key: str, target_sector: str, target: ImpactProfile, reason: str
) -> ProfileMapping:
    access.require_package_lead(actor, organisation, package)
    alias_key, target_sector = alias_key.strip(), target_sector.strip()
    if not alias_key or not target_sector or not reason.strip():
        raise AccessDenied("Give the profile ID it stands for, its target sector and a reason.")
    if target.retired:
        raise AccessDenied("A mapping cannot point at a retired profile.")
    if alias_key == target.profile_key and target_sector == target.target_sector:
        raise AccessDenied("A profile cannot stand for itself.")
    if ImpactProfile.objects.filter(profile_key=alias_key, target_sector=target_sector, retired_at__isnull=True).exists():
        raise AccessDenied(f"{alias_key} / {target_sector} is a profile of its own; retire it before pointing its ID elsewhere.")
    if ProfileMapping.objects.filter(alias_key=alias_key, target_sector=target_sector, ended_at__isnull=True).exists():
        raise AccessDenied(f"{alias_key} / {target_sector} already points at a profile; end that mapping first.")
    m = ProfileMapping.objects.create(
        package=package, alias_key=alias_key, target_sector=target_sector, target=target, reason=reason, created_by=actor
    )
    audit.record(
        "profile_mapping.created",
        obj=m,
        after={"alias": alias_key, "sector": target_sector, "target": str(target), "package": package.measure_version_id},
        reason=reason,
        actor=actor,
        organisation=organisation,
    )
    return m


@transaction.atomic
def end_mapping(actor, organisation, mapping: ProfileMapping, reason: str = "") -> ProfileMapping:
    access.require_package_lead(actor, organisation, mapping.package)
    if mapping.ended_at is not None:
        raise AccessDenied("That mapping has already ended.")
    mapping.ended_at, mapping.ended_by = timezone.now(), actor
    mapping.save(update_fields=["ended_at", "ended_by"])
    audit.record("profile_mapping.ended", obj=mapping, reason=reason, actor=actor, organisation=organisation)
    return mapping


def template_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(COLUMNS)
    for h in range(1, HOURS + 1):
        w.writerow(["12", h, f"{1 / HOURS:.12g}"])
    return out.getvalue()
