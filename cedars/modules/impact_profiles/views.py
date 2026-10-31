"""The Impact profiles module's pages. Every change goes through the module's functions (etrm, loadshapes, generation, custom, library),
which check access and write the audit events; a refused action comes back as AccessDenied with a sentence shown as it is."""

import csv
import io

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import access, custom, etrm, forms, jobs, loadshapes, shapes
from .models import (
    EtrmSyncRun,
    ExcludedPackage,
    ImpactProfile,
    LibraryExport,
    LoadShape,
    LoadShapeUpload,
    MeasurePackage,
    ProcessingJob,
    ProfileMapping,
)


def _query(request) -> str:
    q = request.GET.copy()
    q.pop("page", None)
    return q.urlencode()


def _refused(request, e, to):
    messages.error(request, str(e))
    return redirect(to)


def _csv_response(rows: list[list], header: list[str], filename: str) -> HttpResponse:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --- the library


@login_required
def library(request):
    form = forms.LibraryFilterForm(request.GET or None)
    qs = ImpactProfile.objects.select_related("owner", "package").annotate(n_versions=Count("versions"))
    retired = False
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            qs = qs.filter(Q(profile_key__icontains=d["q"]) | Q(target_sector__icontains=d["q"]))
        if d["family"]:
            qs = qs.filter(family=d["family"])
        if d["territory"]:
            qs = qs.filter(Q(iou_ac_territory=d["territory"]) | Q(iou_ac_territory=""))
        retired = d["retired"]
    if not retired:
        qs = qs.filter(retired_at__isnull=True)
    page = Paginator(qs.order_by("profile_key", "target_sector", "iou_ac_territory"), 50).get_page(request.GET.get("page"))
    return render(
        request,
        "impact_profiles/library.html",
        {"form": form, "page": page, "query": _query(request), "can_add": access.can_submit(request.user, request.organisation)},
    )


@login_required
def profile(request, pk):
    p = get_object_or_404(ImpactProfile.objects.select_related("owner", "package", "retired_by"), pk=pk)
    versions = list(p.versions.select_related("created_by").prefetch_related("zones__shape"))
    current = next((v for v in versions if v.superseded_at is None), None)
    detail = []
    if current is not None:
        year = current.represents_year or 2009
        for z in current.zones.all():
            values = shapes.load(z.shape)
            detail.append(
                {
                    "zone": z.climate_zone,
                    "sum": float(values.sum()),
                    "peak": shapes.peak_hour(values, year),
                    "months": [(m, 100 * x) for m, x in shapes.monthly_shares(values, year)],
                }
            )
    mine = p.family == ImpactProfile.Family.CUSTOM and request.organisation is not None and p.owner_id == request.organisation.pk
    can_change = not p.retired and (access.is_admin(request.user) or (mine and access.can_submit(request.user, request.organisation)))
    return render(
        request,
        "impact_profiles/profile.html",
        {
            "p": p,
            "versions": versions,
            "current": current,
            "detail": detail,
            "months": [m for m, _ in detail[0]["months"]] if detail else [],
            "aliases": p.aliases.filter(ended_at__isnull=True).select_related("package"),
            "can_replace": can_change and p.family == ImpactProfile.Family.CUSTOM,
            "can_retire": can_change,
            "replace_form": forms.ReplaceForm(),
            "retire_form": forms.ReasonForm(),
        },
    )


@login_required
def profile_version_csv(request, pk, number):
    p = get_object_or_404(ImpactProfile, pk=pk)
    v = get_object_or_404(p.versions, number=number)
    rows = []
    for z in v.zones.select_related("shape"):
        rows += [[z.climate_zone, h, repr(float(x))] for h, x in enumerate(shapes.load(z.shape), start=1)]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in p.profile_key)[:80]
    return _csv_response(rows, custom.COLUMNS, f"impact_profile_{safe}_v{v.number}.csv")


@login_required
@require_POST
def profile_replace(request, pk):
    p = get_object_or_404(ImpactProfile, pk=pk)
    back = f"/impact-profiles/{p.pk}/"
    form = forms.ReplaceForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose the new file and give a reason for the replacement.")
        return redirect(back)
    d = form.cleaned_data
    try:
        v = custom.replace_custom(
            request.user,
            request.organisation,
            p,
            data=d["file"].read(),
            file_name=d["file"].name,
            reason=d["reason"],
            represents_year=d["represents_year"],
        )
    except custom.ProfileRejected as e:
        return render(request, "impact_profiles/rejected.html", {"p": p, "findings": e.findings, "back": back}, status=400)
    except AccessDenied as e:
        return _refused(request, e, back)
    messages.success(request, f"Version {v.number} added. Runs and claims that named an earlier version keep it.")
    return redirect(back)


@login_required
@require_POST
def profile_retire(request, pk):
    p = get_object_or_404(ImpactProfile, pk=pk)
    back = f"/impact-profiles/{p.pk}/"
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a reason for retiring the profile.")
        return redirect(back)
    try:
        custom.retire(request.user, request.organisation, p, form.cleaned_data["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    messages.success(request, "Profile retired. Its versions are kept; the next library write leaves it out.")
    return redirect(back)


@login_required
def custom_new(request):
    form = forms.CustomProfileForm(request.POST or None, request.FILES or None)
    if not access.can_submit(request.user, request.organisation):
        raise Http404
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            p = custom.create_custom(
                request.user,
                request.organisation,
                profile_key=d["profile_key"],
                target_sector=d["target_sector"],
                territory=d["territory"],
                description=d["description"],
                represents_year=d["represents_year"],
                data=d["file"].read(),
                file_name=d["file"].name,
            )
        except custom.ProfileRejected as e:
            return render(request, "impact_profiles/custom_new.html", {"form": form, "findings": e.findings}, status=400)
        except AccessDenied as e:
            messages.error(request, str(e))
            return render(request, "impact_profiles/custom_new.html", {"form": form}, status=400)
        messages.success(request, f"{p.profile_key} added as version 1.")
        return redirect(f"/impact-profiles/{p.pk}/")
    return render(request, "impact_profiles/custom_new.html", {"form": form})


@login_required
def custom_template(request):
    response = HttpResponse(custom.template_csv(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="custom_impact_profile_template.csv"'
    return response


# --- deemed load shapes


@login_required
def packages(request):
    form = forms.PackageFilterForm(request.GET or None)
    qs = MeasurePackage.objects.filter(in_last_sync=True).select_related("current").annotate(n_shapes=Count("load_shapes", distinct=True))
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            qs = qs.filter(Q(name__icontains=d["q"]) | Q(measure_version_id__icontains=d["q"]))
        if d["mine"] and request.organisation is not None:
            qs = qs.filter(pa_lead=request.organisation.code)
    page = Paginator(qs.order_by("measure_version_id"), 50).get_page(request.GET.get("page"))
    return render(
        request, "impact_profiles/packages.html", {"form": form, "page": page, "query": _query(request), "last_sync": etrm.last_success()}
    )


def _package_or_404(mvid) -> MeasurePackage:
    return get_object_or_404(MeasurePackage.objects.select_related("current__sync_run"), measure_version_id=mvid)


@login_required
def package(request, mvid):
    pkg = _package_or_404(mvid)
    org = request.organisation
    can_upload = org is not None and pkg.pa_lead == org.code and access.can_submit(request.user, org)
    form = forms.UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        if not form.is_valid():
            messages.error(request, "Choose a file to upload.")
        else:
            try:
                u = loadshapes.create_upload(
                    request.user, org, pkg, form.cleaned_data["file"], accept_partial=form.cleaned_data["accept_partial"]
                )
            except AccessDenied as e:
                return _refused(request, e, f"/impact-profiles/packages/{pkg.measure_version_id}/")
            messages.success(request, "File received. It is checked on your organization's queue; this page shows the result.")
            return redirect(f"/impact-profiles/uploads/{u.pk}/")
    perms = pkg.current.permutations.all() if pkg.current else []
    perm_page = Paginator(perms, 100).get_page(request.GET.get("page")) if pkg.current else None
    sees_uploads = access.sees_organisation(request.user, pkg.pa_lead)
    return render(
        request,
        "impact_profiles/package.html",
        {
            "pkg": pkg,
            "perm_page": perm_page,
            "form": form,
            "can_upload": can_upload,
            "uploads": pkg.uploads.select_related("uploaded_by")[:20] if sees_uploads else None,
            "shapes": LoadShape.objects.filter(package=pkg).prefetch_related("versions").order_by("tech_id", "bldg_type", "bldg_loc"),
            "profiles": pkg.profiles.order_by("profile_key"),
            "mappings": pkg.mappings.filter(ended_at__isnull=True).select_related("target"),
            "mapping_form": forms.MappingForm(package=pkg) if can_upload else None,
        },
    )


@login_required
@require_POST
def mapping_new(request, mvid):
    pkg = _package_or_404(mvid)
    back = f"/impact-profiles/packages/{pkg.measure_version_id}/"
    form = forms.MappingForm(request.POST, package=pkg)
    if not form.is_valid():
        messages.error(request, "Check the mapping form: " + "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
        return redirect(back)
    d = form.cleaned_data
    try:
        custom.create_mapping(
            request.user,
            request.organisation,
            pkg,
            alias_key=d["alias_key"],
            target_sector=d["target_sector"],
            target=d["target"],
            reason=d["reason"],
        )
    except AccessDenied as e:
        return _refused(request, e, back)
    messages.success(request, f"{d['alias_key']} now uses {d['target'].profile_key}'s shapes from the next library write.")
    return redirect(back)


@login_required
@require_POST
def mapping_end(request, pk):
    m = get_object_or_404(ProfileMapping.objects.select_related("package"), pk=pk)
    back = f"/impact-profiles/packages/{m.package.measure_version_id}/"
    try:
        custom.end_mapping(request.user, request.organisation, m, request.POST.get("reason", ""))
    except AccessDenied as e:
        return _refused(request, e, back)
    messages.success(request, "Mapping ended.")
    return redirect(back)


@login_required
def load_shape_template(request):
    response = HttpResponse(loadshapes.template_csv(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="load_shape_template.csv"'
    return response


@login_required
def load_shape(request, pk):
    ls = get_object_or_404(LoadShape.objects.select_related("package"), pk=pk)
    versions = list(ls.versions.select_related("upload__uploaded_by", "shape"))
    current = next((v for v in versions if v.superseded_at is None), None)
    months, peak = [], None
    if current is not None:
        values = shapes.load(current.shape)
        months = [(m, 100 * x) for m, x in shapes.monthly_shares(values, current.source_year)]
        peak = shapes.peak_hour(values, current.source_year)
    return render(
        request, "impact_profiles/load_shape.html", {"ls": ls, "versions": versions, "current": current, "months": months, "peak": peak}
    )


@login_required
def uploads(request):
    qs = LoadShapeUpload.objects.select_related("package", "organisation", "uploaded_by")
    if not access.is_admin(request.user):
        qs = qs.filter(organisation__in=request.user_organisations)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "impact_profiles/uploads.html", {"page": page})


def _upload_or_404(request, pk) -> LoadShapeUpload:
    u = get_object_or_404(LoadShapeUpload.objects.select_related("package", "package_version", "organisation", "uploaded_by"), pk=pk)
    if not access.sees_organisation(request.user, u.organisation.code):
        raise Http404
    return u


@login_required
def upload(request, pk):
    u = _upload_or_404(request, pk)
    job = u.jobs.order_by("-created_at").first()
    findings = u.findings.all()
    by_stage = {s: [f for f in findings if f.stage == s] for s in loadshapes.STAGES}
    stages = [(code, name, u.stages.get(code, "not run"), len(by_stage[code])) for code, name in loadshapes.STAGES.items()]
    org = request.organisation
    can_generate = (
        u.status in (LoadShapeUpload.Status.ACCEPTED, LoadShapeUpload.Status.GENERATED)
        and org is not None
        and org.code == u.package.pa_lead
        and access.can_submit(request.user, org)
    )
    return render(
        request,
        "impact_profiles/upload.html",
        {
            "u": u,
            "job": job,
            "position": jobs.queue_position(job) if job and job.status == ProcessingJob.Status.QUEUED else None,
            "stages": stages,
            "findings": findings[:500],
            "n_findings": findings.count(),
            "can_generate": can_generate,
            "generating": u.jobs.filter(kind=ProcessingJob.Kind.GENERATE, status__in=["queued", "running"]).exists(),
            "summary": u.generation_summary,
            "kept": u.load_shape_versions.select_related("load_shape").order_by("load_shape__tech_id", "load_shape__bldg_loc")[:200],
        },
    )


@login_required
@require_POST
def upload_generate(request, pk):
    u = _upload_or_404(request, pk)
    back = f"/impact-profiles/uploads/{u.pk}/"
    try:
        access.require_package_lead(request.user, request.organisation, u.package)
        if u.status not in (LoadShapeUpload.Status.ACCEPTED, LoadShapeUpload.Status.GENERATED):
            raise AccessDenied("Only an accepted upload's package can be generated.")
    except AccessDenied as e:
        return _refused(request, e, back)
    jobs.enqueue(ProcessingJob.Kind.GENERATE, request.organisation.code, actor=request.user, upload=u, params={"package": u.package_id})
    messages.success(request, "Generation queued on your organization's queue. This page shows the result when it is done.")
    return redirect(back)


@login_required
def upload_findings_csv(request, pk):
    u = _upload_or_404(request, pk)
    rows = [[f.stage, f.rule_id, f.severity, f.row or "", f.record_id, f.field, f.value, f.message, f.fix] for f in u.findings.all()]
    return _csv_response(
        rows, ["stage", "rule_id", "severity", "row", "series", "field", "value", "message", "fix"], f"load_shape_findings_{u.pk}.csv"
    )


# --- eTRM sync history and library exports


@login_required
def sync_history(request):
    if request.method == "POST":
        try:
            access.require_admin(request.user, "start an eTRM sync")
        except AccessDenied as e:
            return _refused(request, e, "impact_profiles:sync_history")
        jobs.enqueue(ProcessingJob.Kind.SYNC, jobs.SYSTEM, actor=request.user, params={"trigger": "manual"})
        messages.success(request, "eTRM sync queued.")
        return redirect("impact_profiles:sync_history")
    last = etrm.last_success()
    latest = EtrmSyncRun.objects.first()
    form = forms.SyncFilterForm(request.GET or None)
    excluded = ExcludedPackage.objects.filter(sync_run=last) if last else ExcludedPackage.objects.none()
    if form.is_valid() and form.cleaned_data["q"]:
        q = form.cleaned_data["q"]
        excluded = excluded.filter(Q(measure_version_id__icontains=q) | Q(name__icontains=q) | Q(reason__icontains=q))
    return render(
        request,
        "impact_profiles/sync_history.html",
        {
            "last": last,
            "latest": latest,
            "runs": EtrmSyncRun.objects.all()[:20],
            "form": form,
            "page": Paginator(excluded, 50).get_page(request.GET.get("page")),
            "query": _query(request),
            "is_admin": access.is_admin(request.user),
            "retry_minutes": " and ".join(f"{m:g}" for m in settings.ETRM_SYNC_RETRY_MINUTES),
        },
    )


@login_required
def exports(request):
    if request.method == "POST":
        try:
            access.require_admin(request.user, "write the profile library")
        except AccessDenied as e:
            return _refused(request, e, "impact_profiles:exports")
        jobs.enqueue(ProcessingJob.Kind.EXPORT, jobs.SYSTEM, actor=request.user)
        messages.success(request, "Library write queued.")
        return redirect("impact_profiles:exports")
    return render(
        request,
        "impact_profiles/exports.html",
        {"exports": LibraryExport.objects.select_related("created_by", "pack")[:50], "is_admin": access.is_admin(request.user)},
    )


@login_required
def export_file(request, number, name):
    e = get_object_or_404(LibraryExport, number=number)
    stored = {"catalog.parquet": e.catalog_name, "hourly.parquet": e.hourly_name}.get(name)
    if stored is None:
        raise Http404
    return FileResponse(default_storage.open(stored, "rb"), as_attachment=True, filename=f"export_{e.number}_{name}")
