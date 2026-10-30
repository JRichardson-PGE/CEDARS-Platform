"""Pages of subscriptions (BR-15.5) and key dates (BR-15.3): a user's own subscriptions, following a study or a work order from its page,
CPUC ED's list of subscribers, and the key dates CPUC ED posts on the library's home page."""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import deliverables, forms, service, subscriptions
from .models import AttributeList, Collection, DocumentSubscription, KeyDate, Study, WorkOrder


def _document_lists():
    types = list(AttributeList.objects.filter(type_of__in=Collection.values).prefetch_related("options"))
    others = list(AttributeList.objects.filter(type_of="", collections__len__gt=0).prefetch_related("options"))
    return types + others


@login_required
def my_subscriptions(request):
    """What the user follows, and what they hear about it. Collections and values are ticked here; studies and work orders are followed
    from their own pages and can be stopped here."""
    mine = subscriptions.of(request.user)
    if request.method == "POST":
        lists = _document_lists()
        allowed = {o.pk: o for al in lists for o in al.options.all()}
        chosen = [allowed[int(x)] for x in request.POST.getlist("options") if x.isdigit() and int(x) in allowed]
        collections = [c for c in request.POST.getlist("collections") if c in Collection.values]
        try:
            subscriptions.set_all(request.user, collections=collections, options=chosen, channel=request.POST.get("channel", ""))
        except service.DocumentError as e:
            messages.error(request, str(e))
        else:
            n = DocumentSubscription.objects.filter(user=request.user).count()
            messages.success(request, f"Your subscriptions are saved: you follow {n} thing{'s' if n != 1 else ''}.")
        return redirect("documents:subscriptions")
    followed = {s.option_id for s in mine if s.option_id}
    collections = {s.collection for s in mine if s.collection}
    channel = mine[0].channel if mine else DocumentSubscription.Channel.EMAIL
    today = timezone.localdate()
    lists = [(al, [(o, o.pk in followed) for o in al.options.all() if o.offered_on(today) or o.pk in followed]) for al in _document_lists()]
    return render(
        request,
        "documents/subscriptions.html",
        {
            "collections": [(value, label, value in collections) for value, label in Collection.choices],
            "lists": lists,
            "channels": DocumentSubscription.Channel.choices,
            "channel": channel,
            "followed": [s for s in mine if s.study_id or s.work_order_id],
            "is_ed": deliverables.is_ed(request.user),
        },
    )


def _next(request, default: str) -> str:
    url = request.POST.get("next", "")
    return url if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}) else default


@login_required
@require_POST
def follow(request):
    """Follow or stop following a study or a work order (the buttons on their pages and on the subscriptions page)."""
    study = get_object_or_404(Study, number=request.POST["study"]) if request.POST.get("study") else None
    work_order = get_object_or_404(WorkOrder, pk=request.POST["work_order"]) if request.POST.get("work_order") else None
    if (study is None) == (work_order is None):
        raise Http404
    name = f"study {study.study_id}" if study else f"work order {work_order.number}"
    if request.POST.get("action") == "unfollow":
        subscriptions.unsubscribe(request.user, study=study, work_order=work_order)
        messages.success(request, f"You no longer follow {name}.")
    else:
        channel = request.POST.get("channel") or DocumentSubscription.Channel.EMAIL
        subscriptions.subscribe(request.user, study=study, work_order=work_order, channel=channel)
        what = "its status changes and its documents are published" if study else "its documents are published"
        messages.success(request, f"You follow {name}: you will hear when {what}.")
    return redirect(_next(request, (study or work_order).get_absolute_url()))


def _require_ed(request):
    if not request.user.is_authenticated or not deliverables.is_ed(request.user):
        raise Http404


@login_required
def subscribers(request):
    """Who follows what (CPUC staff only): the PDA's subscriber list, as a page and a file."""
    _require_ed(request)
    rows = list(subscriptions.subscribers())
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="document_subscribers.csv"'
        w = csv.writer(response, lineterminator="\n")
        w.writerow(["Name", "Email", "Follows", "Channel", "Since"])
        for s in rows:
            w.writerow([s.user.name, s.user.email, s.target_label, s.get_channel_display(), s.created_at.date().isoformat()])
        return response
    by_target = {}
    for s in rows:
        by_target.setdefault(s.target_label, []).append(s)
    targets = sorted(by_target.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return render(request, "documents/subscribers.html", {"rows": rows, "by_target": targets, "users": len({s.user_id for s in rows})})


@login_required
@require_POST
def add_key_date(request):
    form = forms.KeyDateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the date and what happens on it (and a full web address for the link).")
        return redirect("documents:library")
    d = form.cleaned_data
    try:
        kd = subscriptions.add_key_date(request.user, d["on"], d["title"], detail=d.get("detail") or "", link=d.get("link") or "")
    except AccessDenied as e:
        raise Http404 from e
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{kd.title} on {kd.on:%Y-%m-%d} is on the home page.")
    return redirect("documents:library")


@login_required
@require_POST
def remove_key_date(request, pk):
    kd = get_object_or_404(KeyDate, pk=pk)
    try:
        subscriptions.remove_key_date(request.user, kd, reason=request.POST.get("reason", ""))
    except AccessDenied as e:
        raise Http404 from e
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{kd.title} is taken off the home page.")
    return redirect("documents:library")
