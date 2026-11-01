"""The Edit inputs pages: the drafts list (start one from an upload or a blank job), a draft's tables with every problem cell marked,
and the page that edits one row. Server-rendered forms, so they work without scripts."""

from django import forms as djforms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Min
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from cedars.core import uploads
from cedars.services.accounts.service import AccessDenied

from . import drafts, service
from .models import Run

PER_PAGE = 50
SEVERITY = {"error": "Stops the run", "warning": "Warning", "info": "Note"}
SYMBOL = {"error": "✕", "warning": "!", "info": "i"}


class NewDraftForm(djforms.Form):
    name = djforms.CharField(label="Name", max_length=200, help_text="e.g. Lighting retrofit, 2028")
    run_for = djforms.ChoiceField(
        label="For", required=False, help_text="An organization's drafts are shared with its members who may start runs"
    )
    start = djforms.ChoiceField(
        label="Start from",
        choices=[("upload", "A job file I upload"), ("blank", "A new job (the template's example rows)")],
        widget=djforms.RadioSelect,
        initial="upload",
    )
    job = djforms.FileField(
        label="Job file (.zip)",
        required=False,
        help_text=uploads.limit_text("Measure.csv and, optionally, ProgramCost.csv, zipped.", False),
        validators=[uploads.check_size],
    )

    def __init__(self, *args, organisations=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.organisations = {o.code: o for o in organisations}
        self.fields["run_for"].choices = [(o.code, o.code) for o in organisations] + [("", "Only me")]

    def clean(self):
        data = super().clean()
        if data.get("start") == "upload" and not data.get("job"):
            self.add_error("job", "Choose the job file, or start from a new job.")
        data["organisation"] = self.organisations.get(data.get("run_for") or "")
        return data


def _no_engine(request):
    return render(request, "calculations/drafts.html", {"no_engine": True})


def _issue(i) -> dict:
    """An issue as the pages show it: its severity in words and as a mark, the row by number and the field's anchor on the row page."""
    return {
        "table": i.table,
        "number": i.row + 1,
        "column": i.column,
        "message": i.message[:1].upper() + i.message[1:],
        "label": SEVERITY[i.severity],
        "symbol": SYMBOL[i.severity],
        "anchor": f"f-{slugify(i.column)}",
        "severity": i.severity,
    }


def _issues(d, src=None) -> tuple[list, str]:
    """(the draft's problem cells, why the checks could not run or "")."""
    try:
        return drafts.issues(d, src), ""
    except drafts.ChecksFailed as e:
        return [], str(e)


def _draft_or_404(request, pk):
    draft = get_object_or_404(drafts.visible(request.user), pk=pk)
    return draft


def _table(request) -> str:
    t = request.GET.get("table") or request.POST.get("table") or "Measure"
    return t if t in drafts.TABLES else "Measure"


@login_required
def draft_list(request):
    if drafts.engine() is None:
        return _no_engine(request)
    orgs = [o for o in request.user_organisations if service.can_submit(request.user, o)]
    if request.organisation in orgs:
        orgs.remove(request.organisation)
        orgs.insert(0, request.organisation)
    form = NewDraftForm(request.POST or None, request.FILES or None, organisations=orgs)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            if d["start"] == "blank":
                draft = drafts.create_blank(request.user, d["organisation"], d["name"])
            else:
                draft = drafts.create_from_upload(request.user, d["organisation"], d["name"], d["job"])
        except AccessDenied as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"{draft.name}: {draft.measure_count} measure{'s' if draft.measure_count != 1 else ''} to edit.")
            return redirect("calculations:draft", pk=draft.pk)
    return render(
        request,
        "calculations/drafts.html",
        {
            "form": form,
            "drafts": [(d, drafts.can_change(request.user, d)) for d in drafts.visible(request.user)],
            "max_rows": drafts.max_rows(),
        },
    )


@login_required
@require_POST
def draft_from_run(request, pk):
    """Edit inputs and rerun: a draft from a run's inputs."""
    if drafts.engine() is None:
        return _no_engine(request)
    run = get_object_or_404(Run, pk=pk)
    if not service.can_read(request.user, run):
        raise Http404
    try:
        draft = drafts.create_from_run(request.user, run)
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("calculations:run", pk=run.pk)
    messages.success(request, f"{draft.name}: the run's inputs, ready to edit.")
    return redirect("calculations:draft", pk=draft.pk)


def _display(ed, table, df):
    pinned, scroll, no_effect = ed.display_layout(table, list(df.columns))
    return pinned + scroll, no_effect


@login_required
def draft(request, pk):
    if drafts.engine() is None:
        return _no_engine(request)
    ed = drafts.engine()
    d = _draft_or_404(request, pk)
    can_change = drafts.can_change(request.user, d)
    table = _table(request)
    if request.method == "POST":
        if not can_change:
            raise Http404
        action = request.POST.get("action")
        try:
            if action == "rename":
                drafts.rename(request.user, d, request.POST.get("name", ""))
                messages.success(request, "Renamed.")
            elif action == "add":
                i = drafts.add_row(request.user, d, table, like=0 if table == "Measure" and d.measure_count else None)
                messages.success(request, f"Added row {i + 1}.")
                return redirect(f"{reverse('calculations:draft_row', args=[d.pk, table, i + 1])}")
            elif action == "delete":
                drafts.delete(request.user, d)
                messages.success(request, f"Deleted the draft {d.name}.")
                return redirect("calculations:drafts")
        except AccessDenied as e:
            messages.error(request, str(e))
        return redirect(f"{reverse('calculations:draft', args=[d.pk])}?table={table}")

    df = drafts.frame(d, table)
    columns, no_effect = _display(ed, table, df)
    found, failed = _issues(d)
    by_cell: dict[tuple[int, str], list] = {}
    for i in found:
        if i.table == table:
            by_cell.setdefault((i.row, i.column), []).append(i)
    pages = max(1, -(-len(df) // PER_PAGE))
    try:
        page = min(max(1, int(request.GET.get("page", 1))), pages)
    except ValueError:
        page = 1
    start = (page - 1) * PER_PAGE
    numeric = ed.numeric_columns(table)
    rows = []
    for i in range(start, min(len(df), start + PER_PAGE)):
        cells = []
        for c in columns:
            v = df.at[i, c]
            cell_issues = by_cell.get((i, c), [])
            worst = next((s for s in ("error", "warning", "info") if any(x.severity == s for x in cell_issues)), None)
            cells.append(
                {
                    "value": ed.display_number(v) if c in numeric else v,
                    "worst": worst,
                    "symbol": SYMBOL.get(worst, ""),
                    "said": "; ".join(f"{SEVERITY[x.severity]}: {x.message}" for x in cell_issues),
                }
            )
        rows.append({"index": i, "number": i + 1, "cells": cells})
    counts = {s: sum(1 for i in found if i.severity == s) for s in ("error", "warning", "info")}
    listed = sorted(found, key=lambda i: (("error", "warning", "info").index(i.severity), drafts.TABLES.index(i.table), i.row))
    return render(
        request,
        "calculations/draft.html",
        {
            "draft": d,
            "can_change": can_change,
            "table": table,
            "tables": [(t, len(drafts.frame(d, t))) for t in drafts.TABLES],
            "columns": [(c, c in no_effect) for c in columns],
            "rows": rows,
            "page": page,
            "pages": pages,
            "counts": counts,
            "issues": [_issue(i) for i in listed[:200]],
            "checks_failed": failed,
            "first_row": start + 1,
            "last_row": min(len(df), start + PER_PAGE),
            "more_issues": max(0, len(listed) - 200),
        },
    )


def _field(ed, src, table, column, value, row, years, issues):
    """How the row page shows one column: a select of the engine's values for it (the current value kept), else a text box."""
    choices = None if column in ed.END_USE_COLUMNS else src.column_choices(table, column, years)
    if choices is not None and value and value not in choices:
        choices = [value, *choices]
    affects, how = ed.EFFECTS[table].get(column, (None, ""))
    return {
        "name": column,
        "id": f"f-{slugify(column)}",
        "value": value,
        "choices": choices,
        "effect": f"Changes {affects}." if ed.affects_results(table, column) and affects else "No effect on results.",
        "how": how,
        "help": ed.field_help().get(column, ""),
        "issues": [_issue(i) for i in issues],
    }


@login_required
def draft_row(request, pk, table, number):
    if drafts.engine() is None:
        return _no_engine(request)
    ed = drafts.engine()
    d = _draft_or_404(request, pk)
    if table not in drafts.TABLES:
        raise Http404
    df = drafts.frame(d, table)
    index = number - 1
    if not 0 <= index < len(df):
        raise Http404
    can_change = drafts.can_change(request.user, d)
    back = f"{reverse('calculations:draft', args=[d.pk])}?table={table}&page={index // PER_PAGE + 1}"
    if request.method == "POST":
        if not can_change:
            raise Http404
        action = request.POST.get("action", "save")
        try:
            if action == "delete":
                drafts.delete_row(request.user, d, table, index)
                messages.success(request, f"Deleted row {number}.")
                return redirect(back)
            if action == "copy":
                i = drafts.add_row(request.user, d, table, like=index, copy=True)
                messages.success(request, f"Copied row {number} to row {i + 1}.")
                return redirect("calculations:draft_row", pk=d.pk, table=table, number=i + 1)
            changed = drafts.set_row(request.user, d, table, index, {c: request.POST[c] for c in df.columns if c in request.POST})
        except AccessDenied as e:
            messages.error(request, str(e))
            return redirect("calculations:draft_row", pk=d.pk, table=table, number=number)
        n = len(changed)
        messages.success(request, f"Saved row {number}: {n} value{'s' if n != 1 else ''} changed." if n else "Nothing changed.")
        return redirect("calculations:draft_row", pk=d.pk, table=table, number=number)

    src = drafts.choices()
    row = df.iloc[index].to_dict()
    year = ed.claim_year(row.get("ClaimYearQuarter", ""))
    years = [year] if year else None
    found, failed = _issues(d, src)
    found = [i for i in found if i.table == table and i.row == index]
    pinned, scroll, no_effect = ed.display_layout(table, list(df.columns))
    fields = {c: _field(ed, src, table, c, row[c], index, years, [i for i in found if i.column == c]) for c in df.columns}
    groups = [
        ("Identification", [fields[c] for c in pinned]),
        ("Changes results", [fields[c] for c in scroll if c not in no_effect]),
        ("No effect on results (carried to the outputs)", [fields[c] for c in scroll if c in no_effect]),
    ]
    return render(
        request,
        "calculations/draft_row.html",
        {
            "draft": d,
            "table": table,
            "number": number,
            "count": len(df),
            "can_change": can_change,
            "groups": [(g, f) for g, f in groups if f],
            "issues": [_issue(i) for i in found],
            "checks_failed": failed,
            "back": back,
        },
    )


@login_required
def draft_download(request, pk):
    if drafts.engine() is None:
        return _no_engine(request)
    d = _draft_or_404(request, pk)
    try:
        data = drafts.job_zip_bytes(d)
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("calculations:draft", pk=d.pk)
    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{slugify(d.name) or "job"}.zip"'
    return resp


# --- add a measure from eTRM (BR-06.22)


def _year(request, ed, df) -> tuple[int | None, str]:
    """(the year packages must be in effect in, the year select's value): the year asked for, else row 1's claim year; "all" = any."""
    asked = request.GET.get("year", "")
    if asked == "all":
        return None, "all"
    if asked.isdigit() and 2000 <= int(asked) <= 2100:
        return int(asked), asked
    y = ed.claim_year(df.at[0, "ClaimYearQuarter"]) if len(df) else None
    return (y, str(y)) if y else (None, "all")


@login_required
def draft_etrm(request, pk):
    """Add a measure from eTRM: find a measure package, choose its offering one key at a time (each choice lists only the values left
    by the ones before it, and a key with one value is chosen for you), enter the units, and the new row is filled from the
    permutation. Plain GET forms, so every step works without scripts."""
    from cedars.services.refdata import etrm

    if drafts.engine() is None:
        return _no_engine(request)
    ed = drafts.engine()
    d = _draft_or_404(request, pk)
    if not drafts.can_change(request.user, d):
        raise Http404
    pack = etrm.editor_pack()
    df = drafts.frame(d, "Measure")
    if request.method == "POST" and pack is not None:
        mdid = request.POST.get("measure_detail_id", "")
        perm = etrm.lookup(pack, [mdid]).get(mdid.strip().lower())
        if perm is None:
            messages.error(request, "That permutation is not in the eTRM pack. Choose the offering again.")
        else:
            try:
                done = drafts.add_from_etrm(request.user, d, perm, request.POST.get("units", ""))
            except AccessDenied as e:
                messages.error(request, str(e))
            else:
                left = "; ".join(f"{c} ({why})" for c, _src, why in done.left)
                messages.success(
                    request,
                    f"Added row {done.index + 1} from {perm.measure_detail_id}: {len(done.filled)} fields filled from eTRM."
                    + (f" Left for you: {left}." if left else "")
                    + " Every field can still be changed here.",
                )
                return redirect("calculations:draft_row", pk=d.pk, table="Measure", number=done.index + 1)
        return redirect(f"{reverse('calculations:draft_etrm', args=[d.pk])}?{request.GET.urlencode()}")

    year, year_value = _year(request, ed, df)
    q = request.GET.get("q", "").strip()[:100]
    ctx = {
        "draft": d,
        "pack": pack,
        "q": q,
        "year": year,
        "year_value": year_value,
        "years": _years(year_value, pack),
        "full": len(df) >= ed.MAX_EDIT_ROWS,
        "max_rows": ed.MAX_EDIT_ROWS,
    }
    if pack is None:
        return render(request, "calculations/draft_etrm.html", ctx)
    mvid = request.GET.get("package", "").strip().lower()
    pkg = etrm.package(pack, mvid) if mvid else None
    if pkg is None:
        cap = settings.ETRM_PACKAGE_LIST
        found = etrm.packages(pack, q, year, limit=cap + 1)
        ctx.update(packages=found[:cap], more_packages=len(found) > cap, package_list=cap)
        return render(request, "calculations/draft_etrm.html", ctx)
    narrowed = etrm.narrow(pack, pkg.measure_version_id, {f: request.GET.get(f, "") for f in etrm.KEY_FIELDS})
    perm = narrowed.permutation
    shown = {st.field for st in narrowed.steps}
    ctx.update(
        package=pkg,
        steps=[(st, _options(st, pkg.offerings)) for st in narrowed.steps],
        carried=[(f, request.GET[f]) for f in etrm.KEY_FIELDS if f not in shown and request.GET.get(f)],
        permutation=perm,
        matches=narrowed.matches,
    )
    if perm is not None:
        fill, left = etrm.measure_row(perm, df.columns)
        sources = {**etrm.MEASURE_FIELDS, **etrm.MEASURE_DESCRIPTIONS, "MeasureID": etrm.ID, "AC_Climate_Zone": "Building Location"}
        claim = ed.claim_year(df.at[0, "ClaimYearQuarter"]) if len(df) else None
        ctx.update(
            fill=[(c, fill[c], sources.get(c, "")) for c in df.columns if c in fill],
            left=left,
            claim_year=claim,
            not_in_effect=_not_in_effect(perm, claim),
            norm_unit=perm.values.get("Normalized Unit", ""),
            restricted=perm.values.get("Restricted Permutation", "").strip() not in ("", "0", "False", "false"),
        )
    return render(request, "calculations/draft_etrm.html", ctx)


def _years(year_value: str, pack) -> list[str]:
    """The years to offer: from the pack's earliest package start (or the year asked for) to five years ahead."""
    from cedars.services.refdata.models import EtrmPackage

    first = EtrmPackage.objects.filter(pack=pack).aggregate(m=Min("start_date"))["m"] if pack else None
    now = timezone.localdate().year
    lo = min(first.year if first else now, int(year_value) if year_value.isdigit() else now)
    hi = max(now + 5, int(year_value) if year_value.isdigit() else now)
    return [str(y) for y in range(lo, hi + 1)]


def _options(step, offerings: dict) -> list[tuple[str, str]]:
    """A step's (value, text) options; an offering is shown with its description."""
    if step.field == "offering":
        return [(v, f"{v}: {offerings[v]}" if offerings.get(v) else v) for v in step.options]
    return [(v, v or "(blank)") for v in step.options]


def _not_in_effect(perm, year: int | None) -> str:
    """Why the permutation is not in effect in the claim year, or ""."""
    from datetime import date

    if not year:
        return ""
    if perm.start_date and perm.start_date > date(year, 12, 31):
        return f"It starts on {perm.start_date:%Y-%m-%d}, after {year}."
    if perm.end_date and perm.end_date < date(year, 1, 1):
        return f"It ended on {perm.end_date:%Y-%m-%d}, before {year}."
    return ""
