"""The evaluation study register (BR-15.6 to 15.8), replacing the Energy Division's Project Status Report: one record per EM&V study,
the programs and program years each evaluates (so every program's page lists its evaluations), the work orders and tasks EM&V documents
are delivered under, and the study lists and budget summaries the EM&V plans compile by hand today.

Who may do what:
  - everyone, signed in or not, reads every study and the register, except the contacts;
  - CPUC ED's document administrators add and change any study, keep the work orders and assign a study's manager;
  - a PA with Documents: submit adds the studies it leads and manages (PA-led studies), and the study manager's organisation (an
    evaluation contractor or a PA) updates its studies with Documents: submit;
  - the contacts are seen by CPUC staff and the study manager's organisation.
Every change is an audit event with the fields before and after, labels rather than ids, which is the study's history (status and
budget history, BR-15.6)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch, Q, QuerySet
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry.models import Program

from .models import AttributeOption, Document, Study, StudyProgram, Task, WorkOrder
from .service import ANY, SUBMIT, DocumentError, _cpuc_has, is_admin

LISTS = {
    "study_type": "study_type",
    "sectors": "sector",
    "group": "study_group",
    "lead": "study_lead",
    "funding_cycles": "portfolio_cycle",
    "status": "study_status",
}
COMPLETED = "Completed"
HISTORY_FIELDS = ("status", "budget")


def options(list_code: str, keep=()) -> QuerySet:
    """A list's options offered today, plus any in keep (the ones a study already carries)."""
    today = timezone.localdate()
    keep_ids = [o.pk for o in keep if o is not None]
    return AttributeOption.objects.filter(attribute_list__code=list_code).filter(
        Q(retired_on__isnull=True) | Q(retired_on__gt=today) | Q(pk__in=keep_ids)
    )


# --- who may do what


def can_create(user, organisation: Organisation | None) -> bool:
    if is_admin(user):
        return True
    return (
        organisation is not None
        and organisation.kind == Organisation.Kind.PA
        and user is not None
        and access.has_permission(user, organisation, SUBMIT)
    )


def can_edit(user, study: Study) -> bool:
    return is_admin(user) or (user is not None and user.is_authenticated and access.has_permission(user, study.manager, SUBMIT))


def sees_contacts(user, study: Study) -> bool:
    if user is None:
        return True
    if not user.is_authenticated:
        return False
    return is_admin(user) or _cpuc_has(user, *ANY) or any(access.has_permission(user, study.manager, p) for p in ANY)


# --- studies


@dataclass
class StudyData:
    title: str
    manager: Organisation
    lead: AttributeOption
    study_type: AttributeOption | None = None
    sectors: list[AttributeOption] = field(default_factory=list)
    group: AttributeOption | None = None
    funding_cycles: list[AttributeOption] = field(default_factory=list)
    budget: Decimal | None = None
    status: AttributeOption | None = None
    work_order: WorkOrder | None = None
    description: str = ""
    objective: str = ""
    research_questions: str = ""
    methods: str = ""
    next_activity: str = ""
    next_activity_due: date | None = None
    completion_date: date | None = None
    calmac_id: str = ""
    notes: str = ""
    ed_program_manager: object = None
    contacts: str = ""


TEXT_LIMITS = {"description": 1000, "objective": 500, "research_questions": 1500, "methods": 250, "notes": 500}


def _check(data: StudyData, keep: set[int]) -> None:
    today = timezone.localdate()
    if not data.title.strip():
        raise DocumentError("Give the study a title.")
    if data.lead is None:
        raise DocumentError("Say who leads the study.")
    for name, code in LISTS.items():
        value = getattr(data, name)
        for o in value if isinstance(value, list) else [value]:
            if o is None:
                continue
            if o.attribute_list.code != code:
                raise DocumentError(f"{o.label} is not a value of {code.replace('_', ' ')}.")
            if not o.offered_on(today) and o.pk not in keep:
                raise DocumentError(f"{o.label} is retired.")
    if data.budget is not None and data.budget < 0:
        raise DocumentError("A budget is not negative.")
    for name, limit in TEXT_LIMITS.items():
        if len(getattr(data, name) or "") > limit:
            raise DocumentError(f"Keep the {name.replace('_', ' ')} to {limit:,} characters.")


def _kept(study: Study) -> set[int]:
    ids = {study.study_type_id, study.group_id, study.lead_id, study.status_id}
    ids |= set(study.sectors.values_list("pk", flat=True)) | set(study.funding_cycles.values_list("pk", flat=True))
    return {i for i in ids if i}


def snapshot(study: Study) -> dict:
    """The study's fields as labels, for its audit events (the history; `history` leaves the contacts out for the public)."""
    snap = audit.snapshot(
        study,
        [
            "title",
            "description",
            "objective",
            "research_questions",
            "methods",
            "budget",
            "next_activity",
            "next_activity_due",
            "completion_date",
            "calmac_id",
            "notes",
            "contacts",
        ],
    )
    snap.update(
        study_type=study.study_type.label if study.study_type else "",
        group=study.group.label if study.group else "",
        lead=study.lead.label,
        status=study.status.label if study.status else "",
        manager=study.manager.code,
        work_order=study.work_order.number if study.work_order else "",
        ed_program_manager=study.ed_program_manager.email if study.ed_program_manager else "",
        sectors=sorted(o.label for o in study.sectors.all()),
        funding_cycles=sorted(o.label for o in study.funding_cycles.all()),
    )
    return snap


def make_study_id(number: int, data: StudyData) -> str:
    """<year>_<sector>_<number> as the PSR builds it (2025_Residential_733): the first year of the earliest funding cycle (else this
    year) and the first sector (else Cross-Cutting)."""
    years = [int(m) for o in data.funding_cycles for m in re.findall(r"\b(\d{4})\b", o.label)[:1]]
    year = min(years) if years else timezone.localdate().year
    sector = data.sectors[0].label if data.sectors else "Cross-Cutting"
    return f"{year}_{re.sub(r'[^A-Za-z0-9]+', '-', sector).strip('-')}_{number}"


def _apply(study: Study, data: StudyData) -> None:
    for name in (
        "title",
        "manager",
        "lead",
        "study_type",
        "group",
        "budget",
        "status",
        "work_order",
        "description",
        "objective",
        "research_questions",
        "methods",
        "next_activity",
        "next_activity_due",
        "completion_date",
        "calmac_id",
        "notes",
        "ed_program_manager",
        "contacts",
    ):
        value = getattr(data, name)
        setattr(study, name, value.strip() if isinstance(value, str) else value)


@transaction.atomic
def create_study(actor, organisation: Organisation | None, data: StudyData, *, number: int | None = None) -> Study:
    """Add a study. A PA adds the studies it manages itself; CPUC ED any study, naming its manager. number is the PSR's project number
    when a study is imported; a new study takes the next one."""
    if not can_create(actor, organisation):
        raise AccessDenied("Only CPUC ED, or a PA with Documents: submit for its own studies, adds studies to the register.")
    if not is_admin(actor) and data.manager != organisation:
        raise AccessDenied(f"A PA adds the studies it manages: name {organisation.code} as the study manager.")
    _check(data, set())
    if number is None:
        number = (Study.objects.select_for_update().aggregate(m=Max("number"))["m"] or 0) + 1
    study = Study(number=number, study_id=make_study_id(number, data), created_by=actor)
    _apply(study, data)
    try:
        with transaction.atomic():
            study.save()
    except IntegrityError as e:
        raise DocumentError(f"Study number {number} is taken.") from e
    study.sectors.set(data.sectors)
    study.funding_cycles.set(data.funding_cycles)
    audit.record("documents.study_added", obj=study, after=snapshot(study), actor=actor)
    return study


@transaction.atomic
def change_study(actor, study: Study, data: StudyData, *, reason: str) -> Study:
    """Change a study (its ID never changes). Only CPUC ED changes who manages it."""
    if not can_edit(actor, study):
        raise AccessDenied("Only the study manager's organisation and CPUC ED update a study.")
    if not reason.strip():
        raise DocumentError("Give the reason for the change.")
    if data.manager != study.manager and not is_admin(actor):
        raise AccessDenied("Only CPUC ED assigns a study to another manager.")
    _check(data, _kept(study))
    before = snapshot(study)
    _apply(study, data)
    study.save()
    study.sectors.set(data.sectors)
    study.funding_cycles.set(data.funding_cycles)
    after = snapshot(study)
    if after != before:
        audit.record("documents.study_changed", obj=study, before=before, after=after, reason=reason, actor=actor)
    return study


def history(study: Study, *, public: bool = True) -> list[dict]:
    """Every change to the study, newest first: when, who (their organisation for the public), why, and each field changed (old, new).
    Contacts are left out for the public."""
    out = []
    for e in audit.changes(study).select_related("organisation").order_by("-at", "-id"):
        if e.action == "documents.study_added":
            changes = [("status", "", (e.after or {}).get("status", "")), ("budget", "", (e.after or {}).get("budget"))]
        else:
            changes = audit.diff(e.before, e.after)
        if public:
            changes = [c for c in changes if c[0] not in ("contacts", "ed_program_manager")]
        if not changes and e.action != "documents.study_added":
            continue
        out.append(
            {
                "at": e.at,
                # The public sees the organisation only, never a person.
                "who": (e.organisation.code if e.organisation else "imported" if e.actor_id is None else "not recorded")
                if public
                else e.actor_label or "imported",
                "action": "Added" if e.action == "documents.study_added" else e.reason,
                "changes": changes,
                "status_or_budget": any(c[0] in HISTORY_FIELDS for c in changes),
            }
        )
    return out


# --- programs evaluated (BR-15.7)


@transaction.atomic
def set_coverage(
    actor, study: Study, program: Program, first_year: int, last_year: int, *, whole: bool = True, note: str = ""
) -> StudyProgram:
    """Name a program the study evaluates and the years it covers (or change them)."""
    if not can_edit(actor, study):
        raise AccessDenied("Only the study manager's organisation and CPUC ED update a study.")
    if not first_year or not last_year or first_year > last_year:
        raise DocumentError("Give the first and last program years covered, in order.")
    row = StudyProgram.objects.filter(study=study, program=program).first()
    before = None
    if row is None:
        row = StudyProgram(study=study, program=program)
    else:
        before = audit.snapshot(row, ["first_year", "last_year", "whole", "note"])
    row.first_year, row.last_year, row.whole, row.note = first_year, last_year, whole, note.strip()
    row.save()
    audit.record(
        "documents.study_program_set",
        obj=study,
        before=before and {"program": program.prg_id, **before},
        after={"program": program.prg_id, **audit.snapshot(row, ["first_year", "last_year", "whole", "note"])},
        actor=actor,
    )
    return row


@transaction.atomic
def remove_coverage(actor, row: StudyProgram, *, reason: str) -> None:
    if not can_edit(actor, row.study):
        raise AccessDenied("Only the study manager's organisation and CPUC ED update a study.")
    if not reason.strip():
        raise DocumentError("Give the reason for removing the program.")
    before = {"program": row.program.prg_id, **audit.snapshot(row, ["first_year", "last_year", "whole", "note"])}
    study = row.study
    row.delete()
    audit.record("documents.study_program_removed", obj=study, before=before, reason=reason, actor=actor)


def latest_deliverable(study: Study) -> Document | None:
    """The study's most recently published document (its deliverable at its current stage)."""
    return study.documents.filter(published_at__isnull=False).order_by("-published_at").first()


def evaluations(program: Program) -> list[dict]:
    """The registry hook (registry.service.evaluations): each study covering the program, newest years first."""
    rows = (
        StudyProgram.objects.filter(program=program)
        .select_related("study", "study__status", "study__lead")
        .order_by("-last_year", "-first_year", "-study__number")
    )
    out = []
    for r in rows:
        d = latest_deliverable(r.study)
        out.append(
            {
                "study_id": r.study.study_id,
                "title": r.study.title,
                "url": r.study.get_absolute_url(),
                "first_year": r.first_year,
                "last_year": r.last_year,
                "years": r.years,
                "whole": r.whole,
                "note": r.note,
                "status": r.study.status.label if r.study.status else "",
                "lead": r.study.lead.label,
                "deliverable": {"title": d.title, "url": d.get_absolute_url(), "stage": d.document_type.label} if d else None,
            }
        )
    return out


def covering(program: Program, year: int) -> list[Study]:
    """The studies that evaluated the program in a given program year (for a claims view, BR-15.7)."""
    return list(Study.objects.filter(coverage__program=program, coverage__first_year__lte=year, coverage__last_year__gte=year))


# --- the register (BR-15.6, 15.8)


@dataclass
class RegisterFilter:
    q: str = ""
    cycles: list[AttributeOption] = field(default_factory=list)
    statuses: list[AttributeOption] = field(default_factory=list)
    sectors: list[AttributeOption] = field(default_factory=list)
    leads: list[AttributeOption] = field(default_factory=list)
    types: list[AttributeOption] = field(default_factory=list)
    managers: list[Organisation] = field(default_factory=list)
    program: str = ""


def register(f: RegisterFilter | None = None) -> QuerySet:
    f = f or RegisterFilter()
    qs = Study.objects.all()
    for term in f.q.split():
        qs = qs.filter(
            Q(title__icontains=term)
            | Q(study_id__icontains=term)
            | Q(description__icontains=term)
            | Q(work_order__number__icontains=term)
            | Q(calmac_id__icontains=term)
        )
    if f.cycles:
        qs = qs.filter(funding_cycles__in=f.cycles)
    if f.sectors:
        qs = qs.filter(sectors__in=f.sectors)
    if f.statuses:
        qs = qs.filter(status__in=f.statuses)
    if f.leads:
        qs = qs.filter(lead__in=f.leads)
    if f.types:
        qs = qs.filter(study_type__in=f.types)
    if f.managers:
        qs = qs.filter(manager__in=f.managers)
    if f.program:
        qs = qs.filter(coverage__program__prg_id__iexact=f.program.strip())
    return (
        Study.objects.filter(pk__in=qs.values("pk"))
        .select_related("study_type", "group", "lead", "status", "manager", "work_order")
        .prefetch_related(
            "sectors",
            "funding_cycles",
            Prefetch("coverage", queryset=StudyProgram.objects.select_related("program")),
        )
    )


def upcoming(limit: int = 10) -> list[Study]:
    """The PSR's Activity Report, first half: the next activities due from today."""
    today = timezone.localdate()
    return list(Study.objects.filter(next_activity_due__gte=today).select_related("manager").order_by("next_activity_due")[:limit])


def recently_updated(limit: int = 10) -> list[Study]:
    return list(Study.objects.select_related("manager", "status").order_by("-updated_at")[:limit])


SUMMARY_BY = {"type": "Study type", "sector": "Sector", "lead": "Study lead", "status": "Status", "group": "Group"}


def summary(cycle: AttributeOption | None = None) -> dict:
    """The EM&V plan's study and budget tables (BR-15.8): for one funding cycle (or all studies), by study type, sector, lead, status and
    group, the number of studies and their budgets, planned (every study not cancelled) against completed. A study with several sectors
    counts in each sector, so the sector table can add up to more than the total; every other table adds up to it."""
    studies = list(register(RegisterFilter(cycles=[cycle] if cycle else [])))
    tables = {}
    for by, title in SUMMARY_BY.items():
        acc = defaultdict(lambda: {"studies": 0, "budget": Decimal(0), "completed": 0, "completed_budget": Decimal(0)})
        for s in studies:
            if s.status and s.status.label == "Cancelled":
                continue
            if by == "sector":
                keys = [o.label for o in s.sectors.all()] or ["Not given"]
            else:
                o = {"type": s.study_type, "lead": s.lead, "status": s.status, "group": s.group}[by]
                keys = [o.label if o else "Not given"]
            for k in keys:
                row = acc[k]
                row["studies"] += 1
                row["budget"] += s.budget or 0
                if s.status and s.status.label == COMPLETED:
                    row["completed"] += 1
                    row["completed_budget"] += s.budget or 0
        rows = sorted(acc.items(), key=lambda kv: (-kv[1]["budget"], kv[0]))
        tables[by] = {"title": title, "rows": rows}
    live = [s for s in studies if not (s.status and s.status.label == "Cancelled")]
    total = {
        "studies": len(live),
        "budget": sum((s.budget or 0 for s in live), Decimal(0)),
        "completed": sum(1 for s in live if s.status and s.status.label == COMPLETED),
        "completed_budget": sum((s.budget or 0 for s in live if s.status and s.status.label == COMPLETED), Decimal(0)),
        "cancelled": len(studies) - len(live),
    }
    return {"tables": tables, "total": total}


# --- work orders (BR-15.3's second classification)


def _require_admin(actor):
    if not is_admin(actor):
        raise AccessDenied("Only CPUC ED's document administrators keep the work orders.")


@transaction.atomic
def add_work_order(
    actor, *, number: str, name: str = "", firm: Organisation | None = None, firm_name: str = "", cpuc_contract_id=""
) -> WorkOrder:
    _require_admin(actor)
    number = number.strip()
    if not number:
        raise DocumentError("Give the work order number.")
    if WorkOrder.objects.filter(number__iexact=number).exists():
        raise DocumentError(f"Work order {number} is already listed.")
    wo = WorkOrder.objects.create(
        number=number, name=name.strip(), firm=firm, firm_name=firm_name.strip(), cpuc_contract_id=cpuc_contract_id.strip()
    )
    audit.record("documents.work_order_added", obj=wo, after=audit.snapshot(wo), actor=actor)
    return wo


@transaction.atomic
def add_task(actor, work_order: WorkOrder, name: str) -> Task:
    _require_admin(actor)
    name = name.strip()
    if not name:
        raise DocumentError("Give the task a name.")
    if work_order.tasks.filter(name__iexact=name).exists():
        raise DocumentError(f"{work_order.number} already has the task {name}.")
    task = Task.objects.create(work_order=work_order, name=name)
    audit.record("documents.task_added", obj=task, after=audit.snapshot(task), actor=actor)
    return task


@transaction.atomic
def set_cancelled(actor, work_order: WorkOrder, cancelled: bool, *, reason: str) -> WorkOrder:
    _require_admin(actor)
    before = audit.snapshot(work_order)
    work_order.cancelled = cancelled
    work_order.save(update_fields=["cancelled"])
    audit.record(
        "documents.work_order_changed", obj=work_order, before=before, after=audit.snapshot(work_order), reason=reason, actor=actor
    )
    return work_order
