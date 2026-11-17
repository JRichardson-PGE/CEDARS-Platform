"""Every column of a large year (the demo's 500 on "Start from: Every column"): sorting every chosen column of every record at once ran
DuckDB past its query memory. The records are sorted on their keys alone and their columns read per file, a chunk at a time."""

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from cedars.modules.data import query as dq
from cedars.modules.data import records

pytestmark = pytest.mark.django_db
ROWS, NUMBERS, TEXTS = 20_000, 150, 30


@pytest.fixture
def wide(tmp_path):
    """Three PAs' files of wide records, in no particular CEInputID order, one with a column the others lack and its own type."""
    rng = np.random.default_rng(7)
    files, tables = [], []
    for i, pa_code in enumerate(("PGE", "SCE", "SDGE")):
        ids = [f"{pa_code}-{n:06d}" for n in rng.permutation(ROWS)]
        cols = {"CEInputID": ids, "Used_Year": ["2026"] * ROWS, "CreditedPA": [pa_code] * ROWS}
        cols |= {f"N{j}": rng.random(ROWS) for j in range(NUMBERS)}
        cols |= {f"T{j}": [f"text {v}" for v in rng.integers(0, 10_000, ROWS)] for j in range(TEXTS)}
        if i == 1:
            cols["OnlySCE"] = rng.integers(0, 9, ROWS)  # integers here, blank in the other files
        t = pa.table(cols)
        path = tmp_path / f"{pa_code}.parquet"
        pq.write_table(t, path, row_group_size=10_000)
        files.append({"uri": path.as_uri(), "sha256": pa_code, "organisation": pa_code, "cycle": "", "kind": "Annual claim"})
        tables.append(t.append_column("_org", pa.array([pa_code] * ROWS)))
    whole = pa.concat_tables(tables, promote_options="default")
    order = pc.sort_indices(whole, [("CEInputID", "ascending"), ("_org", "ascending")])
    return files, whole.take(order)


def test_every_column_of_a_large_year_pages_and_downloads_within_the_query_memory(wide, settings, monkeypatch):
    files, expected = wide
    settings.DATA_QUERY_MEMORY = "96MB"
    q = dq.Query(dataset="claims", metrics=("tsb",), year_from=2026, year_to=2026, exclude=())
    cat = records.catalog(files)
    columns = records.chosen(cat, {"columns": ["all"]})[0]
    assert len(columns) == 3 + 3 + NUMBERS + TEXTS + 1  # the source columns, the keys, every number and text, OnlySCE

    sql, args = records._sql(q, files, cat, columns)  # the old way: every column of every record sorted at once
    con = dq.connect()
    with pytest.raises(duckdb.OutOfMemoryException):
        con.execute(sql + " LIMIT 25 OFFSET 40000", args).fetchall()
    con.close()

    def want(i):
        r = {c: expected[c][i].as_py() for c in expected.column_names}
        r |= {"FiledBy": r["_org"], "Filing": "Annual claim", "CountedPA": r["_org"]}
        return [r.get(c) for c in columns]

    page = records.rows(q, files, cat, columns, limit=25, offset=40_000)  # a page deep in the third PA's records
    assert page == [want(40_000 + i) for i in range(25)]

    monkeypatch.setattr(records, "CHUNK_CELLS", 1_000_000)  # several chunks
    schema, stream = records.batches(q, files, cat, columns)
    got = pa.Table.from_batches(list(stream), schema)
    assert got.num_rows == 3 * ROWS and got.column_names == columns
    assert got["CEInputID"].to_pylist() == expected["CEInputID"].to_pylist()
    assert got["N7"].to_pylist() == expected["N7"].to_pylist() and got["T3"].to_pylist() == expected["T3"].to_pylist()
    assert got["OnlySCE"].to_pylist() == expected["OnlySCE"].to_pylist()  # blank outside SCE's file, SCE's integers inside
    assert got["FiledBy"].to_pylist() == expected["_org"].to_pylist()
