import mimetypes
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from cedars.core import storage, tables, zipstream
from cedars.core.templatetags.ui import status_tone
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import cost_tests
from cedars.services.refdata.models import PackVersion

from . import dispatch, drafts, forms, input_templates, result_views, run_files, service, walkchart
from . import results as results_mod
from . import walkthrough as walk
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
    """A length of time in words: "under a second", "under a minute", "about 3 min", "about 1.5 h"."""
    if seconds is None:
        return ""
    if seconds < 1:
        return "under a second"
    seconds = int(seconds)
    if seconds < 60:
        return "under a minute"
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
        out["text"] = f"Finished in {duration(run.seconds)}." if run.seconds is not None else "Finished."
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
            v = dict(service.acc_purposes()).get(v, v)
        elif k == "market_effects":
            v = f"{float(v):.0%}"
        elif k == "compare_with":
            v = f"{v} ACC"
        elif k == "profile_years":
            v = ", ".join(str(y) for y in v)
        out.append((service.jobspec.SETTINGS.get(k, k), v))
    return out


def settings_line(run: Run) -> str:
    """The runs list's one line of a run's settings (ReCET's settings_summary): what differs from New run's defaults, and the set."""
    st = run.settings or {}
    parts = []
    if run.cost_tests.get("id"):
        parts.append(f"cost tests {run.cost_tests['id']}{' (custom file)' if run.cost_tests.get('status') == 'custom' else ''}")
    if st.get("include_societal") is False:
        parts.append("SCT off")
    if st.get("emissions") is False:
        parts.append("emissions off")
    if st.get("acc_purpose"):
        parts.append(f"priced for {dict(forms.ACC_PURPOSES).get(st['acc_purpose'], st['acc_purpose']).lower()}")
    if st.get("filing_validation"):
        parts.append(service.FILING_CHECKS.get(st["filing_validation"], st["filing_validation"]))
    if st.get("allocate_statewide"):
        parts.append("statewide allocated")
    if st.get("output_template"):
        parts.append(f"template {st.get('output_template_name') or 'unnamed'}")
    parts.append(run.output_format)
    return " · ".join(parts)


def _run_or_404(request, pk) -> Run:
    run = get_object_or_404(Run.objects.select_related("organisation", "owner", "allocation_version"), pk=pk)
    if not service.can_read(request.user, run):
        raise Http404
    return run


# The Runs list's columns (UI review 3, U9): newest first by default; any sortable heading sorts the whole list, not just the page.
RUN_COLUMNS = [
    tables.Column("name", "Run", field=("name", "created_at")),
    tables.Column("started", "Started", descending_first=True, field="created_at"),
    tables.Column("for", "For", field="organisation__code"),
    tables.Column("engine", "Engine", field=("calculation", "engine_release")),
    tables.Column("acc", "ACC", field="acc_version"),
    tables.Column("measures", "Measures", numeric=True, descending_first=True, field="measure_count"),
    tables.Column("status", "Status", field="status"),
    tables.Column("time", "Run time", descending_first=True, field="run_time"),
    tables.Column("by", "Started by", field=("owner__email",)),
]


@login_required
def runs(request):
    sort = tables.sorting(request.GET, RUN_COLUMNS, "-started")
    qs = service.visible_runs(request.user).annotate(run_time=F("finished_at") - F("started_at")).order_by(*sort.ordering(RUN_COLUMNS))
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    deletion = service.deletion_dates(page.object_list)
    rows = [
        (r, status_view(r), {"settings": settings_line(r), "time": duration(r.seconds), "deletion": deletion.get(r.pk, "")})
        for r in page.object_list
    ]
    return render(
        request,
        "calculations/runs.html",
        {
            "page": page,
            "rows": rows,
            "sort": sort,
            "query": tables.clean(request.GET).urlencode(),
            "live": any(not r.ended for r, *_ in rows),
            "can_start": service.default_release() is not None,
        },
    )


def _submittable(user, organisations):
    return [o for o in organisations if service.can_submit(user, o)]


@login_required
def new_run(request):
    releases = list(service.releases())
    if not releases:
        return render(request, "calculations/new.html", {"form": None})
    draft = _draft_for(request)
    rerun = _rerun_for(request) if draft is None else None
    orgs = _submittable(request.user, request.user_organisations)
    first = draft.organisation if draft is not None else rerun.organisation if rerun is not None else request.organisation
    if first in orgs:
        orgs.remove(first)
        orgs.insert(0, first)
    default = service.default_release()
    initial = {"engine": f"{default.release}|recet", "acc_version": "2024", "include_societal": True, "emissions": True}
    if draft is not None:
        initial |= {"name": draft.name, "run_for": draft.organisation.code if draft.organisation else ""}
    if rerun is not None:
        initial |= forms.rerun_initial(rerun)
    form = forms.NewRunForm(
        request.POST or None,
        request.FILES or None,
        releases=releases,
        organisations=orgs,
        user=request.user,
        draft=draft,
        rerun=rerun,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            if draft is not None:
                run = drafts.start_run(request.user, form.cleaned_data["organisation"], form.run_request(), draft)
            elif rerun is not None:
                run = _start_rerun(request, form, rerun)
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
            "rerun": rerun,
            "unserved": dispatch.unserved(releases),
            "worker_minutes": settings.HEALTH["worker_seen"],
            "input_templates": _input_template_links(releases),
            "cost_test_sets": _cost_test_sets(),
            "acc_table": service.acc_table(releases),
        },
    )


def _cost_test_sets() -> list[dict]:
    """New run's "What each cost-test set prices": every registered set not withdrawn, with its program years and the tests it writes."""
    out = []
    packs = PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS).exclude(status=PackVersion.Status.WITHDRAWN)
    for p in packs.order_by("label", "-registered_at"):
        h = cost_tests.header(p)
        headline = set(h.get("headline") or ())
        out.append(
            {
                "id": p.label,
                "version": p.version,
                "label": h.get("label", ""),
                "status": p.get_status_display(),
                "years": cost_tests.years_text(p),
                "tests": [{"id": t, "label": label, "headline": t in headline} for t, label in (h.get("tests") or {}).items()],
            }
        )
    return out


def _template_accs(releases) -> list[str]:
    """The ACCs ReCET's own calculation has in the active releases, newest first."""
    return sorted({a for r in releases for a in service.accs_for(r, service.RECET)}, reverse=True)


def _input_template_links(releases) -> list[dict] | None:
    """New run's template downloads: [{label, links: [(text, url)]}], one link per ACC for a template that differs by ACC. None when
    the engine is not installed here (the templates are its own)."""
    from django.urls import reverse

    if input_templates.engine() is None:
        return None
    accs = _template_accs(releases)
    out = []
    for t in input_templates.TEMPLATES.values():
        url = reverse("calculations:input_template", args=[t.kind])
        if t.by_acc and accs:
            links = [(f"{a} ACC", f"{url}?acc={a}") for a in accs]
        else:
            links = [(t.file, url)]
        out.append({"label": t.label, "file": t.file, "links": links})
    return out


@login_required
def input_template(request, kind):
    """One of the engine's input templates (input_templates.TEMPLATES), for New run."""
    if kind not in input_templates.TEMPLATES:
        raise Http404("No such template.")
    accs = _template_accs(service.releases())
    acc = request.GET.get("acc") or (accs[0] if accs else "2026")
    if acc not in accs:
        raise Http404("No such ACC.")
    try:
        f = input_templates.path(kind, acc)
    except LookupError:
        raise Http404("The engine is not installed on this server, so its templates are not available.") from None
    name = f.name if not input_templates.TEMPLATES[kind].by_acc else f.name.replace("template_", f"template_{acc}_acc_", 1)
    return FileResponse(open(f, "rb"), as_attachment=True, filename=name)


def _rerun_for(request):
    """The run New run starts again with different settings (?from= on the page, a hidden field when it is sent), or None. Anyone who
    can see the run can rerun it as a run of their own choosing."""
    pk = request.POST.get("from") or request.GET.get("from")
    if not pk:
        return None
    try:
        return _run_or_404(request, pk)
    except ValidationError:
        raise Http404 from None


def _start_rerun(request, form, rerun):
    """Queue the rerun: the run's stored inputs New run keeps (form.reused()), with any uploaded in their place."""
    req = form.run_request()
    reused = form.reused()
    with service.reused_inputs(rerun, reused) as stored:
        job = form.cleaned_data.get("job") or stored.get("job_zip") or stored.get("workbook")
        if job is None:
            raise AccessDenied("The run's job file is no longer stored; upload the job file.")
        req.supplied = {**{n: p for n, p in stored.items() if n in forms.SUPPLIED_FIELDS}, **(req.supplied or {})}
        if req.cet_outputs is None and "cet_outputs" in stored:
            req.cet_outputs = stored["cet_outputs"]
        organisation = form.cleaned_data["organisation"]
        if isinstance(job, Path):
            return service.create_run(request.user, organisation, req, job)
        return service.create_run_from_upload(request.user, organisation, req, job)


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
    log, log_cut = results_mod.run_log(run) if run.status != Run.Status.QUEUED else ("", False)
    return render(
        request,
        "calculations/run.html",
        {
            "run": run,
            "status": status_view(run),
            "files": files,
            "file_groups": run_files.grouped(files),
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
            "input_notes": ((run.spec or {}).get("context") or {}).get("notes") or [],
            "can_edit_inputs": _can_edit_inputs(request.user, run),
            "can_rerun": bool(service.job_input(run)),
            "can_delete": _can_delete(request.user, run),
            "can_rename": run.owner_id == request.user.pk,
            "cet_comparison": results_mod.cet_comparison(run, files),
            "check": results_mod.input_check(run) if run.status != Run.Status.QUEUED else None,
            "log": log,
            "log_cut": log_cut,
            "inputs": _input_files(run),
            "deletion": service.deletion_dates([run]).get(run.pk, ""),
        },
    )


def _input_files(run: Run) -> list[dict]:
    """The run's inputs as given (job zip or workbook, supplied files, the registry's programs, the allocation table), to download."""
    return [
        {"name": name, "filename": PurePosixPath(f["key"]).name, "sha256": f.get("sha256", "")}
        for name, f in sorted((run.inputs or {}).items())
        if f.get("key")
    ]


def _can_edit_inputs(user, run: Run) -> bool:
    """Edit inputs and rerun: offered on an ended run small enough for Edit inputs, when the engine is installed."""
    if drafts.engine() is None or not run.ended or not service.job_input(run):
        return False
    return (run.measure_count or 0) <= drafts.max_rows() and service.can_submit(user, run.organisation)


@never_cache
@login_required
def run_status(request, pk):
    return JsonResponse(status_view(_run_or_404(request, pk)))


def _can_delete(user, run: Run) -> bool:
    """Whether the page offers Delete this run: one's own ("Only me") ended run not priced into a filing (service.delete_run decides)."""
    return run.organisation_id is None and run.owner_id == user.pk and run.ended and run.submission_version_id is None


@login_required
def delete(request, pk):
    """Delete one's own ("Only me") ended run and its files (R9); audited. GET asks first (Jake, 2026-11-04: the button by Results asks
    for confirmation before deleting); the confirmation's POST deletes."""
    run = _run_or_404(request, pk)
    if request.method == "GET":
        if not _can_delete(request.user, run):
            messages.error(request, "Only your own ended runs (for Only me) can be deleted.")
            return redirect("calculations:run", pk=run.pk)
        files = service.output_files(run)
        return render(request, "calculations/run_delete.html", {"run": run, "files": len(files)})
    if request.method != "POST":
        return HttpResponse(status=405)
    if not request.POST.get("sure"):
        messages.error(request, "Confirm that the run and its files go for good.")
        return redirect("calculations:delete", pk=run.pk)
    name = run.name
    try:
        service.delete_run(request.user, run)
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("calculations:run", pk=run.pk)
    messages.success(request, f"Deleted the run {name} and its files.")
    return redirect("calculations:runs")


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
@require_POST
def rename(request, pk):
    """The owner renames a run or changes its note (nothing else about a run changes)."""
    run = _run_or_404(request, pk)
    try:
        service.rename_run(request.user, run, name=request.POST.get("name", ""), note=request.POST.get("note", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Name and note saved.")
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


@never_cache
@login_required
def run_log(request, pk):
    """The run's whole log as text: the run page's Log reads it while the run runs, and it opens or downloads as a file."""
    run = _run_or_404(request, pk)
    text, _cut = results_mod.run_log(run, limit=None)
    if not text and run.ended:
        raise Http404("This run kept no log.")
    resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
    if request.GET.get("download"):
        resp["Content-Disposition"] = f'attachment; filename="{run.job_id}_log.txt"'
    return resp


def _zip_name(run: Run, what: str) -> str:
    return f"{run.job_id}_{what}.zip"


@login_required
def run_zip(request, pk):
    """Every output file of an ended run in one zip, written as it is sent (never built in memory or on the server's disk)."""
    run = _run_or_404(request, pk)
    if not run.ended:
        raise Http404("The files are ready to download together once the run has ended.")
    files = service.output_files(run)
    if not files:
        raise Http404("This run wrote no files.")
    store = storage.store("results")
    entries = [(f["name"], lambda k=run.storage_prefix + "out/" + f["name"]: store.open(k)) for f in files]
    resp = StreamingHttpResponse(zipstream.stream(entries), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{_zip_name(run, "files")}"'
    return resp


@login_required
def run_input(request, pk, name):
    """One of the run's inputs, as it was given."""
    run = _run_or_404(request, pk)
    f = next((i for i in _input_files(run) if i["name"] == name), None)
    if f is None:
        raise Http404
    key = run.inputs[name]["key"]
    store = storage.store("uploads")
    url = store.download_url(key, f["filename"])
    if url:
        return HttpResponseRedirect(url)
    content_type = mimetypes.guess_type(f["filename"])[0] or "application/octet-stream"
    return FileResponse(store.open(key), as_attachment=True, filename=f["filename"], content_type=content_type)


@login_required
def run_inputs_zip(request, pk):
    """Every input of the run in one zip, each under its own file name, as it was given."""
    run = _run_or_404(request, pk)
    inputs = _input_files(run)
    if not inputs:
        raise Http404
    store = storage.store("uploads")
    seen, entries = set(), []
    for f in inputs:
        arc = f["filename"] if f["filename"] not in seen else f"{f['name']}_{f['filename']}"
        seen.add(arc)
        entries.append((arc, lambda k=run.inputs[f["name"]]["key"]: store.open(k)))
    resp = StreamingHttpResponse(zipstream.stream(entries), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{_zip_name(run, "inputs")}"'
    return resp


def _needs(missing: list[tuple[str, str]]) -> str:
    """Filings a release lacks the ACC of, in words: "2028 claims (2026 ACC), 2029 claims (2026 ACC)"."""
    return ", ".join(f"{what} ({acc} ACC)" for what, acc in missing)


@login_required
def engines(request):
    """The engine releases runs can use (CPUC system administrators add, change, set the default, retire and return them to service)."""
    if not access.is_system_admin(request.user):
        raise Http404
    current = service.default_release()
    form = forms.EngineReleaseForm(request.POST or None, current_default=current)
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
                    retire_previous=bool(d.get("retire_previous")),
                    reason=d["reason"],
                )
                retired = current is not None and d["default"] and d.get("retire_previous")
                messages.success(request, f"Added {r}." + (f" {current} is retired." if retired else ""))
                if r.default and (missing := service.missing_accs(r)):
                    messages.warning(request, f"{r} cannot price {_needs(missing)}: filings price on the default release.")
                return redirect("calculations:engines")
            if action in ("default", "retire", "restore"):
                r = get_object_or_404(EngineRelease, pk=request.POST.get("release"))
                if action == "default":
                    service.set_default_release(request.user, r)
                    messages.success(request, f"{r} is now the default for new runs.")
                    if missing := service.missing_accs(r):
                        messages.warning(request, f"{r} cannot price {_needs(missing)}: filings price on the default release.")
                elif action == "retire":
                    service.retire_release(request.user, r, request.POST.get("reason", ""))
                    messages.success(request, f"{r} is retired.")
                else:
                    service.restore_release(request.user, r, request.POST.get("reason", ""))
                    messages.success(request, f"{r} is back in service.")
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
                    Run.objects.filter(engine_release=r.release).count(),
                )
                for r in EngineRelease.objects.all()
            ],
            "form": form,
        },
    )


@login_required
def engine_release(request, pk):
    """Change one release: its calculations and ACCs, worker image, Batch job definition and notes (never its number)."""
    if not access.is_system_admin(request.user):
        raise Http404
    r = get_object_or_404(EngineRelease, pk=pk)
    initial = {
        "calculations": service.calculations_text(r),
        "image": r.image,
        "batch_job_definition": r.batch_job_definition,
        "notes": r.notes,
    }
    form = forms.EngineReleaseChangeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            service.change_release(
                request.user,
                r,
                calculations=list(d["calculations"]),
                accs=d["calculations"],
                image=d["image"],
                batch_job_definition=d["batch_job_definition"],
                notes=d["notes"],
                reason=d["reason"],
            )
        except AccessDenied as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Saved {r}.")
            if r.default and (missing := service.missing_accs(r)):
                messages.warning(request, f"{r} cannot price {_needs(missing)}: filings price on the default release.")
            return redirect("calculations:engines")
    runs = Run.objects.filter(engine_release=r.release).count()
    return render(request, "calculations/engine_release.html", {"release": r, "form": form, "runs": runs})


# --- Results and Compare (results.py: ReCET's own results code over the run's group sums)


def _table_params(request, rv, cat, levels):
    """(level, keys, years, sort, descending, text) from the query string. Columns come from the picker's ticks (col) or a comma list
    (columns); the engine's default view otherwise."""
    level = request.GET.get("level", "portfolio")
    level = level if level in {k for k, _ in levels} else "portfolio"
    picked = request.GET.getlist("col") or [k for k in request.GET.get("columns", "").split(",") if k]
    if not picked and request.GET.get("view"):
        view = result_views.get(request.user, request.GET["view"])
        picked = list(view.columns) if view else []
    keys = [k for k in picked if k in cat] or [k for k in rv.DEFAULT_VIEW if k in cat]
    return level, keys, rv.parse_years(request.GET.getlist("years")), request.GET.get("sort", ""), request.GET.get("desc") == "1"


def _years(rv, years, known):
    years = [y for y in years if y in known]
    return [] if set(years) == set(known) else years


def _query(**kw) -> str:
    from urllib.parse import urlencode

    return urlencode([(k, x) for k, v in kw.items() for x in (v if isinstance(v, list) else [v]) if x not in (None, "")])


def _picker(request, cat, keys, state: dict) -> dict:
    """The column picker's context, shared by Results and Compare: the two lists' catalog and labels (field_picker.js; the columns
    shown are sent in order as `columns`), and, for the picker without scripts, the groups (narrowed by Find over names and
    definitions), which boxes are ticked (none after Clear), the ticked columns Find hides (kept as hidden fields so Show keeps
    them), the person's saved views, and the query that rebuilds the page without the picker's own parameters."""
    find = request.GET.get("find", "").strip()[:80]
    clear = request.GET.get("clear") == "1"
    groups = every = results_mod.picker_groups(cat)
    if find:
        f = find.lower()
        groups = [(g, [i for i in items if f in i[0].lower() or f in i[1].lower() or f in (i[2] or "").lower()]) for g, items in groups]
        groups = [(g, items) for g, items in groups if items]
    shown = {k for _, items in groups for k, _, _ in items}
    ticked = [] if clear else list(keys)
    views = list(result_views.visible(request.user))
    chosen = request.GET.get("view", "")
    return {
        "groups": groups,
        "ticked": ticked,
        "hidden_keys": [k for k in ticked if k not in shown],
        "find": find,
        "clear": clear,
        "open": bool(request.GET.get("col") or find or clear or request.GET.get("picker")),
        "views": views,
        "view": next((v for v in views if str(v.pk) == chosen), None),
        "state": [(k, x) for k, v in state.items() for x in (v if isinstance(v, list) else [v]) if x not in (None, "")],
        "base": "?" + _query(**state),
        "columns": ",".join(keys),
        "count": len(keys),
        # the two lists (field_picker.js): every column with its label, group and definition, in the engine's order
        "catalog": [{"c": k, "t": label, "g": g, "d": help or ""} for g, items in every for k, label, help in items],
        "field_picker": {
            "target": "pick-columns",
            "sep": "comma",
            "noun": "the table",
            "kinds": False,
            "groups": [g for g, _items in every],
            "left": "Columns you can add",
            "right": "Columns shown, in table order",
            "find_right": "Find in the columns shown",
        },
    }


def _cell(rv, cat, column, v):
    if column == "Measures":
        return f"{int(v):,}" if v == v and v is not None else ""
    return rv.format_value(cat.get(column), v)


def _rows(rv, cat, table):
    numeric = {c for c in table.columns if c in cat or c == "Measures"}
    return [[(_cell(rv, cat, c, v), c in numeric) for c, v in zip(table.columns, r, strict=True)] for r in table.itertuples(index=False)]


def _acc_tabs(run: Run, acc: str) -> list[dict]:
    """A comparison run's tabs on Results: each ACC's tables, then the comparison of the two."""
    parts = results_mod.acc_parts(run)
    if not parts:
        return []
    tabs = [{"key": a, "label": f"{a} ACC" + (" (the run's own)" if a == run.acc_version else ""), "href": f"?acc={a}"} for a in parts]
    tabs.append({"key": "comparison", "label": "Comparison of the two", "href": "?acc=comparison"})
    for t in tabs:
        t["on"] = t["key"] == acc
    return tabs


def _results_acc(request, run: Run) -> str:
    """The ACC tab asked for (?acc=), the run's own when it names none or one the run does not have; "" for a run with one ACC."""
    parts = results_mod.acc_parts(run)
    if not parts:
        return ""
    asked = request.GET.get("acc", "")
    return asked if asked in parts or asked == "comparison" else parts[0]


@login_required
def results(request, pk):
    run = _run_or_404(request, pk)
    acc = _results_acc(request, run)
    ctx = {"run": run, "acc": acc, "acc_tabs": _acc_tabs(run, acc)}
    if acc == "comparison":
        ctx["comparison"] = results_mod.acc_comparison(run)
        if ctx["comparison"] is None:
            ctx["note"] = "This run's comparison of the two ACCs is not available (the run has not finished, or wrote none)."
        return render(request, "calculations/results.html", ctx)
    try:
        res = results_mod.load(run, acc or None)
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
    state = {"acc": acc, "level": level, "pa": pa, "years": years, "columns": ",".join(keys)}
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
            "picker": _picker(request, cat, keys, {"acc": acc, "level": level, "pa": pa, "years": years}),
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
def walkthrough(request, pk):
    """Walk through one measure's cost-effectiveness calculation, step by step (walkthrough.py): first pick a measure of the run (any
    measure, searched by ID, description, program or measure code), then follow its steps one at a time or all on one page. A run that
    priced a claim, budget filing or monthly report is open to that filing's readers too (a published one to every signed-in user),
    who reach it from the filing's version page and may not read the run itself."""
    from cedars.services.submissions import service as submissions

    run = get_object_or_404(Run.objects.select_related("organisation", "owner", "allocation_version"), pk=pk)
    run_readable = service.can_read(request.user, run)
    filings = [
        {
            "label": f"{v.submission.plain_name}, version {v.number}",
            "href": submissions.version_page(v),
        }
        for v in submissions.versions_priced_by(request.user, run.pk)
    ]
    filings = [f for f in filings if f["href"]]
    if not run_readable and not filings:
        raise Http404
    parts = results_mod.acc_parts(run)
    acc = request.GET.get("acc", "") if request.GET.get("acc", "") in parts else (parts[0] if parts else "")
    measure = request.GET.get("measure", "").strip()[:200]
    q = request.GET.get("q", "").strip()[:100]
    show_all = request.GET.get("all") == "1"
    step_key = request.GET.get("step") if request.GET.get("step") in walk.STEP_KEYS else walk.STEP_KEYS[0]
    ctx = {
        "run": run,
        "run_readable": run_readable,
        "filings": filings,
        "acc": acc,
        "acc_tabs": [
            {
                "label": f"{a} ACC" + (" (the run's own)" if a == run.acc_version else ""),
                "href": "?" + _query(acc=a, measure=measure),
                "on": a == acc,
            }
            for a in parts
        ],
        "measure": measure,
        "q": q,
        "show_all": show_all,
        "pick_limit": walk.PICK_LIMIT,
    }
    try:
        if measure:
            found = walk.measure_row(run, acc or None, measure)
            if found is None:
                ctx["missing"] = measure
                ctx["measure"] = measure = ""
            else:
                row, repeated = found
                settings = walk.run_settings(run, acc or None)
                engine = walk.Engine.of(run, settings)
                cset = None if engine.replica else walk.cost_test_set(run, acc or None, row)  # a replica prices CET's own tests
                packs = None  # the load-shape and value charts read the run's packs: only on the steps that show them
                if show_all or step_key in ("timing", "value"):
                    packs = walkchart.pack_series(run, acc, row, settings, engine)
                steps = walk.build(row, settings, cset, repeated, engine, packs)
                index = walk.STEP_KEYS.index(step_key)
                link = lambda k: "?" + _query(acc=acc, measure=measure, step=k)  # noqa: E731
                ctx.update(
                    {
                        "row_title": walk.text(row.get("MeasDescription")) or walk.text(row.get("MeasCode")),
                        "steps": steps,
                        "step": steps[index],
                        "nav": [
                            {"number": s.number, "title": s.title, "href": link(s.key), "on": s.key == step_key and not show_all}
                            for s in steps
                        ],
                        "previous": {"title": steps[index - 1].title, "href": link(steps[index - 1].key)} if index > 0 else None,
                        "next": {"title": steps[index + 1].title, "href": link(steps[index + 1].key)} if index + 1 < len(steps) else None,
                        "all_href": "?" + _query(acc=acc, measure=measure, all="1"),
                        "one_href": link(step_key),
                        "file": walk.file_name(run, acc or None),
                        "checked": walk.checks(steps),
                    }
                )
        if not measure:
            ctx["choices"], ctx["matches"] = walk.choices(run, acc or None, q)
    except walk.NoWalkthrough as e:
        ctx["note"] = str(e)
    return render(request, "calculations/walkthrough.html", ctx)


@login_required
def results_csv(request, pk):
    run = _run_or_404(request, pk)
    acc = _results_acc(request, run)
    try:
        res = results_mod.load(run, acc or None)
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
    return _csv_response(t, f"cedars_run_{run.pk}_{level}{f'_acc{acc}' if acc else ''}.csv")


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
    shown_rows = len(table)
    text = request.GET.get("q", "")
    table = results_mod.filter_rows(table, text)
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
            "picker": _picker(request, cat, keys, {"with": str(b.pk), "level": level, "years": years}),
            "text": text,
            "filtered_from": shown_rows if text else None,
            "definitions": [(labels[k], results_mod.definition(k, cat[k])) for k in keys if k in cat],
            "level": level,
            "levels": [{"key": k, "label": label, "href": "?" + _query(**{**state, "level": k})} for k, label in rv.COMPARE_LEVELS],
            "all_years": known,
            "years": years,
            "keys": keys,
            "ids": ids,
            "values": [(k, labels.get(k, k), cat[k].help if k in cat else "") for k in values],
            "rows": rows,
            "csv": "?" + _query(**state, q=text),
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
    table = results_mod.filter_rows(rv.compare(ra, rb, level, keys, years=years), request.GET.get("q", ""))
    side = {"a": f"A: {a.name}", "b": f"B: {b.name}", "diff": "difference", "pct": "% change"}

    def name(c: str) -> str:
        k, _, part = c.rpartition("__")
        return f"{cat[k].label if k in cat else k} ({side[part]})" if k and part in side else c

    return _csv_response(table.rename(columns=name), f"cedars_compare_{a.pk}_{b.pk}_{level}.csv")


def _back(request, fallback):
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next", "")
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if ok and nxt.startswith("/") else fallback


@login_required
@require_POST
def result_view_save(request):
    """Save the columns shown on Results or Compare as one of the person's views, then go back to the page."""
    back = _back(request, reverse("calculations:runs"))
    columns = [c for c in request.POST.get("columns", "").split(",") if c]
    try:
        view = result_views.save(request.user, request.POST.get("name", ""), columns)
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect(back)
    messages.success(request, f'Saved the view "{view.name}" ({len(view.columns)} column{"s" if len(view.columns) != 1 else ""}).')
    return redirect(back)


@login_required
@require_POST
def result_view_delete(request, pk):
    view = result_views.get(request.user, pk)
    if view is None:
        raise Http404
    name = view.name
    result_views.delete(request.user, view)
    messages.success(request, f'Deleted the view "{name}".')
    return redirect(_back(request, reverse("calculations:runs")))
