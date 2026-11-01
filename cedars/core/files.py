"""The file types people may attach, and storing one such file, shared by the document library (cedars.modules.documents) and the comments
on filing versions (cedars.services.submissions), so both accept the same files, check them the same way and keep them in the same
storage area ("uploads", cedars.core.storage)."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath

from . import storage

# The file types a document or an attachment may be, with the type it is served as. Executables and web pages are refused (the PDA holds
# 16 .exe main files and 3 file names containing HTML: capture of 2026-10-30). Only the INLINE types open in the browser; the rest download.
FILE_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
INLINE = {".pdf", ".txt", ".png", ".jpg", ".jpeg"}


class FileRefused(ValueError):
    """A file that is not accepted; str(e) is the sentence a page shows."""


def clean_name(name: str) -> str:
    """The file's own name (no folders), without characters that break a download header or a storage key."""
    base = PurePath(str(name).replace("\\", "/")).name
    base = re.sub(r'[\x00-\x1f"<>|:*?/\\]+', "_", base).strip(" .")
    return base[:200] or "file"


def accepted() -> str:
    return ", ".join(sorted(FILE_TYPES))


def content_type(name: str) -> str:
    """The type a file is served as, from its extension; FileRefused when the extension is not accepted."""
    ext = Path(name).suffix.lower()
    if ext not in FILE_TYPES:
        raise FileRefused(f"{name}: files of type {ext or '(none)'} are not accepted. Accepted: {accepted()}.")
    return FILE_TYPES[ext]


def opens_inline(name: str) -> bool:
    return Path(name).suffix.lower() in INLINE


@dataclass(frozen=True)
class Stored:
    name: str
    key: str
    sha256: str
    size: int
    content_type: str
    check: object = None  # what `inspect` returned (the accessibility check, for comments on filings)


def store(folder: str, name: str, file, max_mb: int, *, inspect=None) -> Stored:
    """Check and keep one file (a Django UploadedFile or any open binary file) in the uploads area at `<folder>/<name>`. Refuses a type
    not in FILE_TYPES, an empty file and one over max_mb. `inspect(path, name)`, when given, sees the file on local disk before it is
    kept and may refuse it (raise FileRefused); what it returns is Stored.check."""
    name = clean_name(name)
    kind = content_type(name)
    digest, size = hashlib.sha256(), 0
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        if hasattr(file, "seek"):
            file.seek(0)
        chunks = file.chunks() if hasattr(file, "chunks") else iter(lambda f=file: f.read(1 << 20), b"")
        for chunk in chunks:
            digest.update(chunk)
            tmp.write(chunk)
            size += len(chunk)
    try:
        if size == 0:
            raise FileRefused(f"{name} is empty.")
        if size > max_mb * 1024 * 1024:
            raise FileRefused(f"{name} is larger than the {max_mb} MB a file may be (see Limits, /limits/).")
        checked = inspect(Path(tmp.name), name) if inspect else None
        key = f"{folder.rstrip('/')}/{name}"
        storage.store("uploads").put_file(key, Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return Stored(name, storage.clean_key(key), digest.hexdigest(), size, kind, checked)
