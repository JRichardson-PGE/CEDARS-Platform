"""Synthetic legacy CEDARS downloads: zips laid out as CEDARS publishes them (the real header rows, from the dictionary's
legacy_layouts.json) holding made-up records. CEDARS-Platform is public, so tests never use the real downloads or anything taken from them
beyond the column labels."""

import csv
import hashlib
import io
import zipfile
from pathlib import Path

from cedars.services.dictionary import legacy

MANIFEST_HEADER = ["dataset", "year", "pa", "filename", "url", "bytes", "sha256", "downloaded"]


def csv_text(layout: str, rows: list[dict] | None = None) -> str:
    """A CSV in the layout: its header, then each row's values by label (blank where a row leaves a label out)."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    header = legacy.layouts()[layout]
    w.writerow(header)
    for r in rows or []:
        w.writerow([r.get(c, "") for c in header])
    return out.getvalue()


def write_zip(path: Path, members: dict[str, str], macos_junk: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(name, text.encode("utf-8"))
            if macos_junk:
                z.writestr(f"__MACOSX/._{name}", b"junk")
    return path


def manifest_row(dataset: str, year: str, pa: str, path: Path) -> list[str]:
    data = path.read_bytes()
    url = f"https://cedars.example.org/reports/download-record-level-report/{pa}/{year}/{dataset}/"
    return [dataset, year, pa, path.name, url, str(len(data)), hashlib.sha256(data).hexdigest(), "yes"]


def manifest_text(rows: list[list[str]]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(MANIFEST_HEADER)
    w.writerows(rows)
    return out.getvalue()


def claim_row(claim_id: str, pa: str, prg_id: str, year: int = 2023, **values) -> dict:
    """A made-up claims record (layout labels), with a few savings and CET outputs."""
    return {
        "Claim ID": claim_id,
        "Year": str(year),
        "PA": pa,
        "Program ID": prg_id,
        "Program Name": f"Example program {prg_id}",
        "Claim Year Quarter": f"{year}Q2",
        "Number of Units": "2.0",
        "Unit kWh First Baseline": "100.0",
        "Total System Benefit": "50.25",
        "TRC Cost": "40.0",
        **values,
    }


def filing_row(
    filing_id: str, pa: str, prg_id: str, year: int, *, units: float = 2.0, admin: float = 100.0, marketing: float = 50.0, **values
) -> dict:
    """A made-up budget filing record (layout labels): two weighted cost columns, a rebate, CET outputs and the program columns."""
    wpc = admin + marketing
    rebate = 10.0 * units
    row = {
        "Filing ID": filing_id,
        "Year": str(year),
        "PA": pa,
        "Program ID": prg_id,
        "Program Name": f"Example program {prg_id}",
        "Program Segment": "Resource Acquisition",
        "Program Status": "Active",
        "Primary Sector": "Residential",
        "Resource Flag": "True",
        "Exclude From Budget Flag": "False",
        "Exclude from CE Flag": "False",
        "Filing Year Quarter": f"{year}Q1",
        "Number of Units": f"{units:.15f}",
        "Unit End User Rebate": "10.000000000000000",
        "End User Rebate": f"{rebate}",
        "Weighted Admin Costs Overhead and GA": f"{admin}",
        "Weighted Marketing Outreach": f"{marketing}",
        "Weighted Program Cost": f"{wpc}",
        "Budget": f"{wpc + rebate}",
        "Total System Benefit": "500.5",
        "TRC Cost": "200.0",
        "PAC Cost": "100.0",
        "TRC": "2.5",
        "PAC": "4.0",
        "TRC Benefits": "500.0",
        "PAC Benefits": "400.0",
        "First Year Net kWh": "1000",
        "Levelized TRC Cost": "0.12",
    }
    row.update(values)
    return row
