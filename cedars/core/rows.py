"""Table rows held compactly. A filing's tables are read into memory as text rows to be checked, and a large filing has a million records
or more in a table: as one dict per row, each with its own copies of the text, a million claims took about 26 KB of memory each. Here
a row is a `Row`, a mapping that reads like a dict but keeps only a list of its values against its table's one shared column index, and
a value that repeats (a code, a date, a blank, an ID across tables) is one string shared by every row that has it. Reading streams
each table's lines from the file instead of decoding the whole file and splitting it first.

`read_csv` reads one table's CSV (pipe-, tab- or comma-delimited, UTF-8 or else Windows-1252, as every filing reader takes them);
`write_csv` writes rows into a .zip member as they are produced, without building the whole file as text first."""

from __future__ import annotations

import csv
import io
import itertools
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from typing import IO


class Row(MutableMapping):
    """One record, as text keyed by column name. It reads like a dict (`get`, `[]`, `in`, `items`, `{**row}`, `dict(row)`); its values
    are a list against an index {column: position} that every row of its table shares. Setting a column the row does not have gives
    that row its own index."""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: list[str]):
        self._index = index
        self._values = values

    @classmethod
    def of(cls, mapping: Mapping[str, str]) -> Row:
        return cls({k: i for i, k in enumerate(mapping)}, list(mapping.values()))

    def __getitem__(self, key: str) -> str:
        return self._values[self._index[key]]

    def get(self, key: str, default=None):
        i = self._index.get(key)
        return default if i is None else self._values[i]

    def __contains__(self, key) -> bool:
        return key in self._index

    def __iter__(self) -> Iterator[str]:
        return iter(self._index)

    def __len__(self) -> int:
        return len(self._index)

    def __setitem__(self, key: str, value: str) -> None:
        i = self._index.get(key)
        if i is None:
            self._index = {**self._index, key: len(self._values)}
            self._values = [*self._values, value]
        else:
            self._values[i] = value

    def __delitem__(self, key: str) -> None:
        if key not in self._index:
            raise KeyError(key)
        kept = [(k, self._values[i]) for k, i in self._index.items() if k != key]
        self._index = {k: n for n, (k, _) in enumerate(kept)}
        self._values = [v for _, v in kept]

    def keys(self):
        return self._index.keys()

    def values(self) -> list[str]:
        return [self._values[i] for i in self._index.values()]

    def items(self) -> list[tuple[str, str]]:
        return [(k, self._values[i]) for k, i in self._index.items()]

    def copy(self) -> dict[str, str]:
        return dict(self.items())

    def __eq__(self, other) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return NotImplemented

    __hash__ = None  # type: ignore[assignment] - mutable, as a dict is

    def __repr__(self) -> str:
        return f"Row({dict(self.items())!r})"

    def __reduce__(self):
        return (Row, (dict(self._index), list(self._values)))


class Shared:
    """One string for each distinct value read: `text(value)` gives back the copy already held, if there is one. Kept for one read
    (an upload's tables together, so an ID in several tables is held once) and then dropped."""

    def __init__(self):
        self._seen: dict[str, str] = {}

    def text(self, value: str) -> str:
        return self._seen.setdefault(value, value)


def rows(header: list[str | None], lines: Iterable[list[str]], shared: Shared | None = None, *, skip_blank: bool = True) -> list[Row]:
    """`lines` (the cells of each line after the header) as rows under `header`'s names, values trimmed; a column named None is left
    out, a line with no text in any cell is skipped (`skip_blank`), a short line's missing cells are blank. A name given twice keeps the
    last such column's value, as a dict built from the header would."""
    shared = shared or Shared()
    keep = [i for i, n in enumerate(header) if n is not None]
    index: dict[str, int] = {}
    for pos, i in enumerate(keep):
        index[header[i]] = pos  # type: ignore[index] - not None
    text = shared.text
    width = len(header)
    out = []
    for r in lines:
        if skip_blank and (not r or not any(c.strip() for c in r)):
            continue
        if len(r) >= width:
            out.append(Row(index, [text(r[i].strip()) for i in keep]))
        else:
            out.append(Row(index, [text(r[i].strip()) if i < len(r) else "" for i in keep]))
    return out


def _delimiter(first_line: str) -> str:
    return "|" if "|" in first_line else ("\t" if "\t" in first_line else ",")


def _lines(f: IO[str]) -> Iterator[list[str]]:
    """The cells of each line of a delimited text file: the delimiter is read from the first line (a pipe, else a tab, else a comma)."""
    first = f.readline()
    yield from csv.reader(itertools.chain([first], f), delimiter=_delimiter(first.split("\n", 1)[0]))


def read_csv(
    source: bytes | Callable[[], IO[bytes]],
    shared: Shared | None = None,
    *,
    names: Callable[[list[str]], list[str | None]] | None = None,
    skip_blank_header: bool = True,
) -> tuple[list[str], list[Row]]:
    """One table's delimited text as (header, rows): the header's cells trimmed, rows under the names `names(header)` gives (default:
    the header itself), values trimmed, blank lines skipped (before the header too, unless `skip_blank_header` is False). `source` is
    the file's bytes or a function opening it as a binary stream (a .zip member, read without decompressing it whole). UTF-8 (with or
    without a byte-order mark), else the whole file as Windows-1252."""
    for encoding in ("utf-8-sig", "cp1252"):
        raw = io.BytesIO(source) if isinstance(source, bytes | bytearray) else source()
        try:
            lines = _lines(io.TextIOWrapper(raw, encoding=encoding, newline=""))
            header: list[str] = []
            for cells in lines:
                header = [h.strip() for h in cells]
                if any(header) or not skip_blank_header:
                    break
            if not any(header) and skip_blank_header:
                return [], []
            return header, rows(names(header) if names else header, lines, shared)
        except UnicodeDecodeError:
            if encoding == "cp1252":
                raise
        finally:
            raw.close()
    raise AssertionError("unreachable")  # pragma: no cover


def count_rows(content: bytes) -> int:
    """How many records a delimited table holds: its lines after the header with text in some cell, as `read_csv` reads them."""
    return len(read_csv(content, skip_blank_header=False, names=lambda h: [None] * len(h))[1])


def read_header(content: bytes) -> list[str]:
    """A delimited table's header cells, trimmed, read from its first line alone (UTF-8, else Windows-1252)."""
    end = content.find(b"\n")
    first = content if end < 0 else content[:end]
    try:
        line = first.decode("utf-8-sig")
    except UnicodeDecodeError:
        line = first.decode("cp1252")
    return [h.strip() for h in next(csv.reader([line], delimiter=_delimiter(line)), [])]


def write_csv(z: zipfile.ZipFile, member: str, columns: list[str], lines: Iterable[Iterable[str]]) -> None:
    """Write a comma-delimited CSV into .zip `z` as member `member`: the `columns` line, then each of `lines` (each line's cells, in
    the columns' order), streamed into the archive as written."""
    with z.open(member, "w", force_zip64=True) as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        w.writerows(lines)
