"""Several files in one filing upload (cedars.core.uploads, BR-06.3), and Budgets, Claims and Monthly reports taking them."""

import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from cedars.core import uploads

TABLES = ("Measure", "ProgramCost")


def table_of(name):
    return next((t for t in TABLES if name.strip().lower() == t.lower()), None)


def zipped(**files) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


def members(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n).decode() for n in z.namelist()}


def workbook(**sheets) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_one_zip_or_workbook_is_passed_through_unchanged():
    data = zipped(**{"Measure.csv": "CEInputID|PrgID\nM1|P\n", "ProgramCost.csv": "PrgID\nP\n"})
    c = uploads.combine([("job.zip", data)], table_of)
    assert (c.data, c.filename, c.sources, c.parts) == (data, "job.zip", [], {})


def test_parts_of_a_table_are_joined_in_the_order_chosen():
    files = [
        ("Measure_1.csv", b"CEInputID|PrgID|NumUnits\nM1|P|1\nM2|P|2\n"),
        ("Measure (2).csv", b"numunits,prgid,ceinputid\n3,P,M3\n"),  # comma-delimited, columns in another order and case
        ("ProgramCost.CSV", "PrgID|Note\nP|café\n".encode("cp1252")),
    ]
    c = uploads.combine(files, table_of)
    got = members(c.data)
    assert got["Measure.csv"] == "CEInputID,PrgID,NumUnits\nM1,P,1\nM2,P,2\nM3,P,3\n"
    assert got["ProgramCost.csv"] == "PrgID,Note\nP,café\n"
    assert c.parts == {"Measure": ["Measure_1.csv", "Measure (2).csv"]} and c.filename == "combined upload (3 files).zip"
    assert [s["name"] for s in c.sources] == [n for n, _ in files] and c.sources[0]["sha256"] == uploads.sha256(files[0][1])


def test_zips_workbooks_and_csv_files_combine():
    files = [
        ("first.zip", zipped(**{"Measure.csv": "CEInputID|NumUnits\nM1|1\n", "ProgramCost.csv": "PrgID\nP\n"})),
        ("second.xlsx", workbook(Measure=[["CEInputID", "NumUnits"], ["M2", 2.0]], Notes=[["read me"]])),
        ("third.csv", b"x"),  # names no table: kept under its own name, for the module's reader to refuse
    ]
    got = members(uploads.combine(files, table_of).data)
    assert got["Measure.csv"] == "CEInputID,NumUnits\nM1,1\nM2,2\n" and "Notes.csv" not in got and got["third.csv"] == "x"


def test_one_zip_holding_a_table_in_parts_is_joined():
    data = zipped(**{"job/Measure_1.csv": "CEInputID\nM1\n", "job/Measure_2.csv": "CEInputID\nM2\n", "job/ProgramCost.csv": "PrgID\nP\n"})
    c = uploads.combine([("job.zip", data)], table_of)
    assert members(c.data)["Measure.csv"] == "CEInputID\nM1\nM2\n" and c.filename == "combined upload (job.zip).zip"


def test_parts_with_different_columns_are_refused():
    files = [("Measure_1.csv", b"CEInputID|NumUnits\nM1|1\n"), ("Measure_2.csv", b"CEInputID|Units\nM2|1\n")]
    with pytest.raises(uploads.CombineError, match="Measure parts have different columns: Measure_2.csv has no NumUnits; also Units"):
        uploads.combine(files, table_of)
    with pytest.raises(uploads.CombineError, match="notes.docx is not a .zip, .xlsx or .csv"):
        uploads.combine([("Measure.csv", b"CEInputID\n"), ("notes.docx", b"x")], table_of)


def test_a_refused_name_refuses_the_upload_with_or_without_a_part_label():
    def refuse(name):
        if name.lower() == "secret":
            raise uploads.CombineError(f"{name} is not allowed")

    for files in (
        [("Secret_2.csv", b"a\n1\n"), ("Measure.csv", b"CEInputID\n")],
        [("w.xlsx", workbook(Secret=[["a"]])), ("Measure.csv", b"CEInputID\n")],
    ):
        # (one .zip or .xlsx alone goes to the module unchanged, whose own reader refuses the name)
        with pytest.raises(uploads.CombineError, match="is not allowed"):
            uploads.combine(files, table_of, refuse=refuse)


def test_the_form_field_takes_several_files():
    field = uploads.MultipleFileField()
    files = [SimpleUploadedFile(f"Measure_{i}.csv", b"CEInputID\n") for i in range(3)]
    assert [f.name for f in field.clean(files)] == ["Measure_0.csv", "Measure_1.csv", "Measure_2.csv"]
    assert len(field.clean(files[0])) == 1
    with pytest.raises(Exception, match="at most 20 files"):
        field.clean([SimpleUploadedFile(f"m{i}.csv", b"x") for i in range(21)])


def split(data: bytes) -> list[SimpleUploadedFile]:
    """A one-zip upload sent as its CSV files, the Measure table in two parts."""
    out = []
    for name, text in members(data).items():
        if name == "Measure.csv":
            head, *rows = text.strip("\n").split("\n")
            out += [SimpleUploadedFile("Measure_1.csv", (head + "\n" + rows[0] + "\n").encode())]
            out += [SimpleUploadedFile("Measure_2.csv", (head + "\n" + "\n".join(rows[1:]) + "\n").encode())]
        else:
            out.append(SimpleUploadedFile(name, text.encode()))
    return out
