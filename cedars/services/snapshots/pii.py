"""The publication scan for personal information (BR-08.5): every text column of a snapshot is searched for values that look like
an email address, a phone number, a street address, a Social Security number or an account number before the snapshot joins a public
release. A hit holds the publication (service.py); this module only finds and describes.

The patterns are code (they are how the scan reads text, like a parser); which ones run and which columns are left out are the CPUC's
settings (`ScanSettings`, the Publication scan page), changed with a reason and audited.

The scan reads the Parquet file a row group at a time, matches each column's DISTINCT values (pyarrow's RE2 matcher), and counts the
rows only for the values that matched, so a million-row file costs a few seconds. What it keeps never holds the values themselves:
the main database must not hold personal information (it belongs in the vault), so a finding names the column, the pattern, how many
records, up to `RECORDS` record IDs (CEInputID, else the row number) and up to `EXAMPLES` examples masked to their first character
and their punctuation ("j***@e******.c**")."""

import re
from dataclasses import dataclass, field
from pathlib import Path

RECORDS = 5
EXAMPLES = 3
ID_COLUMN = "CEInputID"

# Before and after a number: not a letter, digit, underscore, dot or dash, so IDs such as "PGE-2028-000123456" and decimals such as
# "0.123456789" are not numbers on their own.
_B = r"(?:^|[^0-9A-Za-z_.-])"
_E = r"(?:[^0-9A-Za-z_.-]|$)"
_SUFFIX = (
    "street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way|court|ct|place|pl|parkway|pkwy|highway|hwy|circle|cir|terrace|ter"
)


@dataclass(frozen=True)
class Pattern:
    code: str
    label: str  # "look like ...": what a finding says the values look like
    regex: str  # RE2 (pyarrow) and Python re both read it
    ignore_case: bool = False


PATTERNS = (
    Pattern("email", "email addresses", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    Pattern("phone", "phone numbers", _B + r"(?:\+?1[ .-]?)?(?:\([2-9][0-9]{2}\) ?|[2-9][0-9]{2}[ .-]?)[2-9][0-9]{2}[ .-][0-9]{4}" + _E),
    Pattern(
        "street_address",
        "street addresses",
        r"(?:^|[^0-9a-z])(?:[0-9]{1,6}[a-z]?(?: [a-z0-9'.-]+){1,4} (?:" + _SUFFIX + r")\.?(?:[^a-z0-9]|$)|p\.? ?o\.? box [0-9]+)",
        ignore_case=True,
    ),
    Pattern("ssn", "Social Security numbers", _B + r"[0-9]{3}-[0-9]{2}-[0-9]{4}" + _E),
    Pattern("account_number", "account numbers (9 or more digits)", _B + r"(?:[0-9]{9,19}|[0-9]{4}[ -][0-9]{4}[ -][0-9]{4,7})" + _E),
)
BY_CODE = {p.code: p for p in PATTERNS}
DEFAULT_EXEMPT = ("CEInputID", "PrgID")  # keys the platform assigns or registers; never personal information


@dataclass
class Finding:
    column: str
    pattern: str
    records: int = 0
    examples: list[str] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        p = BY_CODE.get(self.pattern)
        return p.label if p else self.pattern

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "pattern": self.pattern,
            "records": self.records,
            "examples": self.examples,
            "record_ids": self.record_ids,
        }


@dataclass
class Result:
    findings: list[Finding]
    rows: int
    columns: list[str]  # the text columns searched
    skipped: list[str]  # text columns left out by the settings
    patterns: list[str]
    error: str = ""

    @property
    def clear(self) -> bool:
        return not self.findings and not self.error


def mask(text: str) -> str:
    """Every digit but the value's first character and every letter but each word's first as *; punctuation and spaces kept, so a
    finding shows the shape of what matched ("j***@e******.c**", "4**-***-****") without the value."""
    out, first = [], True
    for i, ch in enumerate(text):
        if ch.isascii() and ch.isalnum():
            word_start = i == 0 or not (text[i - 1].isascii() and text[i - 1].isalnum())
            out.append(ch if first or (ch.isalpha() and word_start) else "*")
            first = False
        else:
            out.append(ch)
    return "".join(out)


def _example(pattern: Pattern, value: str) -> str:
    m = re.search(pattern.regex, value, re.IGNORECASE if pattern.ignore_case else 0)
    found = (m.group(0) if m else value).strip(" ,;:")
    return mask(found[:80])


def _text_columns(schema) -> list[str]:
    import pyarrow as pa

    out = []
    for f in schema:
        t = f.type.value_type if pa.types.is_dictionary(f.type) else f.type
        if pa.types.is_string(t) or pa.types.is_large_string(t):
            out.append(f.name)
    return out


def scan(path: Path | str, *, patterns: list[str] | None = None, exempt: list[str] | None = None) -> Result:
    """Search the file's text columns. `patterns`: the codes that run (default all); `exempt`: column names left out (compared without
    case). Raises on an unreadable file; the caller holds the publication then."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    chosen = [BY_CODE[c] for c in (patterns if patterns is not None else list(BY_CODE)) if c in BY_CODE]
    left_out = {c.strip().lower() for c in (exempt if exempt is not None else DEFAULT_EXEMPT) if c.strip()}
    pf = pq.ParquetFile(path)
    text = _text_columns(pf.schema_arrow)
    columns = [c for c in text if c.lower() not in left_out]
    skipped = [c for c in text if c.lower() in left_out]
    has_id = ID_COLUMN in pf.schema_arrow.names
    findings: dict[tuple[str, str], Finding] = {}
    offset = 0
    if columns and chosen:
        read = list(dict.fromkeys(columns + ([ID_COLUMN] if has_id else [])))
        for rg in range(pf.num_row_groups):
            t = pf.read_row_group(rg, columns=read)
            ids = t.column(ID_COLUMN) if has_id else None
            for col in columns:
                arr = t.column(col).combine_chunks()
                if pa.types.is_dictionary(arr.type):
                    arr = arr.cast(pa.large_string())
                distinct = pc.unique(arr).drop_null()
                if len(distinct) == 0:
                    continue
                for p in chosen:
                    hits = distinct.filter(pc.match_substring_regex(distinct, p.regex, ignore_case=p.ignore_case))
                    if len(hits) == 0:
                        continue
                    rows = pc.is_in(arr, value_set=hits)
                    f = findings.setdefault((col, p.code), Finding(col, p.code))
                    f.records += pc.sum(rows).as_py() or 0
                    for v in hits.to_pylist():
                        if len(f.examples) >= EXAMPLES:
                            break
                        ex = _example(p, v)
                        if ex not in f.examples:
                            f.examples.append(ex)
                    if len(f.record_ids) < RECORDS:
                        idx = pc.indices_nonzero(rows).to_pylist()[: RECORDS - len(f.record_ids)]
                        f.record_ids += [str(ids[i].as_py()) if ids is not None else f"row {offset + i + 1}" for i in idx]
            offset += t.num_rows
    order = {p.code: i for i, p in enumerate(PATTERNS)}
    out = sorted(findings.values(), key=lambda f: (columns.index(f.column), order[f.pattern]))
    return Result(out, pf.metadata.num_rows, columns, skipped, [p.code for p in chosen])
