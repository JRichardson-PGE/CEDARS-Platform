import mimetypes
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.core.templatetags.ui import status_tone
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata.models import PackVersion

from . import dispatch, drafts, forms, service
from . import results as results_mod
from .models import EngineRelease, InputDraft, Run

PER_PAGE = 50
STAGES = {
    "split": "Splitting the job into parts",
    "preflight": "Checking the inputs",
    "loading": "Loading",
    "pricing": "Pricing",
    "writing": "Writing the results",
    "file": "Writing the output files",
    "done": "Done",
}


def duration(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return "under a minute" if seconds > 0 else "now"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"about {minutes} min"
    return f"about {minutes / 60:.1f} h"


def status_view(run: Run) -> dict:
    """What the run page and its poll show: status, stage, progress and the estimates, all as text."""
    p = run.progress or {}
    fraction = p.get("fraction")
    out = {
        "status": run.status,
        "status_label": run.get_status_display(),
        "tone": status_tone(run.status),
        "ended": run.ended,
        "stage": STAGES.get(p.get("stage"), (p.get("stage") or "").capitalize()),
        "percent": round(100 * fraction) if isinstance(fraction, int | float) else None,
        "queue_position": service.queue_position(run),
    }
    est = service.eta(run)
    if run.status == Run.Status.QUEUED and est:
        pos = out["queue_position"]
        out["text"] = (
            f"Queued, {'next' if pos == 1 else f'number {pos}'} in line for {run.get_lane_display().lower()}. "
            # under a minute to wait reads "Starting now", not "Starts in now" (UI review 2, #27)
            f"{'Starting now' if (est['starts_in'] or 0) < 60 else 'Starts in ' + duration(est['starts_in'])}, "
            f"done in {duration(est['done_in'])} (estimated)."
        )
    elif run.status == Run.Status.RUNNING:
        parts = [out["stage"] or "Running"]
        if out["percent"] is not None:
            parts.append(f"{out['percent']}% done")
        if est.get("done_in") is not None:
            parts.append(f"done in {duration(est['done_in'])} (estimated)")
        out["text"] = ", ".join(parts) + "."
    elif run.status == Run.Status.SUCCEEDED:
        out["text"] = f"Finished in {duration(run.seconds)}." if run.seconds else "Finished."
    elif run.status == Run.Status.CANCELLED:
        out["text"] = "Stopped."
    elif run.status == Run.Status.REFUSED:
        out["text"] = "The engine refused the job: " + (run.error.splitlines()[-1] if run.error else "no reason given.")
    else:
        out["text"] = "The run failed."
    return out


def settings_view(settings: dict) -> list[tuple[str, object]]:
    """The run's settings as (label, value); an output template as its name and column count, not the whole list."""
    out = []
    for k, v in sorted(settings.items()):
        if k in ("output_template_name", "cost_test_set"):  # the set has its own row, Cost tests, with its version and hash
            continue
        if k == "output_template":
            v = f"{settings.get('output_template_name') or 'unnamed'} ({len(v)} columns)"
        elif k == "filing_validation":  # the choice's words, not its code (UAT 2, U10)
            v = service.FILING_CHECKS.get(v, v)
        elif k == "acc_purpose":
            v = dict(forms.ACC_PURPOSES).get(v, v)
        out.append((service.jobspec.SETTINGS.get(k, k), v))
    return out


def _run_or_404(request, pk) -> Run:
    run = get_object_or_404(Run.objects.select_related("organisation", "owner", "allocation_version"), pk=pk)
    if not service.can_read(request.user, run):
        raise Http404
    return run


@login_required
def runs(request):
    qs = service.visible_runs(request.user)
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    rows = [(r, status_view(r)) for r in page.object_list]
    return render(
        request,
        "calculations/runs.html",
        {"page": page, "rows": rows, "live": any(not r.ended for r, _ in rows), "can_start": service.default_release() is not None},
    )


def _submittable(user, organisations):
    return [o for o in organisations if service.can_submit(user, o)]


@login_required
def new_run(request):
    releases = list(service.releases())
    if not releases:
        return render(request, "calculations/new.html", {"form": None})
    draft = _draft_for(request)
    orgs = _submittable(request.user, request.user_organisations)
    first = draft.organisation if draft is not None else request.organisation
    if first in orgs:
        orgs.remove(first)
        orgs.insert(0, first)
    default = service.default_release()
    initial = {"engine": f"{default.release}|recet", "acc_version": "2024", "include_societal": True, "emissions": True}
    if draft is not None:
        initial |= {"name": draft.name, "run_for": draft.organisation.code if draft.organisation else ""}
    form = forms.NewRunForm(
        request.POST or None, request.FILES or None, releases=releases, organisations=orgs, user=request.user, draft=draft, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        try:
            if draft is not None:
                run = drafts.start_run(request.user, form.cleaned_data["organisation"], form.run_request(), draft)
            else:
                run = service.create_run_from_upload(
                    request.user, form.cleaned_data["organisation"], form.run_request(), form.cleaned_data["job"]
                )
        except (AccessDenied, drafts.NoEngine) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"{run.name} is queued{' (inputs check only)' if run.settings.get('preflight_only') else ''}.")
            return redirect("calculations:run", pk=run.pk)
    return render(
        request,
        "calculations/new.html",
        {
            "form": form,
            "releases": releases,
            "service": service,
            "draft": draft,
            "unserved": dispatch.unserved(releases),
            "worker_minutes": settings.HEALTH["worker_seen"],
        },
    )


def _draft_for(request):
    """The Edit inputs draft New run runs (?draft= on the page, a hidden field when it is sent), or None."""
    pk = request.POST.get("draft") or request.GET.get("draft")
    if not pk:
        return None
    try:
        return drafts.visible(request.user).get(pk=pk)
    except (InputDraft.DoesNotExist, ValidationError):
        raise Http404 from None


@login_required
def run(request, pk):
    run = _run_or_404(request, pk)
    receipt = run.receipt or {}
    files = service.output_files(run) if run.status != Run.Status.QUEUED else []
    return render(
        request,
        "calculations/run.html",
        {
            "run": run,
            "status": status_view(run),
            "files": files,
            "can_stop": not run.ended and service.can_cancel(request.user, run),
            "calculation": service.calculation_label(run.calculation),
            "settings": settings_view(run.settings),
            "settings_cost_test_set": (run.settings or {}).get("cost_test_set", ""),
            "programs": service.programs_label(run),
            # the reference data packs pinned, less the cost-test set, which has its own row (UAT 2, U10)
            "reference_packs": [
                p for kind, p in (((run.spec or {}).get("context") or {}).get("packs") or {}).items() if kind != PackVersion.Kind.COST_TESTS
            ],
            "engine": receipt.get("engine") or {},
            "packs": ((receipt.get("packs") or {}).get("files") or {}),
            "totals": results_mod.headline_totals(receipt.get("totals") or {}, run.measure_count),
            "other_totals": results_mod.other_totals(receipt.get("totals") or {}),
            "error_text": service.plain_error(run.error),
            "sysadmin": access.is_system_admin(request.user),
            "notes": receipt.get("notes") or [],
            "can_edit_inputs": _can_edit_inputs(request.user, run),
            "cet_comparison": results_mod.cet_comparison(run, files),
        },
    )


def _can_edit_inputs(user, run: Run) -> bool:
    """Edit inputs and rerun: offered on an ended run small enough for Edit inputs, when the engine is installed."""
    if drafts.engine() is None or not run.ended or "job_zip" not in run.inputs:
        return False
    return (run.measure_count or 0) <= drafts.max_rows() and service.can_submit(user, run.organisation)


@never_cache
@login_required
def run_status(request, pk):
    return JsonResponse(status_view(_run_or_404(request, pk)))


@login_required
@require_POST
def stop(request, pk):
    run = _run_or_404(request, pk)
    try:
        service.cancel_run(request.user, run, request.POST.get("reason", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Stopping the run." if run.status == Run.Status.RUNNING else "The run is stopped.")
    return redirect("calculations:run", pk=run.pk)


@login_required
def run_file(request, pk, name):
    """One output file: a pre-signed S3 URL, or streamed from the local store. Never more than the run's own files."""
    run = _run_or_404(request, pk)
    key = service.output_key(run, name)
    if key is None:
        raise Http404
    filename = PurePosixPath(name).name
    store = storage.store("results")
    url = store.download_url(key, filename)
    if url:
        return HttpResponseRedirect(url)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(store.open(key), as_attachment=True, filename=filename, content_type=content_type)


def _needs(missing: list[tuple[str, str]]) -> str:
    """Filings a release lacks the ACC of, in words: "2028 claims (2026 ACC), 2029 claims (2026 ACC)"."""
    return ", ".join(f"{what} ({acc} ACC)" for what, acc in missing)


@login_required
def engines(request):
    """The engine releases runs can use (CPUC system administrators add, set the default and retire them)."""
    if not access.is_system_admin(request.user):
        raise Http404
    form = forms.EngineReleaseForm(request.POST or None)
    if request.method == "POST":
        action = request.POST.get("action", "add")
        try:
            if action == "add" and form.is_valid():
                d = form.cleaned_data
                r = service.add_release(
                    request.user,
                    release=d["release"],
                    calculations=list(d["calculations"]),
                    accs=d["calculations"],
                    image=d["image"],
                    batch_job_definition=d["batch_job_definition"],
                    notes=d["notes"],
                    default=d["default"],
                    reason=d["reason"],
                )
                messages.success(request, f"Added {r}.")
                return redirect("calculations:engines")
            if action in ("default", "retire"):
                r = get_object_or_404(EngineRelease, pk=request.POST.get("release"))
                if action == "default":
                    service.set_default_release(request.user, r)
                    messages.success(request, f"{r} is now the default for new runs.")
                    if missing := service.missing_accs(r):
                        messages.warning(request, f"{r} cannot price {_needs(missing)}: filings price on the default release.")
                else:
                    service.retire_release(request.user, r, request.POST.get("reason", ""))
                    messages.success(request, f"{r} is retired.")
                return redirect("calculations:engines")
        except AccessDenied as e:
            form.add_error(None, str(e))
    return render(
        request,
        "calculations/engines.html",
        {
            "releases": [
                (
                    r,
                    [(service.calculation_label(c), ", ".join(service.accs_for(r, c))) for c in r.calculations],
                    _needs(service.missing_accs(r)) if r.active else "",
                )
                for r in EngineRelease.objects.all()
            ],
            "form": form,
        },
    )


# --- Results and Compare (results.py: ReCET's own results code over the run's group sums)


def _table_params(request, rv, cat, levels):
    """(level, keys, years, sort, descending, text) from the query string. Columns come from the picker's ticks (col) or a comma list
    (columns); the engine's default view otherwise."""
    level = request.GET.get("level", "portfolio")
    level = level if level in {k for k, _ in levels} else "portfolio"
    picked = request.GET.getlist("col") or [k for k in request.GET.get("columns", "").split(",") if k]
    keys = [k for k in picked if k in cat] or [k for k in rv.DEFAULT_VIEW if k in cat]
    return level, keys, rv.parse_years(request.GET.getlist("years")), request.GET.get("sort", ""), request.GET.get("desc") == "1"


def _years(rv, years, known):
    years = [y for y in years if y in known]
    return [] if set(years) == set(known) else years


def _query(**kw) -> str:
    from urllib.parse import urlencode

    return urlencode([(k, x) for k, v in kw.items() for x in (v if isinstance(v, list) else [v]) if x not in (None, "")])


def _cell(rv, cat, column, v):
    if column == "Measures":
        return f"{int(v):,}" if v == v and v is not None else ""
    return rv.format_value(cat.get(column), v)


def _rows(rv, cat, table):
    numeric = {c for c in table.columns if c in cat or c == "Measures"}
    return [[(_cell(rv, cat, c, v), c in numeric) for c, v in zip(table.columns, r, strict=True)] for r in table.itertuples(index=False)]


@login_required
def results(request, pk):
    run = _run_or_404(request, pk)
    ctx = {"run": run}
    try:
        res = results_mod.load(run)
    except results_mod.NoResults as e:
        return render(request, "calculations/results.html", {**ctx, "note": str(e)})
    rv = results_mod.engine()
    cat = res.catalog()
    level, keys, years, sort, desc = _table_params(request, rv, cat, rv.LEVELS)
    pas = res.pas()
    pa = request.GET.get("pa") if request.GET.get("pa") in pas else rv.ALL_PAS
    all_years = res.years()
    years = _years(rv, years, all_years)
    note, table = "", None
    measure_ok = res.measure_level_available()
    if level == "measure" and not measure_ok:
        note = (
            f"This run has {res.n:,} measures: the measure level is shown only for runs of up to {rv.MAX_RUN_MEASURE_ROWS:,}. "
            "Its measure-level table is in the run's files."
        )
    elif level == "measure" and not res.measure_level_allowed(pa, years):
        note = (
            f"The measure level is shown for up to {rv.MAX_MEASURE_ROWS:,} measures: pick one PA or fewer program years, "
            "or download the measure-level file."
        )
    else:
        if level == "measure":
            results_mod.with_measure_rows(run, res)
        table = res.table(level, pa, keys, years)
    text = request.GET.get("q", "")
    state = {"level": level, "pa": pa, "years": years, "columns": ",".join(keys)}
    labels = {k: rv.display_label(c) for k, c in cat.items()}
    head = []
    if table is not None:
        shown_rows = len(table)
        table = results_mod.sort_rows(results_mod.filter_rows(table, text), sort, desc)
        for c in table.columns:
            on = c == sort
            head.append(
                {
                    "key": c,
                    "label": labels.get(c, c),
                    "help": cat[c].help if c in cat else "",
                    "sort": "descending" if on and desc else ("ascending" if on else None),
                    "href": "?" + _query(**state, q=text, sort=c, desc="" if on and desc else ("1" if on or c in cat else "")),
                }
            )
    ctx.update(
        {
            "res": res,
            "level": level,
            "levels": [
                {
                    "key": k,
                    "label": label,
                    "href": "?" + _query(**{**state, "level": k}),
                    "off": k == "measure" and not measure_ok,
                }
                for k, label in reversed(rv.LEVELS)
            ],
            "pa": pa,
            "pas": pas,
            "all_pas": rv.ALL_PAS,
            "all_years": all_years,
            "years": years,
            "keys": keys,
            "groups": results_mod.picker_groups(cat),
            "default_keys": [k for k in rv.DEFAULT_VIEW if k in cat],
            "tiles": results_mod.tiles(res, pa, years),
            "chart": results_mod.sector_bars(res, pa, years),
            "note": note,
            "table": table,
            "head": head,
            "rows": _rows(rv, cat, table) if table is not None else [],
            "filtered_from": shown_rows if table is not None and text else None,
            "text": text,
            "csv": "?" + _query(**state),
            "definitions": [(labels[c], cat[c].help) for c in (table.columns if table is not None else []) if c in cat and cat[c].help],
            "measure_limit": rv.MAX_RUN_MEASURE_ROWS,
        }
    )
    return render(request, "calculations/results.html", ctx)


def _csv_response(table, name):
    from django.http import HttpResponse

    resp = HttpResponse(table.to_csv(index=False), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@login_required
def results_csv(request, pk):
    run = _run_or_404(request, pk)
    try:
        res = results_mod.load(run)
    except results_mod.NoResults:
        raise Http404 from None
    rv = results_mod.engine()
    cat = res.catalog()
    level, keys, years, _sort, _desc = _table_params(request, rv, cat, rv.LEVELS)
    pa = request.GET.get("pa") if request.GET.get("pa") in res.pas() else rv.ALL_PAS
    years = _years(rv, years, res.years())
    if level == "measure":
        if not res.measure_level_allowed(pa, years):
            raise Http404
        results_mod.with_measure_rows(run, res)
    t = res.table(level, pa, keys, years).rename(columns={k: c.label for k, c in cat.items()})
    return _csv_response(t, f"cedars_run_{run.pk}_{level}.csv")


def _compare_candidates(user, run):
    return service.visible_runs(user).filter(status=Run.Status.SUCCEEDED).exclude(pk=run.pk).order_by("-created_at")[:100]


def _compare(request, pk):
    a = _run_or_404(request, pk)
    other = request.GET.get("with", "")
    try:
        b = _run_or_404(request, other) if other else None
    except (ValueError, ValidationError):
        raise Http404 from None
    return a, b


@login_required
def compare(request, pk):
    a, b = _compare(request, pk)
    ctx = {"a": a, "b": b, "candidates": _compare_candidates(request.user, a)}
    if b is None:
        return render(request, "calculations/compare.html", {**ctx, "note": "Pick a run to compare this one with."})
    try:
        ra, rb = results_mod.load(a), results_mod.load(b)
    except results_mod.NoResults as e:
        return render(request, "calculations/compare.html", {**ctx, "note": str(e)})
    rv = results_mod.engine()
    cat = {**rb.catalog(), **ra.catalog()}
    level, keys, years, _sort, _desc = _table_params(request, rv, cat, rv.COMPARE_LEVELS)
    known = sorted(set(ra.years()) | set(rb.years()))
    years = _years(rv, years, known)
    table = rv.compare(ra, rb, level, keys, years=years)
    labels = {k: rv.display_label(c) for k, c in cat.items()}
    ids = [c for c in table.columns if "__" not in c]
    values = ["Measures"] + [k for k in keys if k in cat]
    rows = []
    for r in table.to_dict("records"):
        cells = [(str(r[c]) if r[c] == r[c] else "", False) for c in ids]
        for k in values:
            for part in ("a", "b", "diff"):
                cells.append((_cell(rv, cat, k, r[f"{k}__{part}"]), True))
            p = r[f"{k}__pct"]
            cells.append((f"{p:+.1%}" if isinstance(p, int | float) and p == p else "", True))
        rows.append(cells)
    state = {"with": str(b.pk), "level": level, "years": years, "columns": ",".join(keys)}
    ctx.update(
        {
            "level": level,
            "levels": [{"key": k, "label": label, "href": "?" + _query(**{**state, "level": k})} for k, label in rv.COMPARE_LEVELS],
            "all_years": known,
            "years": years,
            "keys": keys,
            "ids": ids,
            "values": [(k, labels.get(k, k), cat[k].help if k in cat else "") for k in values],
            "rows": rows,
            "csv": "?" + _query(**state),
            "calc_a": service.calculation_label(a.calculation),
            "calc_b": service.calculation_label(b.calculation),
            "match": "PrgID" if level == "program" else "name",
        }
    )
    return render(request, "calculations/compare.html", ctx)


@login_required
def compare_csv(request, pk):
    a, b = _compare(request, pk)
    if b is None:
        raise Http404
    try:
        ra, rb = results_mod.load(a), results_mod.load(b)
    except results_mod.NoResults:
        raise Http404 from None
    rv = results_mod.engine()
    cat = {**rb.catalog(), **ra.catalog()}
    level, keys, years, _sort, _desc = _table_params(request, rv, cat, rv.COMPARE_LEVELS)
    years = _years(rv, years, sorted(set(ra.years()) | set(rb.years())))
    table = rv.compare(ra, rb, level, keys, years=years)
    side = {"a": f"A: {a.name}", "b": f"B: {b.name}", "diff": "difference", "pct": "% change"}

    def name(c: str) -> str:
        k, _, part = c.rpartition("__")
        return f"{cat[k].label if k in cat else k} ({side[part]})" if k and part in side else c

    return _csv_response(table.rename(columns=name), f"cedars_compare_{a.pk}_{b.pk}_{level}.csv")
