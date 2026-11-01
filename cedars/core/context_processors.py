from django.conf import settings


def platform(request):
    # platform_version, not version: a page whose view passes its own "version" (a catalog, a filing) would hide the footer's
    ctx = {"platform_version": settings.VERSION, "environment": settings.ENVIRONMENT, "support_email": settings.CEDARS_SUPPORT_EMAIL}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from cedars.services.accounts import service
        from cedars.services.accounts.middleware import mfa_pending

        ctx["mfa_pending"] = mfa_pending(request)

        orgs = getattr(request, "user_organisations", [])
        ctx["nav"] = {
            "sysadmin": service.is_system_admin(user),
            "steward": service.is_steward(user),
            "org_admin": any(service.is_org_admin(user, o) for o in orgs),
        }
        from cedars.services.notifications.service import unread

        ctx["unread_notifications"] = unread(user)
    return ctx
