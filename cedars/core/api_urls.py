"""/api/v1: the OpenAPI description and who-am-I; each module adds its own paths here."""

from django.urls import path
from drf_spectacular.utils import extend_schema, inline_serializer
from drf_spectacular.views import SpectacularAPIView
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import api


@extend_schema(
    summary="Who the call acts as",
    responses=inline_serializer(
        "Me",
        {
            "email": serializers.EmailField(),
            "organisation": serializers.CharField(allow_null=True),
            "assurance": serializers.CharField(),
            "token": serializers.CharField(allow_null=True),
            "permissions": serializers.ListField(child=serializers.CharField()),
        },
    ),
)
@api_view(["GET"])
def me(request):
    org = api.organisation(request)
    token = api.token_of(request)
    return Response(
        {
            "email": request.user.email,
            "organisation": org.code if org else None,
            "assurance": getattr(request._request, "assurance", ""),
            "token": f"{token.prefix}…" if token else None,
            "permissions": api.permissions_of(request),
        }
    )


urlpatterns = [
    path("schema", SpectacularAPIView.as_view(permission_classes=[AllowAny], authentication_classes=[]), name="api-schema"),
    path("me", me, name="api-me"),
]
