"""Loading programs from CEDARS' Programs list export (the layout ReCET ships as data/programs/cedars_programs_*.csv)."""

import csv
import io
from datetime import date

from django.db import transaction

from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation

from . import service
from .models import Program

IOUS = {"PGE", "SCE", "SCG", "SDGE"}
TRUE = {"true", "1", "yes", "y", "t"}
FLAG_COLUMNS = [
    "Resource_Flag",
    "NonResource_Flag",
    "Deemed_Flag",
    "Custom_Flag",
    "Upstream_Flag",
    "Midstream_Flag",
    "Downstream_Flag",
    "DirectInstall",
    "Audit_Flag",
    "Financing",
]
BEFORE_RECORDS = date(2000, 1, 1)  # a program with no StartYear is treated as in force from before any record here


def parse_sectors(text: str) -> dict[str, float]:
    """'Commercial: 0.900000, Industrial: 0.100000' -> {'Commercial': 0.9, 'Industrial': 0.1}."""
    out = {}
    for part in (text or "").split(","):
        name, _, share = part.partition(":")
        if name.strip() and share.strip():
            out[name.strip()] = float(share)
    return out


def _year(v: str) -> int | None:
    v = (v or "").strip()
    return int(float(v)) if v else None


@transaction.atomic
def import_cedars_programs(content: bytes, actor=None) -> dict[str, int]:
    """Add every program not yet in the registry, as its first version. Organisations missing for a PA code are added (IOUs typed as
    such; the rest left for the CPUC to type). Existing programs are left alone: changes go through service.change_program."""
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    existing = set(Program.objects.values_list("prg_id", flat=True))
    orgs = {o.code: o for o in Organisation.objects.all()}
    added = skipped = new_orgs = 0
    for r in rows:
        prg_id, pa = r["PrgID"].strip(), r["PA"].strip().upper()
        if not prg_id or prg_id in existing:
            skipped += 1
            continue
        statewide = pa == "SW"
        org = None
        if not statewide:
            org = orgs.get(pa)
            if org is None:
                org = access.create_organisation(actor, code=pa, name=pa, kind="pa", pa_type="IOU" if pa in IOUS else "")
                orgs[pa] = org
                new_orgs += 1
        start = _year(r.get("StartYear", ""))
        service.create_program(
            actor,
            prg_id=prg_id,
            administrator=org,
            statewide=statewide,
            valid_from=date(start, 1, 1) if start else BEFORE_RECORDS,
            reason="imported from CEDARS' Programs list",
            name=r.get("ProgramName", "").strip() or prg_id,
            status=r.get("ProgramStatus", "").strip(),
            segment=r.get("ProgramSegment", "").strip(),
            primary_sector=r.get("PrimarySector", "").strip(),
            sectors=parse_sectors(r.get("BPSector", "")),
            implementer=r.get("ProgramImplementer", "").strip(),
            category=r.get("ProgramCategory", "").strip(),
            implementation_contractor=r.get("ImplementationContractor", "").strip(),
            start_year=start,
            end_year=_year(r.get("EndYear", "")),
            flags={c: r.get(c, "").strip() for c in FLAG_COLUMNS if r.get(c, "").strip()},
            statewide_program=r.get("StatewideProgram", "").strip(),
            parent_program=r.get("ParentProgram", "").strip(),
            exclude_from_budget=r.get("Exclude_From_Budget", "").strip().lower() in TRUE,
            exclude_from_ce=r.get("Exclude_From_CE", "").strip().lower() in TRUE,
            exclude_from_statewide=r.get("Exclude_From_Statewide_Flag", "").strip().lower() in TRUE,
        )
        existing.add(prg_id)
        added += 1
    return {"added": added, "skipped": skipped, "organisations_added": new_orgs}
