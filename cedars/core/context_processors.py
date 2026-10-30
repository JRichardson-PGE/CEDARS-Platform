from django.conf import settings


def platform(request):
    ctx = {"version": settings.VERSION, "environment": settings.ENVIRONMENT}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from cedars.services.accounts import service

        orgs = getattr(request, "user_organisations", [])
        ctx["nav"] = {
            "sysadmin": service.is_system_admin(user),
            "steward": service.is_steward(user),
            "org_admin": any(service.is_org_admin(user, o) for o in orgs),
        }
    return ctx
