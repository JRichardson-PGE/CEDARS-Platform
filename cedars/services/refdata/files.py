"""Opening one file of a registered data pack, wherever the pack's storage_uri says its files are.

A pack is registered three ways: a module export saves its files in default storage under a folder (storage_uri is that folder, as the
Impact profiles module's library export does), `register_pack` records a folder on the server (a file:// address) and a pack copied to
object storage has an s3:// address. Every reader of a pack's files (profiles.py, acc.py) opens them here, as a seekable file Parquet
can read a row group at a time from.
"""

from urllib.parse import urlparse
from urllib.request import url2pathname

from django.core.files.storage import default_storage

from .models import PackVersion


def manifest_name(pack: PackVersion, basename: str) -> str | None:
    """The manifest path of the pack's file called `basename` (a pack registered from a whole reference folder keeps the folder's
    relative paths, such as acc/2024/electric_hourly.parquet; one registered from the file's own folder has the bare name)."""
    for name in sorted(pack.manifest or {}):
        if name == basename or name.endswith("/" + basename):
            return name
    return None


def open_file(pack: PackVersion, name: str):
    """The pack's file `name` (a manifest path), opened for reading in binary."""
    base = (pack.storage_uri or "").rstrip("/")
    if base.startswith("file://"):
        return open(url2pathname(urlparse(f"{base}/{name}").path), "rb")
    if base.startswith("s3://"):
        from pyarrow import fs

        filesystem, path = fs.FileSystem.from_uri(f"{base}/{name}")
        return filesystem.open_input_file(path)
    return default_storage.open(f"{base}/{name}", "rb")
