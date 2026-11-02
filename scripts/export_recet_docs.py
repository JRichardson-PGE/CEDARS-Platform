"""Copy the pinned ReCET engine's documents into the platform (data/recet_docs/), so Help shows the documents of the engine CEDARS runs.

    uv run python scripts/export_recet_docs.py data/recet_docs/      (needs the pinned ReCET engine installed: engine/README.md)

Copies each document `cedars.core.engine_docs.DOCS` lists from the installed package's `recet/docs/` unchanged and writes manifest.json
(the engine's version, the pinned commit and each file's SHA-256). Re-run it whenever the pin moves; tests/test_engine_docs.py checks the
shipped copies are the pinned engine's."""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import recet  # noqa: E402

from cedars.core.engine_docs import DOCS  # noqa: E402


def main(out_dir: Path) -> Path:
    src = Path(recet.__file__).parent / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        old.unlink()
    files = {}
    for doc in DOCS:
        data = (src / doc.file).read_bytes()
        (out_dir / doc.file).write_bytes(data)
        files[doc.file] = hashlib.sha256(data).hexdigest()
    manifest = {
        "engine": {"version": recet.__version__, "commit": (ROOT / "engine" / "recet-commit").read_text(encoding="utf-8").strip()},
        "documents": files,
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(main(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "recet_docs")))
