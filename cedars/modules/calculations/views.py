import mimetypes
from pathlib import PurePosixPath

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import forms, service
from .models import EngineRelease, Run

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
            f"Starts in {duration(est['starts_in'])}, done in {duration(est['done_in'])} (estimated)."
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
    orgs = _submittable(request.user, request.user_organisations)
    if request.organisation in orgs:
        orgs.remove(request.organisation)
        orgs.insert(0, request.organisation)
    default = service.default_release()
    form = forms.NewRunForm(
        request.POST or None,
        request.FILES or None,
        releases=releases,
        organisations=orgs,
        initial={"engine": f"{default.release}|recet", "acc_version": "2024", "include_societal": True, "emissions": True},
    )
    if request.method == "POST" and form.is_valid():
        try:
            run = service.create_run_from_upload(
                request.user, form.cleaned_data["organisation"], form.run_request(), form.cleaned_data["job"]
            )
        except AccessDenied as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"{run.name} is queued.")
            return redirect("calculations:run", pk=run.pk)
    return render(request, "calculations/new.html", {"form": form, "releases": releases, "service": service})


@login_required
def run(request, pk):
    run = _run_or_404(request, pk)
    receipt = run.receipt or {}
    return render(
        request,
        "calculations/run.html",
        {
            "run": run,
            "status": status_view(run),
            "files": service.output_files(run) if run.status != Run.Status.QUEUED else [],
            "can_stop": not run.ended and service.can_cancel(request.user, run),
            "calculation": service.calculation_label(run.calculation),
            "settings": [(service.jobspec.SETTINGS.get(k, k), v) for k, v in sorted(run.settings.items())],
            "engine": receipt.get("engine") or {},
            "packs": ((receipt.get("packs") or {}).get("files") or {}),
            "totals": receipt.get("totals") or {},
            "notes": receipt.get("notes") or [],
            "can_edit_inputs": _can_edit_inputs(request.user, run),
        },
    )


def _can_edit_inputs(user, run: Run) -> bool:
    """Edit inputs and rerun: offered on an ended run small enough for Edit inputs, when the engine is installed."""
    from . import drafts

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
                )
                messages.success(request, f"Added {r}.")
                return redirect("calculations:engines")
            if action in ("default", "retire"):
                r = get_object_or_404(EngineRelease, pk=request.POST.get("release"))
                if action == "default":
                    service.set_default_release(request.user, r)
                    messages.success(request, f"{r} is now the default for new runs.")
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
                (r, [(service.calculation_label(c), ", ".join(service.accs_for(r, c))) for c in r.calculations])
                for r in EngineRelease.objects.all()
            ],
            "form": form,
        },
    )
