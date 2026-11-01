"""Which platform release each user guide and the FAQ were last revised for (BR-12.5: "user guides shall be updated with each release
so they match system behavior").

help_revisions.json holds, for each guide template (core/templates/core/help/<slug>.html) and the FAQ (core/faq.yaml), the release it
was revised for and the SHA-256 of its text then. A test fails when a guide's text no longer matches its entry, so a change to a guide
is always recorded against the release that ships it: `manage.py help_revisions --update` marks every changed guide as revised for
the release being built (cedars.__version__). Each guide shows its release, the release notes list the guides each release revised,
and the health check names a guide whose part of CEDARS a release changed without revising it."""

import hashlib
import json
import re
from functools import cache
from pathlib import Path

from django.conf import settings

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "help_revisions.json"
GUIDES_DIR = HERE / "templates" / "core" / "help"
FAQ = HERE / "faq.yaml"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def version_key(v: str) -> tuple[int, ...]:
    """'0.10.2' -> (0, 10, 2), for comparing releases; () for anything that is not one."""
    return tuple(int(p) for p in v.split(".")) if v and VERSION.match(v) else ()


def sources() -> dict[str, Path]:
    """Every revised document by key: "guide:<slug>" for each user guide, "faq" for the FAQ."""
    from .help import GUIDES

    out = {f"guide:{g.slug}": GUIDES_DIR / f"{g.slug}.html" for g in GUIDES}
    out["faq"] = FAQ
    return out


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@cache
def manifest() -> dict[str, dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def revised(key: str) -> str:
    """The release a guide ("guide:<slug>") or the FAQ ("faq") was last revised for; "" when it has never been recorded."""
    return manifest().get(key, {}).get("release", "")


def stale() -> list[str]:
    """Keys whose text changed since their entry (or that have none): run `manage.py help_revisions --update`."""
    found = manifest()
    return [k for k, p in sources().items() if found.get(k, {}).get("sha256") != fingerprint(p)]


def revised_in(release: str) -> list[str]:
    """The keys revised for one release."""
    return sorted(k for k, e in manifest().items() if e.get("release") == release)


def update(release: str | None = None) -> list[str]:
    """Record every changed guide as revised for `release` (default: the release being built). Returns the keys it changed."""
    release = release or settings.VERSION
    found = dict(manifest())
    changed = []
    for key, path in sources().items():
        fp = fingerprint(path)
        if found.get(key, {}).get("sha256") != fp:
            found[key] = {"release": release, "sha256": fp}
            changed.append(key)
    for key in set(found) - set(sources()):
        del found[key]
        changed.append(key)
    if changed:
        MANIFEST.write_text(json.dumps(dict(sorted(found.items())), indent=2) + "\n", encoding="utf-8")
        manifest.cache_clear()
    return changed
