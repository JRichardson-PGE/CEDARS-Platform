from django.conf import settings


def platform(request):
    # platform_version, not version: a page whose view passes its own "version" (a catalog, a filing) would hide the footer's
    ctx = {"platform_version": settings.VERSION, "environment": settings.ENVIRONMENT, "support_email": settings.CEDARS_SUPPORT_EMAIL}
    from cedars.services.releases.service import banner, site_notices

    if not hasattr(request, "_coming_changes"):  # once per request: a page can render several templates with this context
        request._coming_changes = banner()
        request._site_notices = site_notices()
    ctx["coming_changes"] = request._coming_changes  # announced changes taking effect within the notice period
    ctx["site_notices"] = request._site_notices  # the CPUC's dated notices (an outage, an extended deadline)
    if not hasattr(request, "_header"):  # once per request, as the banner
        request._header = _header(request)
    ctx.update(request._header)
    return ctx


def _header(request) -> dict:
    """The header's menus (core/nav.py), the counts beside My queue and Notifications, and `nav`, the visitor's roles for the pages."""
    from . import nav

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"menus": nav.header(request)}
    from cedars.services.accounts.middleware import mfa_pending
    from cedars.services.notifications.service import unread

    ctx = {"mfa_pending": mfa_pending(request)}
    have = nav.roles(request)
    ctx["nav"] = {k: k in have for k in ("sysadmin", "steward", "org_admin", "member", "approver")}
    ctx["menus"] = nav.header(request, have)
    if "member" in have and not ctx["mfa_pending"]:
        from . import queue

        ctx["queue_count"] = queue.count(user)
    ctx["unread_notifications"] = unread(user)
    return ctx
