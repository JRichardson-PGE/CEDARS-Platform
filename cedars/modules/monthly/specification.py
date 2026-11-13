"""The monthly report layout as this module's code has it (upload.py, the Commitments fields, the engine's input tables in the field
catalog), for the Monthly reports specification's starting version (specifications.register_layout). Until filings read their
version, a change here changes the starting document: tests/test_specifications.py fails until it is made again and published as a
new version."""

from cedars.services.specifications import service as specifications

from . import upload
from .models import COMMITMENT_FIELDS
from .service import COMMITMENT_KEYS

KEYS = {"Commitments": ["PrgID", "YearMonth"], "Measure": ["CEInputID"], "ProgramCost": ["PrgID", "PrgYear"]}
KEY_TYPES = {"YearMonth": "year_month"}


def layout() -> dict:
    retired = "; ".join(f"{c} ({why})" for c, why in upload.RETIRED.items())
    commitments = [
        specifications.field_doc(f, requirement="required", type=KEY_TYPES.get(f, "text"), definition=d) for f, d in COMMITMENT_KEYS
    ]
    commitments += [specifications.field_doc(f, requirement="required", type="number", definition=d) for f, d in COMMITMENT_FIELDS]
    commitments.append(specifications.field_doc("Comments", definition="Optional: anything the CPUC should know about the row."))
    tables = [
        {
            "name": "Commitments",
            "required": True,
            "keys": KEYS["Commitments"],
            "refuse_without": list(upload.REQUIRED["Commitments"]),
            "guidance": "One row per program with commitments. Columns of CEDARS' monthly Program table that are no longer read are "
            f"ignored with a notice: {retired}.",
            "fields": commitments,
        }
    ]
    tables += [
        {
            "name": t,
            "required": True,
            "keys": KEYS[t],
            "refuse_without": list(upload.REQUIRED[t]),
            "guidance": "The year's BCA inputs so far: the month's spend is read from them and they are priced for the estimates.",
            "catalog": {"module": "calculations", "table": t},
        }
        for t in upload.BCA_TABLES
    ]
    return {
        "specification": {
            "name": "Monthly reports",
            "about": "The report each program administrator files every month: its commitments and its BCA inputs for the year so far. "
            "The 13th-month report is generated, not uploaded, so it has no specification.",
            "position": 3,
        },
        "version": {
            "name": "Starting version",
            "summary": "Generated from what the platform checks today: the Commitments table, the engine's Measure and ProgramCost input "
            "tables, the columns an upload is refused without and the built-in checks. Monthly reports have no rule set yet.",
            "source": "The monthly module's layout (upload.py), the pinned engine's field catalog and the built-in checks",
        },
        "tables": tables,
    }
