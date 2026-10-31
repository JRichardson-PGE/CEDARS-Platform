"""Identical impact profiles: one profile with several names (BRD v0.5 BR-06.18).

Jake's rule (2026-10-31): only profiles that match 100% are consolidated, never within a tolerance. Two profiles are the same profile
when their current versions have the same climate zones, every zone's 8,760 is the same array value for value (the same content-
addressed Shape), and they are drawn on the same calendar (represents_year, blank = 2009, as the engine prices it). Profile IDs, sectors
and territories may differ: such a profile carries every name it is filed under, whichever Impact Profile Name each is.

Each name stays an ImpactProfile of its own (its owner, family and numbered versions), so replacing or retiring one name never changes
another's shapes: the grouping follows the current versions' content (`ProfileVersion.content_sha256`) and cannot go stale. The library
export writes each such profile's 8,760s once and points every name at them (ReCET 0.1.87's `shape_id`); a profile's page lists its
other names; a custom upload or a generated profile identical to an existing one is attached to it as another name.
"""

import hashlib

from django.db.models import Q

from .models import ImpactProfile, ProfileMapping, ProfileVersion

DEFAULT_REPRESENTS_YEAR = 2009


def content_key(represents_year, zone_shapes: dict[str, str]) -> str:
    """The SHA-256 naming a version's content: its calendar and each zone's Shape SHA-256 (zones in sorted order)."""
    text = f"year={represents_year or DEFAULT_REPRESENTS_YEAR}\n" + "".join(f"{z}={zone_shapes[z]}\n" for z in sorted(zone_shapes))
    return hashlib.sha256(text.encode()).hexdigest()


def stamp(version: ProfileVersion) -> str:
    """Set and save `version.content_sha256` from its zones (call it once the zones are saved)."""
    key = content_key(version.represents_year, {z.climate_zone: z.shape.sha256 for z in version.zones.select_related("shape")})
    ProfileVersion.objects.filter(pk=version.pk).update(content_sha256=key)
    version.content_sha256 = key
    return key


def same_profile(content_sha256: str, *, exclude: ImpactProfile | None = None):
    """The profiles in use whose current version has exactly this content, oldest first."""
    if not content_sha256:
        return ImpactProfile.objects.none()
    qs = ImpactProfile.objects.filter(
        retired_at__isnull=True, versions__superseded_at__isnull=True, versions__content_sha256=content_sha256
    ).select_related("owner", "package")
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.order_by("pk").distinct()


def other_names(profile: ImpactProfile) -> dict:
    """{"profiles": other profiles with identical current shapes, "mappings": open mapping rows pointing at any of them or at this one}."""
    current = profile.current_version()
    others = list(same_profile(current.content_sha256, exclude=profile)) if current is not None and not profile.retired else []
    ids = [profile.pk, *(p.pk for p in others)]
    mappings = ProfileMapping.objects.filter(Q(target_id__in=ids), ended_at__isnull=True).select_related("package", "target")
    return {"profiles": others, "mappings": list(mappings)}


def describe(profiles) -> list[str]:
    return [str(p) for p in profiles]
