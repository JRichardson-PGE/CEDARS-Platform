"""The claims submissions API (decision J: the submissions API with claims, read-only endpoints first, writes staged after them).

Reads: the claim years and their confirmations, a PA's versions with their check results, findings, results by program and published
files, the change letters, and NMEC chains. A call acts for one organization, the API token's (or the session's active one), and reads
what the pages show it: its own organization's versions with claims.read, .submit or .confirm there, every organization's from the CPUC
with claims.read or .approve, and every published version (public to signed-in users, BR-05.9).

Writes, the period page's steps for the organization the call acts for: upload a confirmation's claims (checked at once), check the
latest version again, price it, confirm it. Each runs the same service function as the page, so the same rules hold (quarters in
order, a priced version on exactly its records, a stale version checked again); check, price and confirm name the version and act
only on the confirmation's latest one, so a client confirms exactly what it reviewed. Upload, check and price need claims.submit,
confirm claims.confirm. A refusal is 409 with the service's message; an unreadable file is 400. A token reaches claims only with the
claims scope, and never the PII vault (PII files are uploaded on the period page)."""

import mimetypes

from django.http import FileResponse, Http404, HttpResponseRedirect
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from cedars.core import api, storage, uploads
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, Submission, SubmissionVersion

from . import service, spec
from .models import FILED_TOTALS, PROGRAM_COST_FIELDS, ChangeLetter, ClaimYear
from .upload import UploadError
from .upload import combine as combine_upload

MODULE = "claims"
PER_PAGE = 500


# --- who may read what


def _scoped(request) -> bool:
    """Whether the call may reach claims at all: a session, or a token with the claims scope."""
    token = api.token_of(request)
    return token is None or MODULE in token.scopes


def _reads(request, organisation) -> bool:
    """Whether the call may read an organization's unpublished versions and findings."""
    acting = api.organisation(request)
    if acting is None or not _scoped(request):
        return False
    if acting == organisation and any(api.allowed(request, f"{MODULE}.{a}") for a in ("read", "submit", "confirm")):
        return True
    if acting == access.cpuc() and any(api.allowed(request, f"{MODULE}.{a}") for a in ("read", "approve")):
        return True
    return api.token_of(request) is None and access.is_system_admin(request.user)


def _require_scope(request) -> None:
    if not _scoped(request):
        raise PermissionDenied("This token has no claims scope.")


def _version(request, pk) -> SubmissionVersion:
    _require_scope(request)
    v = (
        SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation", "confirmed_by")
        .filter(pk=pk, submission__deadline__module=service.MODULE, submission__deadline__kind__in=service.KINDS)
        .first()
    )
    if v is None or not (v.status == SubmissionVersion.Status.PUBLISHED or _reads(request, v.submission.organisation)):
        raise NotFound("No such claims version.")
    return v


def _deadline(pk) -> Deadline:
    d = Deadline.objects.select_related("period").filter(pk=pk, module=service.MODULE, kind__in=service.KINDS).first()
    if d is None or d.period is None:
        raise NotFound("No such claims confirmation.")
    return d


def _num(x):
    return None if x is None else float(x)


# --- answers


def deadline_json(request, d: Deadline) -> dict:
    return {
        "id": d.pk,
        "kind": d.kind,
        "label": service.label(d),
        "number": service.number(d),
        "last_quarter": service.last_quarter(d),
        "opens_at": d.opens_at,
        "due_at": d.due_at,
        "past_due": submissions.past_due(d),
        "url": request.build_absolute_uri(f"/api/v1/claims/periods/{d.pk}"),
    }


def version_json(request, v: SubmissionVersion, detail: bool = True) -> dict:
    d = v.submission.deadline
    out = {
        "id": v.pk,
        "organisation": v.submission.organisation.code,
        "year": d.period.year,
        "period": d.kind,
        "deadline": d.pk,
        "number": v.number,
        "status": v.status,
        "late": v.late,
        "reason": v.reason,
        "confirmed_at": v.confirmed_at,
        "confirmed_by": v.confirmed_by.email if v.confirmed_by else None,
        "url": request.build_absolute_uri(f"/api/v1/claims/versions/{v.pk}"),
        "page": request.build_absolute_uri(f"/claims/versions/{v.pk}/"),
    }
    if not detail:
        return out
    service.ensure_checked(v)
    v.refresh_from_db()
    s = v.findings or {}
    t = service.table(v)
    letter = ChangeLetter.objects.filter(version=v).first()
    out.update(
        {
            "check": {
                k: s.get(k)
                for k in (
                    "records",
                    "rejected",
                    "warnings",
                    "notes",
                    "rule_set",
                    "by_rule",
                    "tables",
                    "compiled",
                    "replaced",
                    "added",
                    "withdrawn",
                    "withdrawn_total",
                    "base_label",
                    "etrm",
                    "compiled_sha256",
                    "job_sha256",
                )
            },
            "priced": t.priced,
            "stale": service.stale(v),
            "pricing_run": _run_json(request, service.pricing_run(v)),
            "calculation": {k: (v.calculation or {}).get(k) for k in ("run", "acc", "release")} if v.calculation else None,
            "totals": {
                "claims": t.total.claims,
                "expenditures": _num(t.total.expenditures),
                **{key: _num(t.total.value(key)) for key, _, _, _ in service.METRICS if key != "expenditures"},
            },
            "withdrawals": service.withdrawals(v),
            "change_letter": request.build_absolute_uri(f"/api/v1/claims/changes/{letter.pk}") if letter else None,
            "files": [
                {"label": label, "url": request.build_absolute_uri(f"/api/v1/claims/versions/{v.pk}/files/{i}")}
                for i, (label, _, _) in enumerate(service.published_files(v))
            ],
            "findings": request.build_absolute_uri(f"/api/v1/claims/versions/{v.pk}/findings")
            if _reads(request, v.submission.organisation)
            else None,
            "results": request.build_absolute_uri(f"/api/v1/claims/versions/{v.pk}/results"),
        }
    )
    return out


def _run_json(request, run: str | None) -> dict | None:
    if not run:
        return None
    return {
        "id": run,
        "url": request.build_absolute_uri(f"/api/v1/calculations/runs/{run}"),
        "page": request.build_absolute_uri(f"/calculations/runs/{run}/"),
    }


def _row(r) -> dict:
    return {
        "PrgID": r.prg_id,
        "ProgramName": r.name,
        "PA": r.pa,
        "Claims": r.claims,
        **{k: _num(r.by_field.get(k, 0)) for k, _ in PROGRAM_COST_FIELDS},
        "RebatesandIncents": _num(r.rebates),
        "Expenditures": _num(r.expenditures),
        "OnBillFinancing": _num(r.on_bill_financing),
        **{k: _num(r.filed.get(k, 0)) for k in FILED_TOTALS},
        **{
            key if kind != "ratio" else f"{key}Ratio": _num(r.value(key))
            for key, _, kind, _ in service.METRICS
            if kind in ("money", "ratio", "number") and key != "expenditures"
        },
    }


def letter_json(request, c: ChangeLetter, detail: bool = False) -> dict:
    v = c.version
    out = {
        "id": c.pk,
        "organisation": v.submission.organisation.code,
        "year": v.submission.deadline.period.year,
        "period": v.submission.deadline.kind,
        "version": v.number,
        "replaces": c.previous.number if c.previous else None,
        "reason": c.reason,
        "published_at": c.published_at,
        "url": request.build_absolute_uri(f"/api/v1/claims/changes/{c.pk}"),
        "page": request.build_absolute_uri(f"/claims/changes/{c.pk}/"),
    }
    if detail:
        out["changes"] = c.changes
    return out


Page = inline_serializer("ClaimsPage", {"count": serializers.IntegerField(), "next": serializers.URLField(allow_null=True)})


def _page(request, items: list, key: str, per_page: int = PER_PAGE) -> Response:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    nxt = None
    if page * per_page < len(items):
        q = request.query_params.copy()
        q["page"] = page + 1
        nxt = request.build_absolute_uri(f"{request.path}?{q.urlencode()}")
    return Response({"count": len(items), "next": nxt, key: items[(page - 1) * per_page : page * per_page]})


# --- views


@extend_schema(
    summary="Claim years and their confirmations",
    description="Each claim year with its ACC, its allocation version and its five confirmations (Q1 to Q4 and the annual claim).",
    responses=inline_serializer("ClaimYears", {"years": serializers.ListField(child=serializers.DictField())}),
)
@api_view(["GET"])
def years(request):
    _require_scope(request)
    out = []
    for cy in ClaimYear.objects.select_related("allocation_version").order_by("-year"):
        out.append(
            {
                "year": cy.year,
                "acc": cy.acc_version,
                "allocation_version": str(cy.allocation_version) if cy.allocation_version else None,
                "confirmations": [deadline_json(request, d) for d in service.periods(cy.year)],
            }
        )
    return Response({"years": out})


@extend_schema(
    summary="One confirmation for one organization: its versions",
    description="The acting organization's versions, or ?org=CODE's for a CPUC reader. Others see only published versions.",
    parameters=[OpenApiParameter("org", str, description="An organization code (CPUC readers; others see its published versions)")],
    responses=inline_serializer(
        "ClaimsPeriod",
        {
            "confirmation": serializers.DictField(),
            "organisation": serializers.CharField(allow_null=True),
            "standing": serializers.IntegerField(allow_null=True, help_text="The confirmed version that stands, if any"),
            "versions": serializers.ListField(child=serializers.DictField()),
        },
    ),
)
@api_view(["GET"])
def period(request, pk):
    _require_scope(request)
    d = _deadline(pk)
    code = request.query_params.get("org")
    org = access.by_code_or_404(code) if code else api.organisation(request)
    versions = []
    standing = None
    if org is not None:
        sub = Submission.objects.filter(deadline=d, organisation=org).first()
        if sub is not None:
            full = _reads(request, org)
            versions = [
                version_json(request, v, detail=False)
                for v in sub.versions.select_related("submission__deadline__period", "submission__organisation", "confirmed_by").order_by(
                    "-number"
                )
                if full or v.status == SubmissionVersion.Status.PUBLISHED
            ]
        s = service.standing(org, d)
        standing = s.pk if s is not None else None
    return Response(
        {"confirmation": deadline_json(request, d), "organisation": org.code if org else None, "standing": standing, "versions": versions}
    )


@extend_schema(summary="One version: its check, totals, withdrawals and files", responses=inline_serializer("ClaimsVersion", {}))
@api_view(["GET"])
def version(request, pk):
    return Response(version_json(request, _version(request, pk)))


@extend_schema(
    summary="A version's findings",
    description=f"Every finding of the module's checks and the claims rule set, {PER_PAGE} a page, in file order. The organization "
    "and CPUC readers only.",
    parameters=[OpenApiParameter("page", int)],
    responses=inline_serializer(
        "ClaimsFindings",
        {
            "count": serializers.IntegerField(),
            "next": serializers.URLField(allow_null=True),
            "findings": serializers.ListField(child=serializers.DictField()),
        },
    ),
)
@api_view(["GET"])
def findings(request, pk):
    v = _version(request, pk)
    if not _reads(request, v.submission.organisation):
        raise NotFound("No such claims version.")
    return _page(request, service.findings(v), "findings")


@extend_schema(
    summary="A version's year to date by program",
    description="What was filed (costs, incentives, the claims' own totals) and what it priced to, by program, with the total last; "
    "?pa=CODE for one credited PA's share. The same rows as the version page's CSV.",
    parameters=[OpenApiParameter("pa", str)],
    responses=inline_serializer(
        "ClaimsResults",
        {
            "priced": serializers.BooleanField(),
            "programs": serializers.ListField(child=serializers.DictField()),
            "total": serializers.DictField(),
        },
    ),
)
@api_view(["GET"])
def results(request, pk):
    v = _version(request, pk)
    t = service.table(v, pa=request.query_params.get("pa", ""))
    return Response({"priced": t.priced, "programs": [_row(r) for r in t.rows], "total": _row(t.total)})


@extend_schema(summary="A file a version publishes (or, for its organization, keeps)", responses={(200, "application/octet-stream"): bytes})
@api_view(["GET"])
def file(request, pk, which):
    v = _version(request, pk)
    files = service.published_files(v)
    if which >= len(files):
        raise Http404
    _, area, key = files[which]
    store = storage.store(area)
    if not store.exists(key):
        raise Http404
    filename = f"v{v.number}_{key.rsplit('/', 1)[-1]}"
    url = store.download_url(key, filename)
    if url:
        return HttpResponseRedirect(url)
    return FileResponse(
        store.open(key), as_attachment=True, filename=filename, content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )


@extend_schema(
    summary="Change letters",
    operation_id="claims_changes_list",
    description="Every published correction, newest first (BR-09.3); ?org=CODE and ?year= narrow them.",
    parameters=[OpenApiParameter("org", str), OpenApiParameter("year", int), OpenApiParameter("page", int)],
    responses=inline_serializer(
        "ChangeLetters",
        {
            "count": serializers.IntegerField(),
            "next": serializers.URLField(allow_null=True),
            "letters": serializers.ListField(child=serializers.DictField()),
        },
    ),
)
@api_view(["GET"])
def changes(request):
    _require_scope(request)
    code, year = request.query_params.get("org"), request.query_params.get("year")
    org = access.by_code_or_404(code) if code else None
    letters = service.change_letters(organisation=org, year=int(year) if year and year.isdigit() else None)
    return _page(request, [letter_json(request, c) for c in letters], "letters", 50)


@extend_schema(summary="One change letter: what the correction changed", responses=inline_serializer("ChangeLetter", {}))
@api_view(["GET"])
def change(request, pk):
    _require_scope(request)
    c = (
        ChangeLetter.objects.select_related("version__submission__deadline__period", "version__submission__organisation", "previous")
        .filter(pk=pk)
        .first()
    )
    if c is None:
        raise NotFound("No such change letter.")
    return Response(letter_json(request, c, detail=True))


@extend_schema(
    summary="A confirmed claim's NMEC chain",
    description="From the original claim down through every true-up, in filing order, with each true-up's filed totals against its "
    "parent's (BR-05.3). Confirmed claims only.",
    responses=inline_serializer(
        "ClaimChain", {"claim": serializers.CharField(), "chain": serializers.ListField(child=serializers.DictField())}
    ),
)
@api_view(["GET"])
def chain(request, claim_id):
    _require_scope(request)
    links = service.chain(claim_id)
    if not links:
        raise NotFound("No confirmed claim with that ClaimID.")
    return Response(
        {
            "claim": claim_id,
            "chain": [
                {
                    "claim": ln.record.claim_id,
                    "depth": ln.depth,
                    "parent": ln.record.parent_claim_id or None,
                    "true_up": ln.record.true_up,
                    "program": ln.record.prg_id,
                    "project": ln.record.project_id or None,
                    "withdrawn": ln.record.withdrawn,
                    "version": request.build_absolute_uri(f"/api/v1/claims/versions/{ln.record.version_id}"),
                    "filed": ln.record.filed,
                    "change_from_parent": ln.change or None,
                }
                for ln in links
            ],
        }
    )


# --- writes: the period page's steps, for the organization the call acts for


class _Conflict(Exception):
    """A step the version's state does not allow (409)."""


def _acting(request, organisation, permission: str) -> None:
    """The call acts for the organization and may use the permission there (a token: the user holds it now and has the claims scope)."""
    _require_scope(request)
    acting = api.organisation(request)
    if acting is None:
        raise PermissionDenied("The call acts for no organization.")
    if acting != organisation or not api.allowed(request, f"{MODULE}.{permission}"):
        raise PermissionDenied(f"This call needs {MODULE}.{permission} in {organisation.code}.")


def _latest_version(request, pk, permission: str) -> SubmissionVersion:
    """The version named, which must be its confirmation's latest: a step never acts on a version that a later one has replaced."""
    v = _version(request, pk)
    _acting(request, v.submission.organisation, permission)
    latest = submissions.latest(v.submission)
    if latest is not None and latest.pk != v.pk:
        raise _Conflict(f"Version {v.number} is not this confirmation's latest; version {latest.number} is.")
    return v


def _refused(e: Exception) -> Response:
    code = status.HTTP_400_BAD_REQUEST if isinstance(e, UploadError) else status.HTTP_409_CONFLICT
    return Response({"detail": str(e)}, status=code)


def _done(request, v: SubmissionVersion, code=status.HTTP_200_OK) -> Response:
    v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation", "confirmed_by").get(pk=v.pk)
    body = version_json(request, v)
    return Response(body, status=code, headers={"Location": body["url"]} if code == status.HTTP_201_CREATED else None)


Refusal = inline_serializer("ClaimsRefusal", {"detail": serializers.CharField()})
Version = inline_serializer("ClaimsVersionAfterStep", {})


class UploadClaims(serializers.Serializer):
    file = serializers.ListField(
        child=serializers.FileField(),
        min_length=1,
        max_length=uploads.MAX_FILES,
        help_text=f"A .zip of the claims tables as CSV files named after them ({', '.join(t + '.csv' for t in spec.TABLES)}; comma- or "
        "pipe-delimited), or a workbook (.xlsx) with a sheet per table: the records new or changed since the last confirmed quarter. "
        "No PII tables. Repeat the field to send several files (.zip, .xlsx, or one table's .csv each; a table in parts named after "
        "it, Measure_1.csv, Measure_2.csv, with the same columns): they are joined into one upload.",
    )


@extend_schema(
    summary="Upload a confirmation's claims",
    description="Starts the organization's submission for the confirmation (or uses its open version), keeps the file as that version's "
    "records and checks it at once against the year to date. 201 with the version: status validated when nothing is rejected, else "
    "draft with its findings. The earlier confirmations must be confirmed first.",
    request={"multipart/form-data": UploadClaims},
    responses={201: Version, 400: Refusal, 403: Refusal, 409: Refusal},
)
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def upload(request, pk):
    d = _deadline(pk)
    org = api.organisation(request)
    _acting(request, org, "submit")
    form = UploadClaims(data=request.data)
    form.is_valid(raise_exception=True)
    files = [(f.name, f.read()) for f in form.validated_data["file"]]
    try:
        c = combine_upload(files)
        v = service.upload_claims(request.user, d, org, c.data, c.filename, sources=c.sources)
    except (AccessDenied, UploadError) as e:
        return _refused(e)
    return _done(request, v, status.HTTP_201_CREATED)


@extend_schema(
    summary="Check the latest version again",
    description="Compiles the version's records over the year to date as it stands now and runs the checks again: what to do when an "
    "earlier confirmation has changed since (the version's `stale`).",
    request=None,
    responses={200: Version, 403: Refusal, 409: Refusal},
)
@api_view(["POST"])
def check(request, pk):
    try:
        v = service.check(request.user, _latest_version(request, pk, "submit"))
    except (AccessDenied, UploadError, _Conflict) as e:
        return _refused(e)
    return _done(request, v)


class PriceClaims(serializers.Serializer):
    again = serializers.BooleanField(
        default=False, help_text="Start another run although one was started since the records last changed (e.g. it failed)"
    )


@extend_schema(
    summary="Price the latest version's year to date",
    description="Starts the calculation run on the year's ACC (and allocation version) for a validated version. 202 with the version; "
    "its `pricing_run` is the run (readable in the calculations API with a calculations-scoped token, or on its page). When the run "
    "finishes the version's `priced` is true and it can be confirmed. A version already priced on its records, or with a run started "
    "since they changed, is refused unless `again` is set.",
    request={"application/json": PriceClaims, "multipart/form-data": PriceClaims},
    responses={202: Version, 403: Refusal, 409: Refusal},
)
@api_view(["POST"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def price(request, pk):
    form = PriceClaims(data=request.data)
    form.is_valid(raise_exception=True)
    try:
        v = _latest_version(request, pk, "submit")
        if service.priced(v):
            raise _Conflict("This version is already priced on exactly these records; confirm it.")
        run = service.pricing_run(v)
        if run and not form.validated_data["again"]:
            raise _Conflict(f"Run {run} was started for these records; follow it, or set again to start another.")
        service.price(request.user, v)
    except (AccessDenied, _Conflict) as e:
        return _refused(e)
    return _done(request, v, status.HTTP_202_ACCEPTED)


@extend_schema(
    summary="Confirm the latest version",
    description="The sign-off on exactly the priced results: the version is confirmed and published at once (its compiled inputs, own "
    "upload, job, spec and receipt; BR-05.9). Needs claims.confirm.",
    request=None,
    responses={200: Version, 403: Refusal, 409: Refusal},
)
@api_view(["POST"])
def confirm(request, pk):
    try:
        v = service.confirm(request.user, _latest_version(request, pk, "confirm"))
    except (AccessDenied, _Conflict) as e:
        return _refused(e)
    return _done(request, v)
