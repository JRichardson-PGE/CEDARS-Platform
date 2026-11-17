from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from rest_framework.throttling import BaseThrottle

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import forms, service, suggestions
from .models import Feedback


def form(request):
    """The form behind every page's Feedback link: anyone may send, signed in or not. It shows the help of the page the person
    came from, and before a question is sent (or when they ask) the guides and answers that match what they wrote."""
    posted = request.method == "POST"
    page = service.safe_page(request.POST.get("page") if posted else request.GET.get("from", ""))
    help_key = (request.POST.get("help") if posted else request.GET.get("help", "")) or ""
    entry, guide = suggestions.for_page(help_key)
    organisation = getattr(request, "organisation", None) if request.user.is_authenticated else None
    if not service.shared_with(organisation):  # nobody to keep it from
        organisation = None
    f = forms.FeedbackForm(request.POST or None, initial={"page": page}, organisation=organisation)
    matches, checked = [], False
    if posted and f.is_valid():
        kind = f.cleaned_data["kind"] or "comment"
        own_help = reverse("core:help_page", args=[entry.key]) if entry else ""
        matches = suggestions.matching(f.cleaned_data["message"], skip_url=own_help)
        checked = True
        # a question is held once when something may answer it; "Check for answers" shows the same for any kind
        hold = "check" in request.POST or (kind == "help" and matches and "checked" not in request.POST)
        if not hold:
            try:
                service.submit(
                    page=page,
                    message=f.cleaned_data["message"],
                    kind=kind,
                    private=f.cleaned_data.get("private", False),
                    user=request.user,
                    organisation=getattr(request, "organisation", None),
                    address=BaseThrottle().get_ident(request),
                )
            except service.FeedbackRefused as e:
                f.add_error("message", str(e))
            else:
                if request.user.is_authenticated:
                    messages.success(
                        request, "Thank you: your message was sent to the CPUC's CEDARS team. Follow it and any reply on My requests."
                    )
                else:
                    messages.success(request, "Thank you: your message was sent to the CPUC's CEDARS team.")
                return redirect(page)
    return render(
        request,
        "feedback/form.html",
        {
            "form": f,
            "page": page,
            "help_key": entry.key if entry else "",
            "entry": entry,
            "guide": guide,
            "matches": matches,
            "checked": checked and not f.errors,
        },
    )


def _require_staff(request):
    """The CPUC's system administrators and its support desk; anyone else is told nothing is here."""
    if not service.can_answer(request.user):
        raise Http404


def _for_staff(request, pk, *related) -> Feedback:
    """A request the signed-in member of staff may work on (a security concern is not there for the support desk)."""
    _require_staff(request)
    return get_object_or_404(service.for_staff(request.user).select_related(*related), pk=pk)


@login_required
def inbox(request):
    _require_staff(request)
    status = request.GET.get("status", "todo")
    kind = request.GET.get("kind", "")
    mine = service.for_staff(request.user)
    qs = mine.select_related("user", "organisation")
    if status == "todo":  # waiting on the CPUC: the urgent ones, then the longest-waiting first
        qs = qs.filter(status__in=Feedback.TO_ANSWER).order_by(*service.URGENT_FIRST)
    elif status in Feedback.Status.values:
        qs = qs.filter(status=status)
    else:
        status = "all"
    if kind in Feedback.Kind.values:
        qs = qs.filter(kind=kind)
    else:
        kind = ""
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    for fb in page:
        fb.status_words = service.status_words(fb, staff=True)
    counts = {s: mine.filter(status=s).count() for s in Feedback.Status.values}
    statuses = [("todo", f"To answer ({counts['new'] + counts['looking']})")]
    statuses += [(s, f"{service.status_words(Feedback(status=s), staff=True)} ({counts[s]})") for s in Feedback.Status.values]
    statuses.append(("all", "All"))
    return render(
        request,
        "feedback/inbox.html",
        {
            "page": page,
            "status": status,
            "kind": kind,
            "statuses": statuses,
            "kinds": [(k, w) for k, w in Feedback.Kind.choices if k != Feedback.Kind.SECURITY or access.is_system_admin(request.user)],
            "feedback_query": f"status={status}" + (f"&kind={kind}" if kind else ""),
        },
    )


@login_required
def detail(request, pk):
    return _detail_page(request, _for_staff(request, pk, "user", "organisation", "closed_by"))


def _detail_page(request, fb, **filled):
    forms_ = {
        "answer_form": forms.ReplyForm(
            auto_id="answer_%s", label="Your answer", help_text="The sender is told and reads it on My requests."
        ),
        "ask_form": forms.ReplyForm(
            auto_id="ask_%s", label="Your question", help_text="The request waits in the sender's My queue until they reply."
        ),
        "note_form": forms.ReplyForm(auto_id="note_%s", label="Note", help_text="For the CPUC's staff only. The sender never sees it."),
        "close_form": forms.CloseForm(auto_id="close_%s"),
        "reopen_form": forms.ReopenForm(auto_id="reopen_%s"),
    }
    forms_.update(filled)
    return render(
        request,
        "feedback/detail.html",
        {
            "fb": fb,
            "reads": service.readers(fb),
            "status_words": service.status_words(fb, staff=True),
            "conversation": service.conversation(fb, staff=True),
            **forms_,
        },
    )


def _staff_reply(request, pk, action, name, label, done):
    fb = _for_staff(request, pk, "user", "organisation", "closed_by")
    f = forms.ReplyForm(request.POST, auto_id=f"{name}_%s", label=label)
    if f.is_valid():
        try:
            action(request.user, fb, f.cleaned_data["body"])
        except (service.FeedbackRefused, AccessDenied) as e:
            f.add_error("body", str(e))
        else:
            messages.success(request, done)
            return redirect("feedback:detail", pk)
    return _detail_page(request, fb, **{f"{name}_form": f})


@login_required
@require_POST
def answer(request, pk):
    return _staff_reply(request, pk, service.answer, "answer", "Your answer", "Your answer was sent. The sender has been told.")


@login_required
@require_POST
def ask(request, pk):
    return _staff_reply(request, pk, service.ask, "ask", "Your question", "Your question was sent. The request now waits for the sender.")


@login_required
@require_POST
def note(request, pk):
    return _staff_reply(request, pk, service.add_note, "note", "Note", "Note kept.")


@login_required
@require_POST
def start(request, pk):
    fb = _for_staff(request, pk)
    try:
        service.start(request.user, fb)
    except (service.FeedbackRefused, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Marked as being looked at.")
    return redirect("feedback:detail", pk)


# ---- the sender's pages


@login_required
def mine(request):
    """My requests: what the signed-in person sent, the ones waiting on them first."""
    page = Paginator(service.mine(request.user), 50).get_page(request.GET.get("page"))
    org = getattr(request, "organisation", None)
    return render(
        request,
        "feedback/mine.html",
        {"page": page, "organisation_admin": service.shared_with(org) and access.is_org_admin(request.user, org)},
    )


def _own(request, pk) -> Feedback:
    """The sender's own request; anyone else is told nothing of it (administrators read it on the Feedback page)."""
    return get_object_or_404(Feedback.objects.select_related("closed_by"), pk=pk, user=request.user)


@login_required
def request_page(request, pk):
    """The sender's page for a request. The CPUC's staff are sent to theirs; an administrator of the organization the sender
    acted for reads it (a question, a problem or an accessibility barrier not kept private) without the reply forms."""
    fb = Feedback.objects.select_related("closed_by", "user", "organisation").filter(pk=pk).first()
    if fb is not None and fb.user_id != request.user.pk:
        if service.can_answer(request.user, fb):
            return redirect("feedback:detail", pk)
        if service.organisation_reads(request.user, fb):
            return _request_page(request, fb, own=False)
    return _request_page(request, _own(request, pk))


@login_required
def organization(request):
    """Our organization's requests: for its administrators, the questions, problems and accessibility barriers its members sent
    while acting for it (not the ones they kept private)."""
    org = getattr(request, "organisation", None)
    if not service.shared_with(org) or not access.is_org_admin(request.user, org):
        raise Http404
    page = Paginator(service.for_organisation(org).select_related("user"), 50).get_page(request.GET.get("page"))
    return render(request, "feedback/organization.html", {"page": page, "organisation": org})


def _request_page(request, fb, reply_form=None, own=True):
    waiting = fb.status == Feedback.Status.WAITING
    return render(
        request,
        "feedback/request.html",
        {
            "fb": fb,
            "own": own,
            "reads": service.readers(fb),
            "conversation": service.conversation(fb, staff=False),
            "reply_form": reply_form
            or forms.ReplyForm(auto_id="reply_%s", label="Your answer to the CPUC's question" if waiting else "Add to your request"),
        },
    )


@login_required
@require_POST
def reply(request, pk):
    fb = _own(request, pk)
    f = forms.ReplyForm(request.POST, auto_id="reply_%s")
    if f.is_valid():
        try:
            service.reply(request.user, fb, f.cleaned_data["body"])
        except (service.FeedbackRefused, AccessDenied) as e:
            f.add_error("body", str(e))
        else:
            messages.success(request, "Your reply was sent to the CPUC's CEDARS team.")
            return redirect("feedback:request", pk)
    return _request_page(request, fb, reply_form=f)


@login_required
@require_POST
def settle(request, pk):
    fb = _own(request, pk)
    try:
        service.settle(request.user, fb)
    except (service.FeedbackRefused, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Thank you. Your request is closed.")
    return redirect("feedback:request", pk)


@login_required
@require_POST
def close(request, pk):
    fb = _for_staff(request, pk)
    f = forms.CloseForm(request.POST)
    try:
        service.close(request.user, fb, f.cleaned_data["note"] if f.is_valid() else "")
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("feedback:detail", pk)
    messages.success(request, "Closed.")
    return redirect("feedback:inbox")


@login_required
@require_POST
def reopen(request, pk):
    fb = _for_staff(request, pk)
    f = forms.ReopenForm(request.POST)
    try:
        service.reopen(request.user, fb, f.cleaned_data["reason"] if f.is_valid() else "")
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("feedback:detail", pk)
    messages.success(request, "Reopened.")
    return redirect("feedback:detail", pk)
