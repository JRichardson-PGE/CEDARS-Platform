from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from . import service
from .models import Notification, Preference, Subscription

OFF = "off"


@login_required
def index(request):
    """The user's notifications, newest first; opening the page does not mark them read (a button does)."""
    if request.method == "POST":
        n = service.mark_read(request.user, request.POST.getlist("id") or None)
        messages.success(request, f"{n} marked as read." if n != 1 else "1 marked as read.")
        return redirect("notifications:index")
    page = Paginator(Notification.objects.filter(user=request.user), 50).get_page(request.GET.get("page"))
    return render(request, "notifications/index.html", {"page": page, "unread": service.unread(request.user)})


@login_required
def settings_page(request):
    """Which events the user hears about, and how (BR-09.2): each event off, on the page, or on the page and by email."""
    user = request.user
    current = {s.event: s.channel for s in Subscription.objects.filter(user=user, module="", organisation__isnull=True)}
    pref, _ = Preference.objects.get_or_create(user=user)
    if request.method == "POST":
        for e in service.EVENTS:
            choice = request.POST.get(e.code, OFF)
            if choice == OFF:
                service.unsubscribe(user, e.code)
            elif choice in Subscription.Channel.values:
                service.subscribe(user, e.code, choice)
        pref.email_direct = request.POST.get("email_direct") == "on"
        pref.save()
        messages.success(request, "Notification settings saved.")
        return redirect("notifications:settings")
    groups = {}
    for e in service.EVENTS:
        groups.setdefault(e.group, []).append((e, current.get(e.code, OFF)))
    choices = [(OFF, "Off")] + list(Subscription.Channel.choices)
    return render(request, "notifications/settings.html", {"groups": groups.items(), "choices": choices, "pref": pref})
