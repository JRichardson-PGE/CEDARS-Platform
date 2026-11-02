"""A zip file written as it is sent: `stream(entries)` yields the archive's bytes while it reads each entry's source, so a run's
whole output (hundreds of MB for a large run) is never held in memory or written to the web server's disk. Entries that are
already compressed (zip, parquet, xlsx) are stored, the rest deflated; every entry may pass 4 GB (ZIP64)."""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator

CHUNK = 1 << 20
STORED = (".zip", ".parquet", ".xlsx", ".xlsm", ".gz", ".png", ".jpg")


class _Sink(io.RawIOBase):
    """Where ZipFile writes: kept until the stream takes it. Not seekable, so ZipFile writes data descriptors."""

    def __init__(self):
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self._chunks.append(bytes(b))
        return len(b)

    def take(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def stream(entries: Iterable[tuple[str, Callable[[], io.IOBase]]]) -> Iterator[bytes]:
    """The zip of `entries` ((name in the archive, a function that opens its source for reading)), in pieces."""
    sink = _Sink()
    now = time.localtime()[:6]
    with zipfile.ZipFile(sink, "w") as zf:
        for name, opener in entries:
            info = zipfile.ZipInfo(name, date_time=now)
            info.compress_type = zipfile.ZIP_STORED if name.lower().endswith(STORED) else zipfile.ZIP_DEFLATED
            src = opener()
            try:
                with zf.open(info, "w", force_zip64=True) as dest:
                    while True:
                        block = src.read(CHUNK)
                        if not block:
                            break
                        dest.write(block)
                        data = sink.take()
                        if data:
                            yield data
            finally:
                src.close()
            data = sink.take()
            if data:
                yield data
    yield sink.take()
