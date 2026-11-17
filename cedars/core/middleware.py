from django.conf import settings


class SecurityHeadersMiddleware:
    """Headers Django 5.2 does not set itself: the Content-Security-Policy and a Permissions-Policy that turns off device APIs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        return response
