"""Compact rows (cedars.core.rows): a filing's tables are read into rows that read like dicts but hold a list of values against one
shared column index, and repeated values once; the readers stream each table's lines."""

import io
import pickle
import zipfile

import pytest

from cedars.core import rows as compact


def test_a_row_reads_and_changes_like_a_dict():
    _, (r, s) = compact.read_csv(b"ClaimID|PA|Note\nC1| PGE |\nC2|PGE|x\n")
    assert r == {"ClaimID": "C1", "PA": "PGE", "Note": ""} and {"ClaimID": "C1", "PA": "PGE", "Note": ""} == r
    assert r["PA"] == "PGE" and r.get("Missing", "") == "" and r.get("Missing") is None and "PA" in r and "Missing" not in r
    assert list(r) == ["ClaimID", "PA", "Note"] and len(r) == 3 and r.items() == [("ClaimID", "C1"), ("PA", "PGE"), ("Note", "")]
    assert {**r, "Note": "y"} == {"ClaimID": "C1", "PA": "PGE", "Note": "y"} and dict(r) == r.copy()
    with pytest.raises(KeyError):
        r["Missing"]
    r["Note"], r["Extra"] = "n", "e"  # a new column is that row's own: the table's other rows do not have it
    del r["PA"]
    assert r == {"ClaimID": "C1", "Note": "n", "Extra": "e"} and s == {"ClaimID": "C2", "PA": "PGE", "Note": "x"}
    assert pickle.loads(pickle.dumps(r)) == r


def test_repeated_values_are_held_once_across_tables():
    shared = compact.Shared()
    _, a = compact.read_csv(b"ClaimID,PA\nC1,PGE\nC2,PGE\n", shared)
    _, b = compact.read_csv(b"ClaimID,Site\nC1,S1\n", shared)
    assert a[0]["PA"] is a[1]["PA"] and a[0]["ClaimID"] is b[0]["ClaimID"]
    assert a[0]._index is a[1]._index  # one column index for the table


def test_reading_matches_the_readers_rules():
    # pipe, tab or comma from the header line; a byte-order mark dropped; blank lines skipped; short lines padded; Windows-1252 fallback
    assert compact.read_csv(b"\xef\xbb\xbfA\tB\n1\n\n \t \n2\t3\n") == (["A", "B"], [{"A": "1", "B": ""}, {"A": "2", "B": "3"}])
    assert compact.read_csv('A,B\r\nCafé,"x, y"\r\n'.encode("cp1252"))[1] == [{"A": "Café", "B": "x, y"}]
    assert compact.read_csv(b"") == ([], [])
    assert compact.read_csv(b"\n\nA,B\n1,2\n") == (["A", "B"], [{"A": "1", "B": "2"}])
    assert compact.read_csv(b"\nA,B\n1,2\n", skip_blank_header=False)[0] == []
    assert compact.read_csv(b"A,B,C\n1,2,3\n", names=lambda h: ["a", None, "c"])[1] == [{"a": "1", "c": "3"}]
    assert compact.read_header(b"A|B \n1|2\n") == ["A", "B"] and compact.count_rows(b"A\n1\n\n2\n") == 2


def test_a_zip_member_is_streamed_both_ways():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        compact.write_csv(z, "T.csv", ["A", "B"], ([str(i), "x,y"] for i in range(3)))
    z = zipfile.ZipFile(io.BytesIO(out.getvalue()))
    assert z.read("T.csv") == b'A,B\n0,"x,y"\n1,"x,y"\n2,"x,y"\n'
    assert compact.read_csv(lambda: z.open("T.csv"))[1][2] == {"A": "2", "B": "x,y"}


def test_a_rule_frame_built_from_rows_is_the_frame_read_from_their_csv():
    pd = pytest.importorskip("pandas")
    from cedars.modules.claims import upload
    from cedars.services.rules import service as rules

    pytest.importorskip("recet")
    _, rs = compact.read_csv(b"CEInputID,PA,E3MeaElecEndUseShape\nC1,PGE,\nC2,,DEER:Res\n")
    cols = ["CEInputID", "PA", "E3MeaElecEndUseShape"]
    pd.testing.assert_frame_equal(rules.frame(cols, rs), rules.read_table(upload.to_csv(cols, rs)))
