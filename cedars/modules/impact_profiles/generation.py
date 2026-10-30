"""Generating deemed impact profiles from a package's load shapes, as CEDARS' Load Shape Tool does.

For each permutation of the package's current eTRM version, the measure-case shape (the measure technology's TechGroup, TechType and
TechID) and the base-case shape (the baseline technology's own TechGroup, TechType and TechID, which eTRM gives separately: a heat pump
replacing a furnace has a furnace base case) of the same Sector, BldgType, BldgVint and BldgHVAC are paired per climate zone, and the
impact profile is CEDARS' formula (`_calculate_impacts` in its source):

    impact[h] = (base[h] x UEC_base - measure[h] x UEC_measure) / (UEC_base - UEC_measure)

which sums to 1 because both shapes do. A permutation whose UECs are equal has no savings to shape and gets no profile (CEDARS returns
nothing there too). An accelerated-replacement permutation gets a profile for each baseline: the 1st paired with the pre-existing
technology, the 2nd with the standard technology (the engine prices a 2nd-baseline shape, so the 1st-baseline-only workaround ends).

The profile ID is the guide's formula, [BldgType]*[BldgVint]*[BldgHVAC]*[Measure Tech ID]*[base-case Tech ID], so eTRM permutations
keep pointing at it. A permutation with "Any" in BldgType, BldgHVAC or BldgLoc is matched to the explicit shapes uploaded for it: every
climate zone uploaded, and for BldgType the Com / Res stand-in (or the only type uploaded). The profile's target sector is the
permutation's electric target sector, or its Sector when eTRM leaves that blank.

A generated profile is never edited: a changed shape, UEC or pairing gives the profile a new version, and the old version stays for the
runs and claims that named it. An unchanged result adds nothing.
"""

from dataclasses import dataclass, field

import numpy as np
from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts.models import Organisation

from . import shapes
from .loadshapes import zone
from .models import ImpactProfile, LoadShape, LoadShapeUpload, LoadShapeVersion, MeasurePackage, ProfileVersion, ProfileZone

STAND_INS = ("Com", "Res")


def profile_key(bldg_type: str, bldg_vint: str, bldg_hvac: str, measure_tech_id: str, base_tech_id: str) -> str:
    return "*".join([bldg_type, bldg_vint, bldg_hvac, measure_tech_id, base_tech_id])


def calculate_impacts(base: np.ndarray, measure: np.ndarray, uec_base: float, uec_measure: float) -> np.ndarray | None:
    denominator = uec_base - uec_measure
    if denominator == 0:
        return None
    return (base * uec_base - measure * uec_measure) / denominator


@dataclass
class Outcome:
    profiles: dict = field(default_factory=dict)  # (key, sector) -> {zone: (array, source)}
    years: dict = field(default_factory=dict)  # (key, sector) -> source year
    skipped: list = field(default_factory=list)  # (permutation, reason)
    notes: list = field(default_factory=list)


def _pick(options: set[str], wanted: str, stand_in: tuple[str, ...]) -> tuple[str | None, str]:
    """The uploaded value standing for a permutation's value: itself, or for "Any" the stand-in or the only value uploaded."""
    if wanted.lower() != "any":
        return (wanted, "") if wanted in options else (None, "")
    if wanted in options:
        return wanted, ""
    preferred = [o for o in options if o in stand_in]
    if len(preferred) == 1:
        return preferred[0], ""
    if len(options) == 1:
        return next(iter(options)), ""
    if not options:
        return None, ""
    return None, f"several values were uploaded for its Any ({', '.join(sorted(options))}); upload one, or the Com / Res stand-in"


def pair(package: MeasurePackage) -> Outcome:
    """The profiles the package's current permutations and current load shapes give, without saving anything."""
    out = Outcome()
    if package.current is None:
        return out
    current = {}
    for v in LoadShapeVersion.objects.filter(load_shape__package=package, superseded_at__isnull=True).select_related("load_shape", "shape"):
        ls = v.load_shape
        current[(ls.sector, ls.bldg_type, ls.bldg_vint, ls.bldg_hvac, ls.bldg_loc, ls.tech_group, ls.tech_type, ls.tech_id)] = v
    loaded: dict[int, np.ndarray] = {}

    def values(v: LoadShapeVersion) -> np.ndarray:
        if v.shape_id not in loaded:
            loaded[v.shape_id] = shapes.load(v.shape)
        return loaded[v.shape_id]

    for p in package.current.permutations.all():
        techs = p.technologies()
        mine = {k: v for k, v in current.items() if (k[0], k[2]) == (p.sector, p.bldg_vint) and k[5:] in techs}
        if not mine:
            out.skipped.append((p, "no load shapes uploaded for it"))
            continue
        btype, why = _pick({k[1] for k in mine}, p.bldg_type, STAND_INS)
        if btype is None:
            out.skipped.append((p, why or f"no load shapes uploaded for BldgType {p.bldg_type}"))
            continue
        hvac, why = _pick({k[3] for k in mine if k[1] == btype}, p.bldg_hvac, ())
        if hvac is None:
            out.skipped.append((p, why or f"no load shapes uploaded for BldgHVAC {p.bldg_hvac}"))
            continue
        loc = zone(p.bldg_loc)
        for baseline, base_tech, uec_base in p.baselines():
            key = profile_key(p.bldg_type, p.bldg_vint, p.bldg_hvac, p.measure_tech_id, base_tech[2])
            if baseline == "1st" and p.electric_impact_profile_id and p.electric_impact_profile_id != key:
                out.notes.append(f"{p}: eTRM's Electric Impact Profile ID {p.electric_impact_profile_id!r} is not the formula's {key!r}.")
            if uec_base is None or p.uec_kwh_meas is None or uec_base == p.uec_kwh_meas:
                out.skipped.append((p, f"{baseline} baseline: the UECs are equal, so there are no savings to shape"))
                continue
            sector = p.elec_target_sector or p.sector
            zones = sorted(
                {k[4] for k in mine if k[1] == btype and k[3] == hvac and (loc.lower() == "any" or k[4] == loc)}, key=_zone_order
            )
            made = 0
            for z in zones:
                m = current.get((p.sector, btype, p.bldg_vint, hvac, z, *p.measure_technology))
                b = current.get((p.sector, btype, p.bldg_vint, hvac, z, *base_tech))
                if m is None or b is None:
                    continue
                impact = calculate_impacts(values(b), values(m), uec_base, p.uec_kwh_meas)
                source = {
                    "permutation": p.pk,
                    "baseline": baseline,
                    "measure_load_shape": m.pk,
                    "base_load_shape": b.pk,
                    "uec_kwh_meas": p.uec_kwh_meas,
                    "uec_kwh_base": uec_base,
                }
                slot = out.profiles.setdefault((key, sector), {})
                if z in slot:
                    if not np.allclose(slot[z][0], impact, rtol=0, atol=1e-12):
                        out.notes.append(f"{key} / {sector} CZ {z}: two permutations give different profiles; the first one is kept.")
                    continue
                slot[z] = (impact, source)
                year = m.source_year
                if b.source_year != year:
                    out.notes.append(
                        f"{key} CZ {z}: the measure shape is drawn on {year} and the base shape on {b.source_year}; {year} is used."
                    )
                out.years.setdefault((key, sector), year)
                made += 1
            if not made:
                missing = "measure-case" if not any(k[5:] == p.measure_technology for k in mine) else "base-case"
                out.skipped.append((p, f"{baseline} baseline: no climate zone has both shapes (the {missing} shape is missing)"))
    return out


def _zone_order(z: str):
    digits = "".join(c for c in z if c.isdigit())
    return (int(digits) if digits else 99, z)


def generate(actor, package: MeasurePackage, upload: LoadShapeUpload | None = None) -> dict:
    """Pair, compute and save the package's profiles; returns the summary shown on the upload page."""
    if upload is not None:
        upload.status = LoadShapeUpload.Status.GENERATING
        upload.save(update_fields=["status"])
    outcome = pair(package)
    summary = {"created": 0, "new_version": 0, "unchanged": 0, "conflicts": [], "skipped": [], "notes": outcome.notes[:200], "profiles": []}
    owner = Organisation.objects.filter(code=package.pa_lead).first()
    now = timezone.now()
    with transaction.atomic():
        for (key, sector), zones in sorted(outcome.profiles.items()):
            profile, created = ImpactProfile.objects.select_for_update().get_or_create(
                profile_key=key,
                target_sector=sector,
                iou_ac_territory="",
                defaults={"family": ImpactProfile.Family.DEEMED, "package": package, "owner": owner, "created_by": actor},
            )
            if profile.family != ImpactProfile.Family.DEEMED or (profile.package_id and profile.package_id != package.pk):
                summary["conflicts"].append(
                    f"{key} / {sector} already belongs to {profile.package or profile.get_family_display()}; not changed."
                )
                continue
            if profile.retired:
                summary["conflicts"].append(f"{key} / {sector} is retired; not regenerated.")
                continue
            stored = {z: shapes.store(arr) for z, (arr, _) in zones.items()}
            year = outcome.years[(key, sector)]
            current = profile.current_version()
            if (
                current is not None
                and current.represents_year == year
                and {z.climate_zone: z.shape_id for z in current.zones.all()} == {z: s.pk for z, s in stored.items()}
            ):
                summary["unchanged"] += 1
                continue
            if current is not None:
                ProfileVersion.objects.filter(pk=current.pk).update(superseded_at=now)
            version = ProfileVersion.objects.create(
                profile=profile,
                number=(current.number + 1) if current else 1,
                represents_year=year,
                source={
                    "kind": "generated",
                    "package": package.measure_version_id,
                    "package_version": package.current.number,
                    "upload": upload.pk if upload else None,
                    "zones": {z: src for z, (_, src) in zones.items()},
                },
                reason="generated from load shapes" if current is None else "regenerated: shapes, UECs or pairing changed",
                created_by=actor,
            )
            ProfileZone.objects.bulk_create(ProfileZone(version=version, climate_zone=z, shape=s) for z, s in stored.items())
            summary["created" if current is None else "new_version"] += 1
            summary["profiles"].append([profile.pk, key, sector, version.number])
            audit.record(
                "impact_profile.generated",
                obj=profile,
                before={"version": current.number} if current else None,
                after={"version": version.number, "zones": sorted(stored, key=_zone_order), "package": package.measure_version_id},
                actor=actor,
                organisation=owner,
            )
        summary["skipped"] = [[str(p), reason] for p, reason in outcome.skipped][:500]
        if upload is not None:
            upload.status, upload.generated_at, upload.generation_summary = LoadShapeUpload.Status.GENERATED, timezone.now(), summary
            upload.save(update_fields=["status", "generated_at", "generation_summary"])
    return summary


def shapes_of(package: MeasurePackage):
    return LoadShape.objects.filter(package=package).prefetch_related("versions")
