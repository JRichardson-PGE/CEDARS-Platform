"""The PSR migration (BR-15.10): every Project Status Report project as a study in the register, under its PSR project number and
study ID, with its lists mapped onto the register's controlled lists, read from the PSR's own public API (/api/projects, the JSON its
pages use) saved to a file or read from the site.

The PSR's lists are free text, so each value is mapped:
  - study lead ED is CPUC, IOU and REN are themselves;
  - status, sectors, group (sub-sector), funding cycles and study type are matched to the register's option of the same label,
    ignoring case, spaces and punctuation ("Cross-Cutting" is "Cross Cutting"), plus the short study types ("Impact", "Process",
    "Market") the PSR collected over the years;
  - the study manager (65 free-text values mixing firms, PAs and people) is matched to an organization by its code or name, and by
    the usual spellings of the PAs (PG&E, SoCalGas, ...);
  - a map file (--map, a CSV of field, psr_value, register_value; --write-map writes one with every value the PSR holds and what it
    maps to now) adds or overrides any of these, so CPUC ED settles the mapping without a code change.
A value that maps to nothing is left off the study (a manager: the CPUC, which assigns the study later) and kept as the PSR had it in
Study.psr, shown with the contacts, never publicly. The contacts (ED program manager, work order lead, ED consultants, the PA
contacts, the PCG) go to the study's contacts. A project already in the register is skipped, so the import can be run again;
--dry-run maps everything and stores nothing. Run the PDA import first: a project's WONum is linked to the work order the PDA gave.

Field meanings, checked against the capture of 2026-10-30 (740 projects; CEDARS-Research notes/pda_psr_capture_findings.md):
  - studyLeads and projectStatus are objects ({studyLeadID, name}, {projectStatusID, projectStatus}; 196 statuses are {}), sectors
    and fundingCycles lists of objects ({sectorID, name}); the other fields are strings, numbers or null;
  - studyID is <first funding cycle>_<first sector>_<projectID> (a cycle range or an empty sector in 492 of them) and is kept as given;
  - WONum is a CPUC contract number ("23NC0465", 79 projects), else an older or a PA's own number ("1028", "3C-REN_010"), or N/A, NA,
    TBD: it links to a work order already listed under that number, and a new work order is added only for a CPUC contract number;
  - planPostedToPDA is a Yes / date note, not a link; calmacID is N/A, NA or TBD on all but 26 projects;
  - completionDate is a UTC midnight (the date as entered); nextActivityDueDate a Pacific midnight in UTC ("...T07:00:00Z");
  - there is no creation date: a study's is its updatedAt (null on 251 projects: the import's date);
  - projectDesc runs past the form's 1,000 characters on 25 projects (kept whole)."""

from __future__ import annotations

import csv
import html
import json
import re
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation

from . import studies
from .models import AttributeOption, Study, WorkOrder
from .pda_import import _when
from .service import DocumentError

LABEL = "PSR migration"
TIMEOUT = 120

# PSR field -> register field, for the lists
LIST_FIELDS = {
    "lead": ("studyLeads", "study_lead"),
    "status": ("projectStatus", "study_status"),
    "study_type": ("studyType", "study_type"),
    "group": ("sub_sector", "study_group"),
    "sectors": ("sectors", "sector"),
    "funding_cycles": ("fundingCycles", "portfolio_cycle"),
}
TEXT_FIELDS = {
    "description": "projectDesc",
    "objective": "objective",
    "research_questions": "researchQuestions",
    "methods": "potentialEMVMethods",
    "notes": "notes",
}
CONTACT_FIELDS = [
    ("WOLead", "Work order lead"),
    ("EDConsultants", "ED consultants"),
    ("PGEContact", "PG&E contact"),
    ("SCEContact", "SCE contact"),
    ("SDGEContact", "SDG&E contact"),
    ("SCGContact", "SoCalGas contact"),
    ("projectCoordinationGroup", "Project coordination group"),
    ("PCGCoordinator", "PCG coordinator"),
    ("PCGEDRep", "PCG ED representative"),
    ("PCGOtherED", "PCG other ED"),
]
# PSR values kept as they were (no field of their own here)
KEPT_AS_IS = [("planPostedToPDA", "Plan posted to the PDA"), ("xRefNum", "Cross-reference number")]
NONE = {"", "na", "none", "tbd", "tba", "null"}
CPUC_CONTRACT = re.compile(r"\d{2}[A-Z]{2}\d{4}")  # 23NC0465, 17PS5017

# The usual ways the PSR names a study lead, a study type or a PA (normalized: letters and digits only, lower case)
BUILTIN = {
    "lead": {"ed": "CPUC", "cpuc": "CPUC", "cpuced": "CPUC", "energydivision": "CPUC", "iou": "IOU", "ren": "REN"},
    "study_type": {
        "impact": "Impact Evaluation",
        "impactevaluation": "Impact Evaluation",
        "process": "Process Evaluation",
        "processevaluation": "Process Evaluation",
        "market": "Market Study",
        "marketstudy": "Market Study",
        "marketeffects": "Market Effects",
        "other": "Other",
    },
    # A manager is matched by its organization's code or name; these are the other spellings the PSR holds for the same one.
    "manager": {
        "pacificgasandelectric": "PGE",
        "southerncaliforniaedison": "SCE",
        "sandiegogasandelectric": "SDGE",
        "socalgas": "SCG",
        "southerncaliforniagas": "SCG",
        "ed": "CPUC",
        "cpuced": "CPUC",
        "energydivision": "CPUC",
        "dnvgl": "DNV",
        "odc": "Opinion Dynamics",
        "evergreen": "Evergreen Economics",
    },
}
MAP_FIELDS = ["lead", "status", "study_type", "group", "sectors", "funding_cycles", "manager"]


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _labels(value) -> list[str]:
    """A PSR value as its labels: a string, a list of strings, or objects holding one (the junction records /api/projects/:id adds)."""
    if value is None:
        return []
    if isinstance(value, str | int | float):
        text = " ".join(html.unescape(str(value)).split())
        return [text] if text else []
    if isinstance(value, dict):
        for key in ("name", "projectStatus", "label", "desc", "description", "value", "title"):
            if isinstance(value.get(key), str | int):
                return _labels(value[key])
        texts = [v for k, v in value.items() if isinstance(v, str) and not k.lower().endswith(("id", "at"))]
        return _labels(texts[0]) if len(texts) == 1 else []
    if isinstance(value, list | tuple):
        return [label for v in value for label in _labels(v)]
    return []


def _text(record: dict, key: str) -> str:
    value = record.get(key)
    return "" if value is None else html.unescape(str(value)).strip()


def _none(text: str) -> bool:
    return norm(text) in NONE


def _psr_day(value) -> date | None:
    """A PSR date: a UTC midnight is the date as entered; any other time is a Pacific date stored in UTC."""
    dt = _when(value)
    if dt is None:
        return None
    utc = dt.astimezone(UTC)
    return utc.date() if utc.time() == time() else timezone.localdate(dt)


# --- the mapping


class Mapping:
    """What each PSR value becomes here: the map file's rows first, then the built-in spellings, then a matching label or code."""

    def __init__(self, rows: list[dict] | None = None):
        self.overrides: dict[str, dict[str, str]] = defaultdict(dict)
        for row in rows or []:
            f, psr_value, value = (row.get(k, "").strip() for k in ("field", "psr_value", "register_value"))
            if f not in MAP_FIELDS:
                raise DocumentError(f"The map file names a field {f!r}: use one of {', '.join(MAP_FIELDS)}.")
            if psr_value:
                self.overrides[f][norm(psr_value)] = value
        self.options = {
            code: {norm(o.label): o for o in AttributeOption.objects.filter(attribute_list__code=code)} for _, code in LIST_FIELDS.values()
        }
        self.organisations = {}
        for o in Organisation.objects.filter(active=True):
            self.organisations.setdefault(norm(o.code), o)
            self.organisations.setdefault(norm(o.name), o)
        self.cpuc = access.cpuc()

    def _target(self, name: str, value: str) -> str:
        key = norm(value)
        if key in self.overrides[name]:
            return self.overrides[name][key]
        return BUILTIN.get(name, {}).get(key, value)

    def option(self, name: str, value: str) -> AttributeOption | None:
        code = LIST_FIELDS[name][1]
        target = self._target(name, value)
        return self.options[code].get(norm(target)) if target else None

    def organisation(self, value: str) -> Organisation | None:
        target = self._target("manager", value)
        if not target:
            return None
        if norm(target) == "cpuc":
            return self.cpuc
        return self.organisations.get(norm(target))


def load_map(path: Path | None) -> list[dict]:
    if path is None:
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_map(records: list[dict], mapping: Mapping, path: Path) -> int:
    """Every value the PSR holds in the mapped fields, how often, and what it maps to now (blank: nothing), for CPUC ED to complete
    and give back as --map. Returns the number of rows."""
    seen: dict[tuple[str, str], int] = Counter()
    for r in records:
        for name, (key, _) in LIST_FIELDS.items():
            for label in _labels(r.get(key)):
                seen[(name, label)] += 1
        manager = _text(r, "studyManager")
        if manager:
            seen[("manager", manager)] += 1
    rows = []
    for (name, value), n in sorted(seen.items(), key=lambda kv: (MAP_FIELDS.index(kv[0][0]), -kv[1], kv[0][1].lower())):
        if name == "manager":
            o = mapping.organisation(value)
            target = o.code if o else ""
        else:
            o = mapping.option(name, value)
            target = o.label if o else ""
        rows.append({"field": name, "psr_value": value, "register_value": target, "projects": n})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["field", "psr_value", "register_value", "projects"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# --- the report


@dataclass
class Report:
    studies: int = 0
    skipped_existing: int = 0
    number_taken: list[int] = field(default_factory=list)
    work_orders: int = 0
    new_work_orders: set = field(default_factory=set)
    work_orders_linked: int = 0
    work_order_numbers_kept: int = 0
    unmapped: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    managers_unmatched: int = 0
    no_lead: list[int] = field(default_factory=list)
    long_text: Counter = field(default_factory=Counter)
    budget_not_a_number: list[int] = field(default_factory=list)
    new_study_ids: list[int] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"{self.studies} studies imported, {self.skipped_existing} already in the register",
            f"{self.work_orders_linked} projects linked to a work order; {self.work_orders} work orders added (CPUC contract numbers not "
            f"listed yet); {self.work_order_numbers_kept} other work order numbers kept as the PSR had them",
        ]
        if self.number_taken:
            out.append(
                f"{len(self.number_taken)} projects left out, their number is taken by a study added here: {_ids(self.number_taken)}"
            )
        if self.failed:
            out.append(f"{len(self.failed)} projects left out on an error: {'; '.join(self.failed[:20])}")
        if self.no_lead:
            out.append(f"{len(self.no_lead)} projects with no study lead filed as CPUC-led: {_ids(self.no_lead)}")
        if self.managers_unmatched:
            out.append(
                f"{self.managers_unmatched} projects whose study manager matched no organization, managed by the CPUC until it "
                f"assigns them ({len(self.unmapped['manager'])} values; --write-map lists them)"
            )
        for name in MAP_FIELDS:
            if name != "manager" and self.unmapped.get(name):
                values = ", ".join(f"{v!r} x{n}" for v, n in self.unmapped[name].most_common(15))
                more = len(self.unmapped[name]) - 15
                out.append(
                    f"{name.replace('_', ' ')} values not in the register's list (kept as the PSR had them): {values}"
                    + (f" and {more} more" if more > 0 else "")
                )
        if self.budget_not_a_number:
            out.append(f"{len(self.budget_not_a_number)} budgets that are not a number (kept as text): {_ids(self.budget_not_a_number)}")
        if self.long_text:
            longer = ", ".join(f"{k} x{n}" for k, n in self.long_text.items())
            out.append(f"texts longer than the study form takes, kept whole (an update has to shorten them): {longer}")
        if self.new_study_ids:
            out.append(f"{len(self.new_study_ids)} projects with no usable study ID given a new one: {_ids(self.new_study_ids)}")
        return out


def _ids(ids) -> str:
    ids = list(ids)
    return ", ".join(str(i) for i in ids[:20]) + (f" and {len(ids) - 20} more" if len(ids) > 20 else "")


# --- one project


def _work_order(number: str, report: Report, *, dry_run: bool) -> WorkOrder | None:
    """The work order listed under the number (the PDA's, or one added earlier), else a new one for a CPUC contract number."""
    number = number.strip()
    if _none(number):
        return None
    wo = WorkOrder.objects.filter(Q(number__iexact=number) | Q(cpuc_contract_id__iexact=number)).order_by("pk").first()
    if wo is not None or not CPUC_CONTRACT.fullmatch(number):
        return wo
    if dry_run:
        if number.upper() not in report.new_work_orders:
            report.new_work_orders.add(number.upper())
            report.work_orders += 1
        return None
    wo = WorkOrder.objects.create(number=number, cpuc_contract_id=number)
    audit.record("documents.work_order_imported", obj=wo, after=audit.snapshot(wo), actor_label=LABEL)
    report.work_orders += 1
    return wo


def _budget(value, kept: dict, pid: int, report: Report) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        kept["Budget"] = str(value)
        report.budget_not_a_number.append(pid)
        return None
    return amount if amount >= 0 else None


def _ed_program_manager(name: str):
    if not name:
        return None
    User = get_user_model()
    matches = list(User.objects.filter(Q(name__iexact=name) | Q(email__iexact=name))[:2])
    return matches[0] if len(matches) == 1 else None


def map_project(record: dict, mapping: Mapping, report: Report, *, dry_run: bool) -> tuple[studies.StudyData, dict]:
    """The study's fields, and what is kept from the PSR as it was."""
    pid = int(record["projectID"])
    kept: dict[str, str] = {}

    def one(name):
        labels = _labels(record.get(LIST_FIELDS[name][0]))
        for label in labels:
            o = mapping.option(name, label)
            if o is not None:
                return o
        for label in labels:
            report.unmapped[name][label] += 1
        if labels:
            kept[name.replace("_", " ").capitalize()] = ", ".join(labels)
        return None

    def many(name):
        out, missing = [], []
        for label in _labels(record.get(LIST_FIELDS[name][0])):
            o = mapping.option(name, label)
            if o is None:
                missing.append(label)
                report.unmapped[name][label] += 1
            elif o not in out:
                out.append(o)
        if missing:
            kept[name.replace("_", " ").capitalize()] = ", ".join(missing)
        return out

    lead = one("lead")
    if lead is None:
        report.no_lead.append(pid)
        lead = mapping.option("lead", "CPUC")
    manager_text = _text(record, "studyManager")
    manager_text = "" if _none(manager_text) else manager_text
    manager = mapping.organisation(manager_text) if manager_text else None
    if manager_text:
        kept["Study manager"] = manager_text
    if manager is None:
        if manager_text:
            report.managers_unmatched += 1
            report.unmapped["manager"][manager_text] += 1
        manager = mapping.cpuc

    contacts = []
    ed_pm_text = _text(record, "EDProgramManager")
    ed_pm = _ed_program_manager(ed_pm_text)
    if ed_pm_text and ed_pm is None:
        contacts.append(f"ED program manager: {ed_pm_text}")
    for key, label in CONTACT_FIELDS:
        text = " ".join(", ".join(_labels(record.get(key))).split())
        if text and not _none(text):
            contacts.append(f"{label}: {text}")
    for key, label in KEPT_AS_IS:
        text = _text(record, key)
        if text and not _none(text):
            kept[label] = text
    wonum = _text(record, "WONum")
    work_order = _work_order(wonum, report, dry_run=dry_run)
    if work_order is not None:
        report.work_orders_linked += 1
    elif wonum and not _none(wonum) and not (dry_run and wonum.upper() in report.new_work_orders):
        kept["Work order number"] = wonum
        report.work_order_numbers_kept += 1
    calmac = _text(record, "calmacID")
    if len(calmac) > 40:
        kept["CALMAC ID"], calmac = calmac, ""
    texts = {}
    for name, key in TEXT_FIELDS.items():
        texts[name] = _text(record, key)
        if len(texts[name]) > studies.TEXT_LIMITS[name]:
            report.long_text[name] += 1
    next_activity = _text(record, "nextActivityDesc")
    if len(next_activity) > 255:
        kept["Next activity"], next_activity = next_activity, next_activity[:255]
    title = _text(record, "studyName") or f"PSR project {pid}"
    if len(title) > 255:
        kept["Study name"], title = title, title[:255]
    data = studies.StudyData(
        title=title,
        manager=manager,
        lead=lead,
        study_type=one("study_type"),
        sectors=many("sectors"),
        group=one("group"),
        funding_cycles=many("funding_cycles"),
        budget=_budget(record.get("projectBudget"), kept, pid, report),
        status=one("status"),
        work_order=work_order,
        next_activity=next_activity,
        next_activity_due=_psr_day(record.get("nextActivityDueDate")),
        completion_date=_psr_day(record.get("completionDate")),
        calmac_id="" if _none(calmac) else calmac,
        ed_program_manager=ed_pm,
        contacts="\n".join(contacts),
        **texts,
    )
    return data, kept


def import_project(record: dict, mapping: Mapping, report: Report, *, dry_run: bool = False) -> Study | None:
    pid = int(record["projectID"])
    existing = Study.objects.filter(number=pid).first()
    if existing is not None:
        if existing.psr:
            report.skipped_existing += 1
        else:
            report.number_taken.append(pid)
        return None
    data, kept = map_project(record, mapping, report, dry_run=dry_run)
    study_id = _text(record, "studyID")
    if not study_id.endswith(f"_{pid}") or len(study_id) > 80 or Study.objects.filter(study_id=study_id).exists():
        report.new_study_ids.append(pid)
        study_id = ""
    updated = _when(record.get("updatedAt")) or timezone.now()
    created = _when(record.get("createdAt")) or updated
    if dry_run:
        report.studies += 1
        return None
    psr = {"project": pid, "study_id": _text(record, "studyID"), "fields": kept}
    try:
        with transaction.atomic():
            study = studies.import_study(data, number=pid, study_id=study_id, psr=psr, created_at=created, updated_at=updated, label=LABEL)
    except DocumentError as e:
        report.failed.append(f"{pid}: {e}")
        return None
    report.studies += 1
    return study


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch(base_url: str, path: str):
    with urllib.request.urlopen(f"{base_url.rstrip('/')}{path}", timeout=TIMEOUT) as r:  # noqa: S310 (an https URL the operator gives)
        return json.load(r)


def records_of(data) -> list[dict]:
    """The PSR's list, bare or under a key (a saved response may wrap it)."""
    if isinstance(data, dict):
        for key in ("data", "projects", "results", "items", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
        raise DocumentError("The projects file holds no list of projects.")
    return data


def run(records: list[dict], mapping: Mapping, *, dry_run: bool = False) -> Report:
    """Import the projects in their PSR number order."""
    if mapping.cpuc is None:
        raise RuntimeError("Set up the CPUC organization before importing the PSR.")
    report = Report()
    for record in sorted(records, key=lambda r: int(r["projectID"])):
        import_project(record, mapping, report, dry_run=dry_run)
    return report
