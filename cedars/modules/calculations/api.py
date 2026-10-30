"""The calculation job API (decision J: calculations first; BR-06.12): start a run from a job zip, follow it, stop it and fetch its
files, as the New run and run pages do. A call acts for one organisation, the API token's (or the session's active one); a run made
with `personal` set, or with a token that has no organisation, is the caller's own and seen only by them. The service checks the
request exactly as it checks New run, so a run started here is the same record, job spec and audit trail."""

from pathlib import PurePosixPath

from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponseRedirect
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from cedars.core import api, storage
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry.models import AllocationVersion

from . import jobspec, output_templates, service
from .models import EngineRelease, Run
from .views import status_view

MODULE = "calculations"
PER_PAGE = 50


def _personal_allowed(request) -> bool:
    """Whether the call may use the caller's own runs (a token must have the calculations scope)."""
    token = api.token_of(request)
    return token is None or MODULE in token.scopes


def _org_reads(request) -> bool:
    return api.organisation(request) is not None and (
        api.allowed(request, "calculations.read") or api.allowed(request, "calculations.submit")
    )


def _visible(request):
    """The runs this call may read: the acting organisation's (with calculations.read or .submit) and the caller's own."""
    q = Q(pk__in=[])
    if _org_reads(request):
        q |= Q(organisation=api.organisation(request))
    if _personal_allowed(request):
        q |= Q(organisation__isnull=True, owner=request.user)
    return Run.objects.filter(q).select_related("organisation", "owner", "allocation_version")


def _run(request, pk) -> Run:
    try:
        return _visible(request).get(pk=pk)
    except Run.DoesNotExist:
        raise NotFound("No such run.") from None


def _files(request, run: Run) -> list[dict]:
    if run.status == Run.Status.QUEUED:
        return []
    return [
        {**f, "url": request.build_absolute_uri(f"/api/v1/calculations/runs/{run.pk}/files/{f['name']}")} for f in service.output_files(run)
    ]


def run_json(request, run: Run, detail: bool = True) -> dict:
    view = status_view(run)
    out = {
        "id": str(run.pk),
        "name": run.name,
        "status": run.status,
        "status_text": view.get("text", ""),
        "organisation": run.organisation.code if run.organisation else None,
        "owner": run.owner.email if run.owner else None,
        "engine": {"release": run.engine_release, "calculation": run.calculation},
        "acc": run.acc_version,
        "measures": run.measure_count,
        "lane": run.lane,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "url": request.build_absolute_uri(f"/api/v1/calculations/runs/{run.pk}"),
    }
    if not detail:
        return out
    receipt = run.receipt or {}
    out.update(
        {
            "settings": run.settings,
            "output_format": run.output_format,
            "allocation_version": str(run.allocation_version) if run.allocation_version else None,
            "input_hash": run.input_hash,
            "inputs": {k: {"sha256": v["sha256"], "bytes": v.get("bytes")} for k, v in run.inputs.items()},
            "stage": view.get("stage") or None,
            "percent": view.get("percent"),
            "queue_position": view.get("queue_position"),
            "estimate": service.eta(run) or None,
            "error": run.error or None,
            "totals": receipt.get("totals") or {},
            "notes": receipt.get("notes") or [],
            "receipt": receipt or None,
            "files": _files(request, run),
            "page": request.build_absolute_uri(f"/calculations/runs/{run.pk}/"),
        }
    )
    return out


# --- schema pieces (documentation only: the views build their answers with run_json)

SUMMARY_FIELDS = {
    "id": serializers.UUIDField(),
    "name": serializers.CharField(),
    "status": serializers.ChoiceField(choices=Run.Status.choices),
    "status_text": serializers.CharField(),
    "organisation": serializers.CharField(allow_null=True),
    "owner": serializers.EmailField(allow_null=True),
    "engine": serializers.DictField(),
    "acc": serializers.CharField(),
    "measures": serializers.IntegerField(),
    "lane": serializers.CharField(),
    "created_at": serializers.DateTimeField(),
    "started_at": serializers.DateTimeField(allow_null=True),
    "finished_at": serializers.DateTimeField(allow_null=True),
    "url": serializers.URLField(),
}
RunSummary = inline_serializer("RunSummary", SUMMARY_FIELDS)
RunDetail = inline_serializer(
    "RunDetail",
    {
        **SUMMARY_FIELDS,
        "settings": serializers.DictField(),
        "output_format": serializers.CharField(),
        "allocation_version": serializers.CharField(allow_null=True),
        "input_hash": serializers.CharField(),
        "inputs": serializers.DictField(),
        "stage": serializers.CharField(allow_null=True),
        "percent": serializers.IntegerField(allow_null=True),
        "queue_position": serializers.IntegerField(allow_null=True),
        "estimate": serializers.DictField(allow_null=True, help_text="starts_in and done_in, seconds (estimates)"),
        "error": serializers.CharField(allow_null=True),
        "totals": serializers.DictField(),
        "notes": serializers.ListField(child=serializers.CharField()),
        "receipt": serializers.DictField(allow_null=True, help_text="The worker's receipt.json once the run has ended"),
        "files": serializers.ListField(child=serializers.DictField()),
        "page": serializers.URLField(),
    },
)


class NewRun(serializers.Serializer):
    """What New run takes, as form fields. Settings left out take New run's defaults."""

    job = serializers.FileField(help_text="Measure.csv and, optionally, ProgramCost.csv, zipped together")
    name = serializers.CharField(max_length=200)
    personal = serializers.BooleanField(default=False, help_text="The caller's own run, not the organisation's")
    release = serializers.CharField(required=False, help_text="A ReCET release from /engines; the default release when left out")
    calculation = serializers.CharField(default=service.RECET, help_text="recet, or a CET replica such as cet-25.1 (D13)")
    acc_version = serializers.CharField(help_text="The ACC by year, e.g. 2024")
    output_format = serializers.ChoiceField(choices=list(service.OUTPUT_FORMATS), default="csv.zip")
    acc_purpose = serializers.CharField(required=False, allow_blank=True)
    allocate_statewide = serializers.BooleanField(default=False)
    allocation_version = serializers.IntegerField(required=False, help_text="A confirmed allocation version's id")
    filing_validation = serializers.ChoiceField(choices=list(service.FILING_CHECKS), required=False, allow_blank=True)
    filing_year = serializers.IntegerField(required=False, min_value=2000, max_value=2100)
    include_societal = serializers.BooleanField(default=True)
    emissions = serializers.BooleanField(default=True)
    preflight_only = serializers.BooleanField(default=False, help_text="Check the inputs only")
    cet_outputs = serializers.FileField(
        required=False, help_text="A parallel run: the outputs CSV of the same job run in CET, compared measure by measure (ReCET only)"
    )
    output_template = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="An output template's id from /templates, or 'default' for the caller's default; every column when left out",
    )
    output_columns = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Measure-level columns to write, in order, comma- or line-separated (not with a template)",
    )


def _release(name: str | None) -> EngineRelease:
    if not name:
        r = service.default_release()
        if r is None:
            raise AccessDenied("No engine release is set up yet.")
        return r
    r = EngineRelease.objects.filter(release=name).first()
    if r is None:
        raise AccessDenied(f"No ReCET release {name}.")
    return r


def _template_settings(data: dict, user) -> dict:
    """The output template settings a call asks for: a template the caller sees, their default, or a column list (checked against the
    field catalog, under canonical names). A call that names none writes every column, whatever the caller's default on New run."""
    ref, text = (data.get("output_template") or "").strip(), (data.get("output_columns") or "").strip()
    if ref and text:
        raise AccessDenied("Give an output template or output columns, not both.")
    if ref == "default":
        return output_templates.settings_for(output_templates.default_for(user))
    if ref:
        template = output_templates.visible(user).filter(pk=int(ref)).first() if ref.isdigit() else None
        if template is None:
            raise AccessDenied(f"No output template {ref}.")
        return output_templates.settings_for(template)
    if text:
        cat = output_templates.catalog()
        if not cat.names:
            raise AccessDenied("Output columns need the published field catalog, and none is published yet.")
        columns, bad = output_templates.check(output_templates.parse(text), cat)
        if bad:
            raise AccessDenied(f"Not measure-level columns: {', '.join(bad)}.")
        if not columns:
            raise AccessDenied("Output columns: name at least one column.")
        return {"output_template": columns, "output_template_name": "Columns given with the call"}
    return {}


def _request(data: dict, user) -> service.RunRequest:
    allocation = None
    if data.get("allocation_version") is not None:
        allocation = AllocationVersion.objects.filter(pk=data["allocation_version"]).first()
        if allocation is None:
            raise AccessDenied(f"No allocation version {data['allocation_version']}.")
    settings = {
        "acc_purpose": data.get("acc_purpose") or None,
        "allocate_statewide": data.get("allocate_statewide") or None,
        "filing_validation": data.get("filing_validation") or None,
        "filing_year": data.get("filing_year"),
        "include_societal": data["include_societal"],
        "emissions": data["emissions"],
        "preflight_only": data.get("preflight_only") or None,
        **_template_settings(data, user),
    }
    return service.RunRequest(
        name=data["name"],
        release=_release(data.get("release")),
        calculation=data["calculation"],
        acc_version=data["acc_version"],
        output_format=data["output_format"],
        settings=settings,
        allocation_version=allocation,
        cet_outputs=data.get("cet_outputs"),
    )


# --- views


@extend_schema(
    summary="Engine releases and what each offers",
    responses=inline_serializer(
        "EngineReleases",
        {
            "releases": serializers.ListField(child=serializers.DictField()),
            "default": serializers.CharField(allow_null=True),
            "settings": serializers.DictField(help_text="The run settings a job may set, with their labels"),
        },
    ),
)
@api_view(["GET"])
def engines(request):
    if not (_personal_allowed(request) or _org_reads(request)):
        raise PermissionDenied("This call has no calculations permission.")
    default = service.default_release()
    return Response(
        {
            "releases": [
                {
                    "release": r.release,
                    "default": r.default,
                    "calculations": [
                        {"id": c, "label": service.calculation_label(c), "accs": service.accs_for(r, c)} for c in r.calculations
                    ],
                }
                for r in service.releases()
            ],
            "default": default.release if default else None,
            "settings": jobspec.SETTINGS,
        }
    )


@extend_schema(
    methods=["GET"],
    operation_id="calculations_runs_list",
    summary="Runs this call can see",
    parameters=[
        OpenApiParameter("status", str, enum=[s for s, _ in Run.Status.choices]),
        OpenApiParameter("page", int, description=f"{PER_PAGE} runs a page, newest first"),
    ],
    responses=inline_serializer(
        "RunList", {"count": serializers.IntegerField(), "next": serializers.URLField(allow_null=True), "runs": serializers.ListField()}
    ),
)
@extend_schema(
    methods=["POST"],
    operation_id="calculations_runs_create",
    summary="Start a run",
    description="Upload a job zip as multipart form data. The run is queued; follow it at the returned url.",
    request={"multipart/form-data": NewRun},
    responses={201: RunDetail},
)
@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser])
def runs(request):
    if request.method == "POST":
        return _start(request)
    qs = _visible(request)
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"])
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    count = qs.count()
    rows = qs[(page - 1) * PER_PAGE : page * PER_PAGE]
    nxt = None
    if page * PER_PAGE < count:
        q = request.query_params.copy()
        q["page"] = page + 1
        nxt = request.build_absolute_uri(f"{request.path}?{q.urlencode()}")
    return Response({"count": count, "next": nxt, "runs": [run_json(request, r, detail=False) for r in rows]})


def _start(request):
    form = NewRun(data=request.data)
    form.is_valid(raise_exception=True)
    data = form.validated_data
    org = None if data["personal"] else api.organisation(request)
    if org is None:
        if not _personal_allowed(request):
            raise PermissionDenied("This token cannot start your own runs; it needs the calculations module.")
    elif not api.allowed(request, "calculations.submit"):
        raise PermissionDenied(f"This call needs calculations.submit in {org.code}.")
    try:
        run = service.create_run_from_upload(request.user, org, _request(data, request.user), data["job"])
    except AccessDenied as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    body = run_json(request, run)
    return Response(body, status=status.HTTP_201_CREATED, headers={"Location": body["url"]})


@extend_schema(
    operation_id="calculations_run_retrieve", summary="One run: status, progress, estimates, receipt and files", responses=RunDetail
)
@api_view(["GET"])
def run(request, pk):
    return Response(run_json(request, _run(request, pk)))


@extend_schema(
    summary="Stop a run",
    request=inline_serializer("StopRun", {"reason": serializers.CharField(required=False)}),
    responses=RunDetail,
)
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def stop(request, pk):
    r = _run(request, pk)
    if not service.can_cancel(request.user, r) or (r.organisation is not None and not api.allowed(request, "calculations.submit")):
        raise PermissionDenied("You cannot stop this run.")
    try:
        r = service.cancel_run(request.user, r, request.data.get("reason", ""))
    except AccessDenied as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(run_json(request, r))


@extend_schema(summary="Download one of a run's output files", responses={(200, "application/octet-stream"): bytes, 302: None})
@api_view(["GET"])
def run_file(request, pk, name):
    r = _run(request, pk)
    key = service.output_key(r, name)
    if key is None:
        raise Http404
    filename = PurePosixPath(name).name
    store = storage.store("results")
    url = store.download_url(key, filename)
    if url:
        return HttpResponseRedirect(url)
    return FileResponse(store.open(key), as_attachment=True, filename=filename)


@extend_schema(
    summary="The output templates this call may name",
    description="Templates offered to everyone first, then the caller's own. Name one by id in a new run's output_template.",
    responses=inline_serializer(
        "OutputTemplates",
        {
            "templates": serializers.ListField(child=serializers.DictField()),
            "default": serializers.IntegerField(allow_null=True, help_text="The caller's default (output_template=default)"),
        },
    ),
)
@api_view(["GET"])
def templates(request):
    if not (_personal_allowed(request) or _org_reads(request)):
        raise PermissionDenied("This call has no calculations permission.")
    default = output_templates.default_for(request.user)
    rows = output_templates.visible(request.user)
    return Response(
        {
            "templates": [{"id": t.pk, "name": t.name, "shared": t.owner_id is None, "columns": list(t.columns)} for t in rows],
            "default": default.pk if default else None,
        }
    )
