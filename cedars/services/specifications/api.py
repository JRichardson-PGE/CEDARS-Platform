"""/api/v1/specifications: the published filing specifications as data, for program administrators' own systems. Open to everyone, as
the specification pages are (published and withdrawn versions only). A version's address is permanent; `current` follows the newest
published version of a specification."""

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import downloads, service
from .models import Specification, SpecificationVersion


def _base(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def _summary(request, v: SpecificationVersion, current) -> dict:
    base = _base(request)
    code = v.specification.code
    return {
        "name": v.name,
        "slug": v.slug,
        "status": v.status,
        "published_at": v.published_at,
        "current": current is not None and current.pk == v.pk,
        "fingerprint": v.fingerprint,
        "url": f"{base}/api/v1/specifications/{code}/{v.slug}",
        "page": f"{base}/specifications/{code}/{v.slug}/",
    }


VersionSummary = inline_serializer(
    "SpecificationVersionSummary",
    {
        "name": serializers.CharField(help_text="The name the CPUC gives the version"),
        "slug": serializers.CharField(help_text="The short form of the name in its addresses"),
        "status": serializers.ChoiceField(choices=["published", "withdrawn"]),
        "published_at": serializers.DateTimeField(),
        "current": serializers.BooleanField(help_text="The version new filing sets are set up with"),
        "fingerprint": serializers.CharField(help_text="SHA-256 of the version's content"),
        "url": serializers.URLField(help_text="The version as data"),
        "page": serializers.URLField(),
    },
)


@extend_schema(
    operation_id="specifications_list",
    summary="Filing specifications and their versions",
    description="Each kind of filing's specification (quarterly claims, budget and application filings, monthly reports) with its "
    "published and withdrawn versions, newest first. Open to everyone.",
    responses=inline_serializer(
        "Specifications",
        {
            "results": inline_serializer(
                "SpecificationSummary",
                {
                    "code": serializers.CharField(),
                    "name": serializers.CharField(),
                    "module": serializers.CharField(),
                    "about": serializers.CharField(),
                    "current": serializers.URLField(allow_null=True),
                    "versions": VersionSummary,
                },
                many=True,
            )
        },
    ),
)
@api_view(["GET"])
@permission_classes([AllowAny])
def index(request):
    out = []
    for s in Specification.objects.all():
        current = service.current(s)
        out.append(
            {
                "code": s.code,
                "name": s.name,
                "module": s.module,
                "about": s.about,
                "current": f"{_base(request)}/api/v1/specifications/{s.code}/current" if current else None,
                "versions": [_summary(request, v, current) for v in service.public_versions(s)],
            }
        )
    return Response({"results": out})


def _published(code: str, slug: str) -> SpecificationVersion:
    s = Specification.objects.filter(code=code).first()
    if s is None:
        raise NotFound("No such specification.")
    v = service.current(s) if slug == "current" else service.public_versions(s).filter(slug=slug).first()
    if v is None:
        raise NotFound("No such published version.")
    return v


@extend_schema(
    operation_id="specifications_version",
    summary="One specification version as data",
    description="The whole version (format cedars-filing-specification 1): its tables with their keys and fields (requirement, type, "
    "length, value list, default, example, other accepted names, definition), its rules in words, its built-in checks, what prices it "
    "and its file format, with each table's JSON Schema address. `current` in place of the version's short name gives the newest "
    "published version. The same file as the version page's JSON download. Open to everyone.",
    responses={200: OpenApiResponse(description="The version as JSON"), 404: OpenApiResponse(description="No such published version")},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def version(request, code, slug):
    return Response(downloads.document(_published(code, slug), _base(request)))


@extend_schema(
    operation_id="specifications_schema",
    summary="A JSON Schema for one table's records",
    description="A JSON Schema (2020-12) for one record of a table of the version, keyed by the fields' names. Open to everyone.",
    responses={200: OpenApiResponse(description="The JSON Schema"), 404: OpenApiResponse(description="No such version or table")},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def schema(request, code, slug, table):
    v = _published(code, slug)
    t = v.tables.filter(name=table).first()
    if t is None:
        raise NotFound("No such table.")
    return Response(downloads.json_schema(v, t))
