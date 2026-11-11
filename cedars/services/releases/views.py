"""Release notes and limits pages. Public: /releases/ (every published note), /releases/<id>/, /releases/feed.xml and /limits/.
CPUC system administrators: /releases/manage/ (drafts: write, publish, discard), Withdraw on a published note, and /limits/manage/
(the limits they may change)."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.syndication.views import Feed
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from cedars.core import revisions
from cedars.core.help import BY_SLUG
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications
from cedars.services.notifications.models import Subscription

from . import limits, service
from .models import Change, SiteNotice
from .service import ReleaseError

EVENT = "release.published"


def _sysadmin(user) -> bool:
    return user.is_authenticated and access.is_system_admin(user)


def _subscribed(user) -> bool:
    return user.is_authenticated and Subscription.objects.filter(user=user, event=EVENT).exists()


def _guides(version: str) -> list:
    """The user guides (and the FAQ) revised for a release, as (title, link)."""
    out = []
    for key in revisions.revised_in(version):
        if key == "faq":
            out.append(("Frequently asked questions", reverse("core:help_faq")))
        elif (g := BY_SLUG.get(key.split(":", 1)[1])) is not None:
            out.append((g.title, reverse("core:help_guide", args=[g.slug])))
    return out


@require_GET
def index(request):
    on = service.today()
    notes = list(service.published())
    coming = [n for n in notes if service.status(n, on) == "coming"]
    coming.sort(key=lambda n: (n.effective_on, n.pk))
    past = [n for n in notes if service.status(n, on) != "coming"]
    running = service.running()
    return render(
        request,
        "releases/index.html",
        {
            "coming": coming,
            "past": past,
            "running": running,
            "running_note": service.note_for_release(running),
            "running_guides": _guides(running),
            "notice_days": service.notice_days(),
            "sysadmin": _sysadmin(request.user),
            "subscribed": _subscribed(request.user),
            "today": on,
        },
    )


@require_GET
def detail(request, pk):
    note = get_object_or_404(service.published(), pk=pk)
    return render(
        request,
        "releases/detail.html",
        {
            "note": note,
            "status": service.status(note),
            "guides": _guides(note.version) if note.version else [],
            "running": service.running(),
            "areas": service.AREAS,
            "limits": limits.BY_KEY,
            "sysadmin": _sysadmin(request.user),
            "subscribed": _subscribed(request.user),
        },
    )


@require_POST
@login_required
def subscribe(request):
    """Send me each release note as it is published (by email), or stop."""
    if request.POST.get("stop"):
        notifications.unsubscribe(request.user, EVENT)
        messages.success(request, "You will no longer be sent release notes. They stay on this page.")
    else:
        notifications.subscribe(request.user, EVENT)
        messages.success(request, "Each release note will be sent to you by email as it is published.")
    back = request.POST.get("next", "")
    return redirect(back if back.startswith("/releases/") else "releases:index")


@require_GET
def limits_page(request):
    coming = service.coming_limits()
    rows = [(group, [(lim, coming.get(lim.key, [])) for lim in items]) for group, items in limits.groups()]
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "release": service.running(),
                "limits": [
                    {
                        "key": lim.key,
                        "label": lim.label,
                        "group": lim.group,
                        "current": lim.current(),
                        "applies": lim.applies,
                        "over": lim.over,
                        "coming": [
                            {
                                "value": c.limit_value,
                                "effective_on": n.effective_on.isoformat(),
                                "note": request.build_absolute_uri(f"/releases/{n.pk}/"),
                            }
                            for n, c in coming.get(lim.key, [])
                        ],
                    }
                    for lim in limits.LIMITS
                ],
            }
        )
    return render(request, "releases/limits.html", {"groups": rows, "sysadmin": _sysadmin(request.user)})


class ReleaseFeed(Feed):
    """Every published note, newest first, for feed readers and other systems."""

    title = "CEDARS release notes"
    description = "Changes to CEDARS' specifications, rules, limits and system, announced before they take effect."

    def link(self):
        return reverse("releases:index")

    def items(self):
        return service.published()[:50]

    def item_title(self, note):
        prefix = "Withdrawn: " if note.is_withdrawn else ""
        return f"{prefix}{note.title} (takes effect {note.effective_on:%Y-%m-%d})"

    def item_description(self, note):
        return note.withdrawn_reason if note.is_withdrawn else note.summary

    def item_link(self, note):
        return reverse("releases:detail", args=[note.pk])

    def item_pubdate(self, note):
        return note.published_at


# --- writing them (CPUC system administrators)


class NoteForm(forms.Form):
    title = forms.CharField(label="Title", max_length=200, help_text="e.g. Larger uploads for claims")
    version = forms.CharField(
        label="Platform release (optional)",
        max_length=20,
        required=False,
        help_text="The release it ships in, e.g. 0.3.0. Leave blank for a change that needs no new code (a rule set, a limit).",
    )
    summary = forms.CharField(label="Summary", widget=forms.Textarea(attrs={"rows": 3}), help_text="What changes and why")
    effective_on = forms.DateField(
        label="Takes effect on", widget=forms.DateInput(attrs={"type": "date"}), help_text="The day the changes apply"
    )


class ChangeForm(forms.Form):
    kind = forms.ChoiceField(label="Kind of change", choices=Change.Kind.choices)
    area = forms.ChoiceField(label="Part of CEDARS", choices=list(service.AREAS.items()))
    text = forms.CharField(label="What changes", widget=forms.Textarea(attrs={"rows": 2}))
    action = forms.CharField(label="What people need to do (optional)", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    limit = forms.ChoiceField(
        label="Limit (for a limit change)",
        required=False,
        choices=[],  # set per form, with each limit as it stands
    )
    limit_value = forms.CharField(label="New value (for a limit change)", max_length=60, required=False, help_text="e.g. 200 MB")

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.fields["limit"].choices = [("", "Not a limit change")] + [
            (lim.key, f"{lim.label} (now {lim.current()})") for lim in limits.LIMITS
        ]


def _require(request):
    if not _sysadmin(request.user):
        raise PermissionDenied("Only a CPUC system administrator writes release notes.")


@login_required
def manage(request):
    """Drafts and a new note."""
    _require(request)
    # another settings page may offer a note about the change just made (workflow settings): its title and summary start the draft
    form = NoteForm(request.POST or None, initial={k: request.GET[k][:2000] for k in ("title", "summary") if k in request.GET})
    if request.method == "POST" and form.is_valid():
        try:
            note = service.create_draft(request.user, **form.cleaned_data)
        except ReleaseError as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "Draft saved. Add its changes, then publish it.")
            return redirect("releases:edit", note.pk)
    return render(
        request,
        "releases/manage.html",
        {
            "form": form,
            "drafts": service.drafts(),
            "notice_days": service.notice_days(),
            "health": _health(),
            "running": service.running(),
            "limits": limits.LIMITS,
        },
    )


def _health():
    from .health import check

    return check()


@login_required
def edit(request, pk):
    """One draft: its fields, its changes, publish or discard."""
    _require(request)
    note = get_object_or_404(service.drafts(), pk=pk)
    note_form = NoteForm(initial={"title": note.title, "version": note.version, "summary": note.summary, "effective_on": note.effective_on})
    change_form = ChangeForm()
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "save":
                note_form = NoteForm(request.POST)
                if note_form.is_valid():
                    service.update_draft(request.user, note, **note_form.cleaned_data)
                    messages.success(request, "Draft saved.")
                    return redirect("releases:edit", note.pk)
            elif action == "add":
                change_form = ChangeForm(request.POST)
                if change_form.is_valid():
                    service.add_change(request.user, note, **change_form.cleaned_data)
                    messages.success(request, "Change added.")
                    return redirect("releases:edit", note.pk)
            elif action == "remove":
                service.remove_change(request.user, get_object_or_404(Change, pk=request.POST.get("change"), note=note))
                messages.success(request, "Change removed.")
                return redirect("releases:edit", note.pk)
            elif action == "publish":
                service.publish(request.user, note, short_notice_reason=request.POST.get("short_notice_reason", ""))
                messages.success(request, "Published. Everyone subscribed to release notes is being told.")
                return redirect("releases:detail", note.pk)
            elif action == "discard":
                service.discard_draft(request.user, note)
                messages.success(request, "Draft discarded.")
                return redirect("releases:manage")
            else:
                raise Http404
        except (ReleaseError, AccessDenied) as e:
            messages.error(request, str(e))
            return redirect("releases:edit", note.pk)
    days = (note.effective_on - service.today()).days
    return render(
        request,
        "releases/edit.html",
        {
            "note": note,
            "note_form": note_form,
            "change_form": change_form,
            "problem": service.notice_problem(note),
            "short_notice": days < service.notice_days(),
            "days": days,
            "notice_days": service.notice_days(),
            "areas": service.AREAS,
        },
    )


@require_POST
@login_required
def withdraw(request, pk):
    _require(request)
    note = get_object_or_404(service.published(), pk=pk)
    try:
        service.withdraw(request.user, note, request.POST.get("reason", ""))
    except ReleaseError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Withdrawn. Everyone subscribed to release notes is being told.")
    return redirect("releases:detail", note.pk)


# --- site notices


def _when(**attrs):
    return forms.DateTimeInput(attrs={"type": "datetime-local", **attrs}, format="%Y-%m-%dT%H:%M")


class NoticeForm(forms.Form):
    text = forms.CharField(
        label="What the notice says",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="A sentence or two, e.g. CEDARS will be unavailable on Saturday from 8 to 10 a.m. for maintenance.",
    )
    link = forms.CharField(
        label="Link (optional)",
        required=False,
        help_text="A page with more: on this site (starting with /) or elsewhere (https://...)",
    )
    starts_at = forms.DateTimeField(label="Shown from", required=False, widget=_when(), help_text="Pacific time; blank: now")
    ends_at = forms.DateTimeField(label="Shown until", widget=_when(), help_text="Pacific time")
    reason = forms.CharField(label="Why it is posted", widget=forms.Textarea(attrs={"rows": 2}), help_text="Kept in the audit log")


@login_required
def notices(request):
    """Site notices: post one, and every notice with its dates, current, coming and past, each current or coming one withdrawable."""
    if not _sysadmin(request.user):
        raise Http404
    form = NoticeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            service.post_notice(
                request.user,
                text=d["text"],
                link=d["link"],
                starts_at=d["starts_at"] or timezone.now(),
                ends_at=d["ends_at"],
                reason=d["reason"],
            )
        except ReleaseError as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "Posted. Every page shows it from its start until its end.")
            return redirect("releases:notices")
    now = timezone.now()
    every = list(SiteNotice.objects.select_related("created_by", "withdrawn_by"))
    shown = [n for n in every if not n.is_withdrawn and n.starts_at <= now < n.ends_at]
    coming = [n for n in every if not n.is_withdrawn and n.starts_at > now]
    past = [n for n in every if n not in shown and n not in coming]
    return render(request, "releases/notices.html", {"form": form, "shown": shown, "coming": coming, "past": past})


@require_POST
@login_required
def withdraw_notice(request, pk):
    if not _sysadmin(request.user):
        raise Http404
    notice = get_object_or_404(SiteNotice, pk=pk)
    try:
        service.withdraw_notice(request.user, notice, request.POST.get("reason", ""))
    except ReleaseError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Withdrawn. Pages no longer show it.")
    return redirect("releases:notices")


@login_required
def limits_manage(request):
    """Change limits: the editable limits, each with its value in force and its default, a form to change it or set it back, and the
    changes made so far. CPUC system administrators only (anyone else gets 404)."""
    if not _sysadmin(request.user):
        raise Http404
    if request.method == "POST":
        key = request.POST.get("key", "")
        reason = request.POST.get("reason", "")
        raw = request.POST.get("value", "").strip()
        if request.POST.get("action") == "default":
            value = None
        else:
            try:
                value = int(raw.replace(",", ""))
            except ValueError:
                messages.error(request, "Enter a whole number.")
                return redirect(f"{reverse('limits:manage')}#{key}" if key in limits.BY_KEY else reverse("limits:manage"))
        try:
            service.set_limit(request.user, key, value, reason)
        except (ReleaseError, AccessDenied) as e:
            messages.error(request, str(e))
        else:
            lim = limits.BY_KEY[key]
            messages.success(request, f"{lim.label}: now {lim.current()}.")
        return redirect(f"{reverse('limits:manage')}#{key}" if key in limits.BY_KEY else reverse("limits:manage"))
    by_key = limits.BY_KEY
    history = [(c, by_key.get(c.key)) for c in service.limit_changes()]
    return render(request, "releases/limits_manage.html", {"editable": limits.editable(), "history": history})
