"""/api/v1/rules: the pre-upload checker as an API (BR-03.4) and the rule library as data.

POST check runs a module's rule-set step on one table, exactly as the upload and the /rules/check/ page run it (rules.precheck: the
module's own code), against the version in force or any other published version, and returns the findings. Nothing sent is kept. It
needs a signed-in call: a session, or an API token whose scopes include the module checked (claims or budgets); it needs no permission
in an organization, as the page does not. The rule sets (GET sets, sets/<id>) are public, as the rule library is: a set's JSON is its
rules with how each kind of rule is checked and the value lists it reads, for systems that check files without sending them."""

from django.http import HttpResponse
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from cedars.core import api, uploads

from . import precheck, service
from .models import RuleSet


def _base(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def set_json(request, rs: RuleSet, in_force: RuleSet | None) -> dict:
    c = precheck.checker_for(code=rs.code)
    return {
        "id": rs.pk,
        "code": rs.code,
        "label": rs.label,
        "module": c.module if c else None,
        "effective_from": rs.effective_from,
        "in_force": in_force is not None and in_force.pk == rs.pk,
        "rules": rs.rules.count(),
        "url": request.build_absolute_uri(f"/api/v1/rules/sets/{rs.pk}"),
        "page": request.build_absolute_uri(f"/rules/{rs.pk}/"),
    }


RuleSetSummary = inline_serializer(
    "RuleSetSummary",
    {
        "id": serializers.IntegerField(),
        "code": serializers.CharField(),
        "label": serializers.CharField(),
        "module": serializers.CharField(allow_null=True, help_text="The module whose uploads it checks: claims or budgets"),
        "effective_from": serializers.DateField(),
        "in_force": serializers.BooleanField(help_text="The version an upload today is checked against"),
        "rules": serializers.IntegerField(),
        "url": serializers.URLField(help_text="The set as JSON with how each rule is checked"),
        "page": serializers.URLField(),
    },
)


@extend_schema(
    summary="Published rule sets",
    description="Every published version of every module's rules, newest first within each; `in_force` marks the one an upload today "
    "is checked against. Public.",
    responses=inline_serializer("RuleSets", {"results": RuleSetSummary}),
)
@api_view(["GET"])
@permission_classes([AllowAny])
def sets(request):
    published = RuleSet.objects.filter(status=RuleSet.Status.PUBLISHED)
    in_force = {code: service.in_force(code) for code in set(published.values_list("code", flat=True))}
    return Response({"results": [set_json(request, rs, in_force.get(rs.code)) for rs in published]})


@extend_schema(
    summary="One rule set, to check files without sending them",
    description="A published rule set as data (format cedars-rule-set, version 1): each rule's test, message and suggested fix, the "
    "specification's columns per table, the value lists its value-list rules read (as in force here) and `how_to_check`, how each kind "
    "of rule is applied. The same file as the rule set page's JSON download. Public.",
    responses={200: OpenApiResponse(description="The rule set as JSON"), 404: OpenApiResponse(description="No such published rule set")},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def rule_set(request, pk):
    rs = RuleSet.objects.filter(pk=pk, status=RuleSet.Status.PUBLISHED).first()
    if rs is None:
        raise NotFound("No such published rule set.")
    return Response(precheck.rule_set_json(rs, _base(request)))


class CheckRequest(serializers.Serializer):
    file = serializers.FileField(
        validators=[uploads.check_size],
        help_text="One table as CSV (pipe-, tab- or comma-delimited, UTF-8 or Windows-1252); at most CEDARS_UPLOAD_MAX_MB (see /limits/)",
    )
    module = serializers.ChoiceField(
        choices=[], required=False, help_text="The filing the file is for: claims or budgets. May be left out when rule_set is given."
    )
    table = serializers.ChoiceField(choices=list(precheck.TABLES), default="Measure")
    rule_set = serializers.IntegerField(
        required=False, help_text="A published rule set's id (GET /api/v1/rules/sets); default the module's version in force"
    )
    filing_year = serializers.IntegerField(
        required=False,
        min_value=2000,
        max_value=2100,
        help_text="Claims: the claim year (default the one year the records are for). Budgets: check only that year's records "
        "(default every year, each as its own filing year).",
    )
    format = serializers.ChoiceField(
        choices=["json", "csv"], default="json", help_text="csv: the findings as the page's download (line, record_id, ...)"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["module"].choices = [c.module for c in precheck.checkers()]


Finding = inline_serializer(
    "CheckFinding",
    {
        "line": serializers.IntegerField(help_text="The file's line (the header is line 1)"),
        "record_id": serializers.CharField(help_text="CEInputID or ClaimID (Measure), PrgID (ProgramCost)"),
        "table": serializers.CharField(),
        "field": serializers.CharField(),
        "rule_id": serializers.CharField(),
        "severity": serializers.ChoiceField(choices=["reject", "warning"]),
        "message": serializers.CharField(),
        "suggested_fix": serializers.CharField(),
        "value": serializers.CharField(),
    },
)
CheckResult = inline_serializer(
    "CheckResult",
    {
        "module": serializers.CharField(),
        "table": serializers.CharField(),
        "rule_set": RuleSetSummary,
        "filing_year": serializers.IntegerField(allow_null=True),
        "file": serializers.CharField(),
        "records": serializers.IntegerField(),
        "rejected": serializers.IntegerField(help_text="Records with at least one reject"),
        "warnings": serializers.IntegerField(help_text="Records with at least one warning"),
        "findings_count": serializers.IntegerField(),
        "findings": Finding,
        "notes": serializers.ListField(child=serializers.CharField(), help_text="Rules that were not run, and why"),
    },
)
Refusal = inline_serializer("CheckRefusal", {"detail": serializers.CharField()})


@extend_schema(
    summary="Check a file before filing it",
    description="Runs the rule-set step of a module's validation on one table, the same code the upload and the /rules/check/ page "
    "run, against the module's rules in force or the published version named, and returns every finding, rejects first. Nothing is "
    "kept. The upload also runs the module's checks that need the whole filing (its other tables, earlier confirmations, eTRM's "
    "values for deemed claims), so a file that passes here can still get findings on upload. A token needs the module's scope. "
    "400: the request or the file cannot be checked as sent.",
    request={"multipart/form-data": CheckRequest},
    responses={
        200: OpenApiResponse(CheckResult, description="The findings (format=csv: text/csv)"),
        400: Refusal,
        403: Refusal,
        503: Refusal,
    },
)
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def check(request):
    form = CheckRequest(data=request.data)
    form.is_valid(raise_exception=True)
    d = form.validated_data
    rs = None
    if d.get("rule_set") is not None:
        rs = RuleSet.objects.filter(pk=d["rule_set"]).first()  # a draft is refused with its own reason (precheck.resolve)
        if rs is None:
            return Response({"detail": "No such published rule set (see GET /api/v1/rules/sets)."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        checker, rs = precheck.resolve(d.get("module"), rs)
    except precheck.CheckError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    token = api.token_of(request)
    if token is not None and checker.module not in token.scopes:
        raise PermissionDenied(f"This token has no {checker.module} scope.")
    upload = d["file"]
    try:
        result = precheck.check(checker.module, d["table"], upload.read(), rule_set=rs, filing_year=d.get("filing_year"))
    except service.EngineMissing as e:
        return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except precheck.CheckError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:  # noqa: BLE001 - a file that cannot be read is the caller's to fix, not a server error
        return Response({"detail": f"The file could not be read as a {d['table']} table: {e}"}, status=status.HTTP_400_BAD_REQUEST)
    if d["format"] == "csv":
        resp = HttpResponse(service.findings_csv(result.findings), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="findings_{result.table}.csv"'
        return resp
    return Response(
        {
            "module": checker.module,
            "table": result.table,
            "rule_set": set_json(request, rs, service.in_force(rs.code)),
            "filing_year": result.filing_year,
            "file": upload.name,
            "records": result.records,
            "rejected": result.rejected,
            "warnings": result.warned,
            "findings_count": len(result.findings),
            "findings": [
                {
                    "line": f.row,
                    "record_id": f.record_id,
                    "table": f.table,
                    "field": f.field,
                    "rule_id": f.rule_id,
                    "severity": f.severity,
                    "message": f.message,
                    "suggested_fix": f.fix,
                    "value": f.value,
                }
                for f in result.findings
            ],
            "notes": result.notes,
        }
    )
