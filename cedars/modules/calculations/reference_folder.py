"""The workers' shared reference folder, filled from the registered data packs.

The ReCET worker prices from a reference folder on its own file system (the ACC files and the impact-profile library), and refuses a
job whose pinned files (a job spec's packs.files) differ from the folder's. On a single server that folder is the server's own; in
production it is one network file system (infra/terraform efs.tf) that every AWS Batch job and the fast-lane worker mount read-only,
and that `calculations_sync` keeps up to date here: each pass copies, from the packs' own storage, the files a new run would be pinned
to (`service.packs_for` chooses the same way: the newest published pack of a kind, else the newest draft) and writes the folder's
manifest.json in ReCET's format, with each file's SHA-256 so the worker does not hash 9 GB per job. A file already there with the
pinned SHA-256 and size is kept, so a pass with nothing new reads only the manifest; a file put there by hand (a folder built with
`recet data build` and copied in) stays until a pack replaces it, and only files this sync copied (their manifest entry names the
pack "version") are taken out when no chosen pack has them.

A newly published pack reaches the folder on the next pass, once its files are copied (a few minutes for the profile library); a run
pinned to it before then is refused by the worker as "reference file differs" and can be started again. The health check
`calculations.reference_folder` lists the files the folder does not hold yet, from what the last pass found (state)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from cedars.services.refdata import files as pack_files
from cedars.services.refdata.models import PackVersion

from .service import REFERENCE_PREFIXES

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"  # ReCET datapack.MANIFEST
MANIFEST_FORMAT = 2  # ReCET datapack.MANIFEST_FORMAT
CHUNK = 8 * 1024 * 1024
STATE_KEY = "calculations:reference_folder"


def folder() -> Path | None:
    """The shared reference folder this deployment keeps (CALCULATIONS_REFERENCE_FOLDER), or None when it keeps none."""
    path = getattr(settings, "CALCULATIONS_REFERENCE_FOLDER", "")
    return Path(path) if path else None


def _newest(packs):
    published = packs.filter(status=PackVersion.Status.PUBLISHED).order_by("-published_at", "-registered_at").first()
    return published or packs.filter(status=PackVersion.Status.DRAFT).order_by("-registered_at").first()


def chosen_packs() -> list[PackVersion]:
    """The packs whose files the folder holds: per ACC year the electric ACC pack a run would be pinned to, and the impact-profile
    library a run would be pinned to."""
    out = []
    live = PackVersion.objects.exclude(status=PackVersion.Status.WITHDRAWN)
    accs = live.filter(kind=PackVersion.Kind.ACC_ELECTRIC, acc_year__isnull=False).values_list("acc_year", flat=True).distinct()
    for year in sorted(set(accs)):
        pack = _newest(live.filter(kind=PackVersion.Kind.ACC_ELECTRIC, acc_year=year))
        if pack is not None:
            out.append(pack)
    pack = _newest(live.filter(kind=PackVersion.Kind.IMPACT_PROFILES))
    if pack is not None:
        out.append(pack)
    return out


TOP_FOLDERS = ("acc/", "impact_profiles/", "cet_versions/")  # ReCET's reference folder (paths.REF_FILES)


def folder_path(key: str, prefix: str) -> str | None:
    """Where a pack's file `key` goes in the folder: a pack registered from the whole reference folder keeps the folder's paths
    (acc/2024/electric_hourly.parquet); one registered from its own folder (acc/2024/, impact_profiles/) has paths under it, which go
    under its kind's folder. None for another kind's file in a whole-folder pack."""
    if key.startswith(prefix):
        return key
    if key.startswith(TOP_FOLDERS):
        return None
    return prefix + key


def wanted() -> dict[str, dict]:
    """{reference-folder path: manifest entry} of every file the folder should hold, with the pack it comes from."""
    files: dict[str, dict] = {}
    for pack in chosen_packs():
        prefix = REFERENCE_PREFIXES[pack.kind].format(acc=pack.acc_year)
        for key, entry in sorted((pack.manifest or {}).items()):
            rel = folder_path(key, prefix)
            if rel is None:
                continue
            info = {k: v for k, v in entry.items() if k in ("what", "type", "required", "acc_version")}
            info["sha256"] = entry["sha256"]
            if pack.kind == PackVersion.Kind.ACC_ELECTRIC:
                # ReCET labels a run on a draft ACC "(draft)" and refuses a filing on it (jobs.apply_draft)
                info["acc_version"] = str(pack.acc_year)
                info["draft"] = pack.status == PackVersion.Status.DRAFT or bool(entry.get("draft"))
            info["pack"], info["key"] = pack.pk, key
            info["version"] = f"{pack.label} {pack.version}".strip()
            files[rel] = info
    return files


def read_manifest(dest: Path) -> dict:
    try:
        return json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def behind(dest: Path | None = None) -> list[str]:
    """The paths the folder should hold but does not (missing, or another SHA-256 or size than its manifest says)."""
    dest = dest or folder()
    if dest is None:
        return []
    have = read_manifest(dest).get("files", {})
    out = []
    for rel, info in wanted().items():
        mine, path = have.get(rel) or {}, dest / rel
        if mine.get("sha256") != info["sha256"] or not path.is_file() or mine.get("size") != path.stat().st_size:
            out.append(rel)
    return out


def _copy(pack: PackVersion, rel: str, target: Path, sha256: str) -> int:
    """Copy one pack file to `target` through a temporary name, checking its SHA-256; returns its size."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")  # two sync tasks overlap during a deploy
    digest, size = hashlib.sha256(), 0
    try:
        with pack_files.open_file(pack, rel) as src, open(tmp, "wb") as out:
            while chunk := src.read(CHUNK):
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError(f"{rel} in {pack} has SHA-256 {digest.hexdigest()}, not the {sha256} its manifest gives")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return size


def sync(dest: Path | None = None) -> dict:
    """Bring the folder up to date with the packs; returns {"copied": [...], "kept": n, "removed": [...], "failed": {path: error}}."""
    dest = dest or folder()
    if dest is None:
        return {"copied": [], "kept": 0, "removed": [], "failed": {}}
    dest.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(dest)
    have = manifest.get("files", {})
    want = wanted()
    packs = {p.pk: p for p in PackVersion.objects.filter(pk__in={i["pack"] for i in want.values()})}
    files, copied, failed, kept = {}, [], {}, 0
    for rel, info in want.items():
        target, mine = dest / rel, have.get(rel) or {}
        entry = {k: v for k, v in info.items() if k not in ("pack", "key")}
        if mine.get("sha256") == info["sha256"] and target.is_file() and mine.get("size") == target.stat().st_size:
            files[rel] = {**entry, "size": target.stat().st_size}
            kept += 1
            continue
        try:
            log.info("reference folder: copying %s from %s", rel, packs[info["pack"]])
            files[rel] = {**entry, "size": _copy(packs[info["pack"]], info["key"], target, info["sha256"])}
            copied.append(rel)
        except Exception as e:  # noqa: BLE001 - one unreadable file must not stop the others; the health check shows it
            log.exception("reference folder: %s", rel)
            failed[rel] = str(e)
            if mine and target.is_file() and mine.get("size") == target.stat().st_size:
                files[rel] = mine  # the file it had stays listed as it is (runs pinned to it still work)
    removed = []
    for rel in sorted(set(have) - set(want)):
        if not have[rel].get("version"):
            files[rel] = have[rel]  # put there by hand (`recet data build`, a copied folder): left as it is
            continue
        # a file this sync copied that no chosen pack has any more (its pack withdrawn): taken out
        (dest / rel).unlink(missing_ok=True)
        removed.append(rel)
    if copied or removed or failed or files != have:
        now = datetime.now().astimezone()
        new = {
            "format": MANIFEST_FORMAT,
            "created": now.isoformat(timespec="seconds"),
            "pack": {"type": "reference", "version": f"CEDARS {now:%Y-%m-%d %H:%M}"},
            "files": files,
        }
        tmp = dest / f".{MANIFEST}.{uuid.uuid4().hex}.part"
        tmp.write_text(json.dumps(new, indent=2), encoding="utf-8")
        os.replace(tmp, dest / MANIFEST)
    behind_now = sorted(set(want) - set(files) | set(failed))
    cache.set(STATE_KEY, {"checked": timezone.now().isoformat(), "behind": behind_now, "failed": failed}, timeout=None)
    return {"copied": copied, "kept": kept, "removed": removed, "failed": failed}


def state() -> dict | None:
    """What the last sync found (the health check reads it on any task, since only the calculations sync mounts the folder):
    {"checked": ISO time, "behind": [paths not there], "failed": {path: error}}; None when no sync has run here."""
    return cache.get(STATE_KEY)


def accs(dest: Path | None = None) -> list[str]:
    """The ACC years whose electric file the folder holds (what the fast lane primes)."""
    dest = dest or folder()
    if dest is None:
        return []
    have = read_manifest(dest).get("files", {})
    return sorted({str(i["acc_version"]) for rel, i in have.items() if i.get("acc_version") and (dest / rel).is_file()})
