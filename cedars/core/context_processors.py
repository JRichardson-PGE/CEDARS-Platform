from django.conf import settings


def platform(request):
    return {"version": settings.VERSION, "environment": settings.ENVIRONMENT}
