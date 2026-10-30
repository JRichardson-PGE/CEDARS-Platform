"""The claims submissions API, read-only first (decision J: the submissions API with claims, read-only endpoints before writes): the
claim years and their confirmations, a PA's versions with their check results, findings, results by program and published files, the
change letters, and NMEC chains. A call acts for one organisation, the API token's (or the session's active one), and reads what the
pages show it: its own organisation's versions with claims.read, .submit or .confirm there, every organisation's from the CPUC with
claims.read or .approve, and every published version (public to signed-in users, BR-05.9). A token reaches claims only with the claims
scope, and never the PII vault."""

import mimetypes

from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from cedars.core import api, storage
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, Submission, SubmissionVersion

from . import service
from .models import FILED_TOTALS, PROGRAM_COST_FIELDS, ChangeLetter, ClaimYear

MODULE = "claims"
PER_PAGE = 500


# --- who may read what


def _scoped(request) -> bool:
    """Whether the call may reach claims at all: a session, or a token with the claims scope."""
    token = api.token_of(request)
    return token is None or MODULE in token.scopes


def _reads(request, organisation) -> bool:
    """Whether the call may read an organisation's unpublished versions and findings."""
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
    summary="One confirmation for one organisation: its versions",
    description="The acting organisation's versions, or ?org=CODE's for a CPUC reader. Others see only published versions.",
    parameters=[OpenApiParameter("org", str, description="An organisation code (CPUC readers; others see its published versions)")],
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
    org = get_object_or_404(Organisation, code=code) if code else api.organisation(request)
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
    description=f"Every finding of the module's checks and the claims rule set, {PER_PAGE} a page, in file order. The organisation "
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


@extend_schema(summary="A file a version publishes (or, for its organisation, keeps)", responses={(200, "application/octet-stream"): bytes})
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
    org = get_object_or_404(Organisation, code=code) if code else None
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
