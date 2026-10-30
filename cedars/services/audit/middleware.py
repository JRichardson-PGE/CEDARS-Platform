import uuid

from .context import acting_as


class AuditContextMiddleware:
    """Sets the audit actor for the request: the signed-in user, their active organisation and their session's assurance.
    Runs after authentication and after accounts.middleware.ActiveOrganisationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user if getattr(request, "user", None) is not None and request.user.is_authenticated else None
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        with acting_as(user, getattr(request, "organisation", None), getattr(request, "assurance", ""), request_id):
            response = self.get_response(request)
        response.setdefault("X-Request-Id", request_id)
        return response
