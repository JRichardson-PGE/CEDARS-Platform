"""The specification editor's pages, for CPUC system administrators: start a draft from a published version, then a page per part
of the draft (Settings, Tables and fields, Rules, Built-in checks, Examples, Impact test, Changes, Publish), each field's and table's
own form, and discard.
Every page renders on the server and works without scripts."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import comments, editor, examples, forms, impact, moving, service, transfer, workbook
from .models import SpecificationVersion

TABS = [
    ("edit_settings", "Settings"),
    ("edit", "Tables and fields"),
    ("edit_rules", "Rules"),
    ("edit_checks", "Built-in checks"),
    ("edit_examples", "Examples"),
    ("edit_impact", "Impact test"),
    ("edit_workbook", "Workbook"),
    ("edit_changes", "Changes"),
    ("edit_comments", "Comments"),
    ("edit_publish", "Publish"),
]
# a proposed version is frozen while filers comment: only the pages that read it (and publish it once comments close)
PROPOSED_TABS = [t for t in TABS if t[0] in ("edit_impact", "edit_changes", "edit_comments", "edit_publish")]


def _admin(request) -> None:
    if not editor.can_manage(request.user):
        raise PermissionDenied("Only a CPUC system administrator changes filing specifications.")


def _get(code: str, slug: str) -> SpecificationVersion:
    v = (
        SpecificationVersion.objects.select_related("specification", "based_on", "rule_set", "cost_test_set")
        .filter(specification__code=code, slug=slug)
        .first()
    )
    if v is None:
        raise Http404("No such specification version.")
    return v


def _editable(request, code: str, slug: str, *, proposed: bool = False):
    """The draft (or, with `proposed`, a version proposed for comment), or a redirect to the version's page when it is not one."""
    _admin(request)
    v = _get(code, slug)
    if proposed and v.status == SpecificationVersion.Status.PROPOSED:
        return v, None
    if v.status == SpecificationVersion.Status.PROPOSED:
        messages.info(request, f"{v.name} is proposed for comment and does not change: return it to draft to change it.")
        return v, redirect("specifications:version", code, v.slug)
    if v.status != SpecificationVersion.Status.DRAFT:
        messages.info(request, "A published version never changes: start a new version from it.")
        return v, redirect("specifications:version", code, v.slug)
    return v, None


def _page(request, template: str, v, tab: str, *, sub: bool = False, **context):
    """A page of the draft: `tab` is the part it belongs to (`sub`: a field's or table's own page under it)."""
    found = editor.problems(v)
    tabs = PROPOSED_TABS if v.status == SpecificationVersion.Status.PROPOSED else TABS
    return render(
        request,
        f"specifications/{template}",
        {"v": v, "tab": tab, "tabs": tabs, "here_is_tab": not sub, "problem_count": len(found), "problems": found,
         "comment_counts": comments.counts(v), **context},
    )  # fmt: skip


@login_required
@require_POST
def start(request, code: str, slug: str):
    _admin(request)
    based_on = _get(code, slug)
    form = forms.StartDraftForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the new version a name and say why it is made.")
        return redirect("specifications:version", code, slug)
    try:
        v = editor.start_draft(request.user, based_on, form.cleaned_data["name"], form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError, ValueError) as e:
        messages.error(request, str(e))
        return redirect("specifications:version", code, slug)
    messages.success(request, f"Started the draft {v.name}, a copy of {based_on.name}. Change it, then publish it.")
    return redirect("specifications:edit", code, v.slug)


@login_required
def edit(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    lock = editor.locks(v)
    tables = []
    for t in v.tables.prefetch_related("fields"):
        tables.append({"t": t, "fields": [{"f": f, "lock": lock.get((t.name, f.name), "")} for f in t.fields.all()]})
    return _page(
        request, "edit_fields.html", v, "edit", tables=tables, engine=editor.engine_available(), pending=editor.pending_renames(v),
        made=editor.made_lists(v),
    )  # fmt: skip


@login_required
def edit_settings(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    initial = {"name": v.name, "summary": v.summary, "engine_release": v.engine_release, "cost_test_set": v.cost_test_set}
    if request.method == "POST":
        form = forms.SettingsForm(request.POST, initial=initial)
        if form.is_valid():
            d = form.cleaned_data
            try:
                v = editor.update_settings(
                    request.user,
                    v,
                    name=d["name"],
                    summary=d["summary"],
                    engine_release=d["engine_release"],
                    cost_test_set=d["cost_test_set"],
                    reason=d["reason"],
                )
            except (AccessDenied, editor.EditError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, "Saved the settings.")
                return redirect("specifications:edit_settings", code, v.slug)
    else:
        form = forms.SettingsForm(initial=initial)
    return _page(request, "edit_settings.html", v, "edit_settings", form=form)


@login_required
def edit_rules(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    rules = list(v.rule_set.rules.order_by("table", "field", "rule_id")) if v.rule_set_id else []
    return _page(request, "edit_rules.html", v, "edit_rules", rules=rules)


@login_required
def edit_checks(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    groups = editor.check_rows(v)
    form = forms.ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        choosing = [r.check for _g, _l, rows in groups for r in rows]
        severities = {c.rule_id: request.POST.get(f"severity-{c.rule_id}", "") for c in choosing if c.severities}
        try:
            editor.set_checks(
                request.user,
                v,
                runs=[i for i in request.POST.getlist("run") if i],
                severities={k: s for k, s in severities.items() if s},
                reason=form.cleaned_data["reason"],
            )
        except (AccessDenied, editor.EditError) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "Saved the built-in checks.")
            return redirect("specifications:edit_checks", v.specification.code, v.slug)
    posted = set(request.POST.getlist("run")) if request.method == "POST" else None
    shown = [
        (g, label, [_CheckShown(r, r.runs if posted is None else (r.check.rule_id in posted or not r.check.optional),
                                request.POST.get(f"severity-{r.check.rule_id}") or r.severity) for r in rows])
        for g, label, rows in groups
    ]  # fmt: skip
    choosable = any(r.check.optional or r.check.severities for _g, _l, rows in groups for r in rows)
    return _page(
        request, "edit_checks.html", v, "edit_checks", groups=shown, form=form, choosable=choosable,
        writable=editor.writable_as_rules(v), as_rules_form=forms.ReasonForm(auto_id="as-rules-%s"),
    )  # fmt: skip


@login_required
@require_POST
def write_check_as_rules(request, code: str, slug: str):
    """Replace one of the draft's built-in checks with the rules that give its findings (editor.write_as_rules)."""
    v, away = _editable(request, code, slug)
    if away:
        return away
    form = forms.ReasonForm(request.POST)
    rule_id = request.POST.get("check", "")
    if not form.is_valid():
        messages.error(request, "Say why the check is written as rules: the reason is kept with the version.")
    else:
        try:
            written = editor.write_as_rules(request.user, v, rule_id, form.cleaned_data["reason"])
        except (AccessDenied, editor.EditError) as e:
            messages.error(request, str(e))
        else:
            n = len(written)
            messages.success(request, f"{rule_id} is written as {n} rule{'s' if n != 1 else ''} of this draft, on its Rules page.")
    return redirect("specifications:edit_checks", v.specification.code, v.slug)


class _CheckShown:
    """A check's row on the Built-in checks page, with what the form shows (what was posted, when a post is shown again)."""

    def __init__(self, row, runs: bool, severity: str):
        self.check, self.setting, self.runs, self.severity = row.check, row.setting, runs, severity


@login_required
def edit_examples(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    if examples.stale(v):  # checked as the draft now stands
        examples.check_all(v)
    sets = [
        {"e": e, "form": forms.ExampleFileForm(auto_id=f"file-{e.slug}-%s"), "records": sum(len(r) for r in (e.rows or {}).values()),
         "rejects": len(examples.rejects(e.report))}
        for e in v.examples.all()
    ]  # fmt: skip
    return _page(
        request, "edit_examples.html", v, "edit_examples", sets=sets, year=examples.example_year(), pa=examples.EXAMPLE_PA,
        has_generated=any(x["e"].slug == examples.GENERATED for x in sets), generate_form=forms.ReasonForm(auto_id="generate_%s"),
    )  # fmt: skip


@login_required
@require_POST
def generate_examples(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why the example is written again.")
        return redirect("specifications:edit_examples", code, slug)
    try:
        e = examples.generate(request.user, v, form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as err:
        messages.error(request, str(err))
        return redirect("specifications:edit_examples", code, slug)
    n = len(examples.rejects(e.report))
    messages.success(
        request, f"Wrote the example {e.name} again and checked it: {n} rejected finding{'s' if n != 1 else ''}." if n else
        f"Wrote the example {e.name} again and checked it: it passes.",
    )  # fmt: skip
    return redirect(f"{_examples_url(v)}#example-{e.slug}")


def _examples_url(v) -> str:
    from django.urls import reverse

    return reverse("specifications:edit_examples", args=[v.specification.code, v.slug])


@login_required
@require_POST
def example_file(request, code: str, slug: str, example: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    e = v.examples.filter(slug=example).first()
    if e is None:
        raise Http404("No such example.")
    form = forms.ExampleFileForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose the example's file and say why it changes.")
        return redirect(f"{_examples_url(v)}#example-{e.slug}")
    f = form.cleaned_data["file"]
    try:
        rows = examples.read_file(v, f.name, f.read())
        e = examples.replace_rows(request.user, e, rows, form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError, examples.ExampleError) as err:
        messages.error(request, str(err))
        return redirect(f"{_examples_url(v)}#example-{e.slug}")
    n = len(examples.rejects(e.report))
    messages.success(
        request, f"Loaded the example {e.name} and checked it: {n} rejected finding{'s' if n != 1 else ''}." if n else
        f"Loaded the example {e.name} and checked it: it passes.",
    )  # fmt: skip
    return redirect(f"{_examples_url(v)}#example-{e.slug}")


@login_required
def edit_impact(request, code: str, slug: str):
    v, away = _editable(request, code, slug, proposed=True)
    if away:
        return away
    test = impact.latest(v)
    return _page(
        request, "edit_impact.html", v, "edit_impact", test=test, report=test.report if test else {}, stale=impact.stale(v),
        tested=impact.filings(v),
    )  # fmt: skip


@login_required
@require_POST
def run_impact(request, code: str, slug: str):
    v, away = _editable(request, code, slug, proposed=True)
    if away:
        return away
    try:
        test = impact.run(request.user, v)
    except (AccessDenied, editor.EditError) as err:
        messages.error(request, str(err))
        return redirect("specifications:edit_impact", code, slug)
    r = test.report
    n = len(r["filings"])
    if not n:
        messages.success(request, "Ran the impact test: there is no confirmed filing of this kind to test yet.")
    else:
        rejected, warned = r["newly_rejected"], r["newly_warned"]
        messages.success(
            request,
            f"Ran the impact test on {n} filing{'s' if n != 1 else ''}: {rejected:,} record{'s' if rejected != 1 else ''} newly "
            f"rejected, {warned:,} newly warned.",
        )
    return redirect(reverse("specifications:edit_impact", args=[code, slug]) + "#impact-result")


WORKBOOK_SESSION = "specification_workbook"  # the workbook loaded for a preview, until it is confirmed: {draft id, sha256, name, data}


def _held(request, v) -> tuple[bytes, str] | None:
    """The workbook held for the draft's preview, and its name (None when there is none)."""
    import base64

    held = request.session.get(WORKBOOK_SESSION) or {}
    if held.get("draft") != v.pk:
        return None
    return base64.b64decode(held["data"]), held.get("name", "")


@login_required
def workbook_file(request, code: str, slug: str):
    """The draft as a workbook to edit."""
    from django.http import HttpResponse

    v, away = _editable(request, code, slug)
    if away:
        return away
    response = HttpResponse(workbook.draft_xlsx(v), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{workbook.file_name(v)}"'
    return response


@login_required
def edit_workbook(request, code: str, slug: str):
    """Download the draft as a workbook, load it back and see every difference before confirming it."""
    import base64

    v, away = _editable(request, code, slug)
    if away:
        return away
    form = forms.WorkbookForm()
    changes, problems, held_name, sha = None, [], "", ""
    if request.method == "POST":
        form = forms.WorkbookForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            content = f.read()
            try:
                changes, problems = workbook.preview(request.user, v, content)
            except (AccessDenied, editor.EditError) as e:
                form.add_error("file", str(e))
                request.session.pop(WORKBOOK_SESSION, None)
            else:
                sha, held_name = workbook.sha256(content), f.name
                request.session[WORKBOOK_SESSION] = {
                    "draft": v.pk, "sha256": sha, "name": f.name, "data": base64.b64encode(content).decode("ascii"),
                    "plan": workbook.plan_of(changes),
                }  # fmt: skip
    elif request.GET.get("held") and _held(request, v):  # back to the preview, e.g. after a refused confirmation
        content, held_name = _held(request, v)
        changes, problems = workbook.preview(request.user, v, content)
        sha = workbook.sha256(content)
        request.session[WORKBOOK_SESSION] = {**request.session[WORKBOOK_SESSION], "plan": workbook.plan_of(changes)}
    load_form = forms.WorkbookLoadForm(initial={"sha256": sha}, auto_id="load_%s") if changes else None
    return _page(
        request, "edit_workbook.html", v, "edit_workbook", form=form, changes=changes, problems=problems, held_name=held_name,
        load_form=load_form,
    )  # fmt: skip


@login_required
@require_POST
def load_workbook(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    form = forms.WorkbookLoadForm(request.POST)
    held = _held(request, v)
    back = f"{reverse('specifications:edit_workbook', args=[code, slug])}?held=1#workbook-preview-h"
    if held is None or workbook.sha256(held[0]) != request.POST.get("sha256") or not form.is_valid():
        if held is None or workbook.sha256(held[0]) != request.POST.get("sha256"):
            messages.error(request, "Load the workbook again: the one shown is no longer held.")
            return redirect("specifications:edit_workbook", code, slug)
        messages.error(request, "Say why the draft changes: the reason is kept with each change.")
        return redirect(back)
    try:
        if workbook.plan_of(workbook.read(v, held[0])) != (request.session.get(WORKBOOK_SESSION) or {}).get("plan"):
            messages.error(request, "The draft has changed since the differences were shown: read them again before confirming.")
            return redirect(back)
        changes = workbook.load(request.user, v, held[0], form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, f"Nothing was changed: {e}")
        return redirect(back)
    request.session.pop(WORKBOOK_SESSION, None)
    v.refresh_from_db()
    n = len(changes)
    messages.success(request, f"Loaded the workbook: {n} change{'s' if n != 1 else ''} made to the draft, each kept with the reason.")
    return redirect("specifications:edit_workbook", code, v.slug)


@login_required
def edit_changes(request, code: str, slug: str):
    v, away = _editable(request, code, slug, proposed=True)
    if away:
        return away
    result = service.compare(v.based_on, v) if v.based_on_id else None
    return _page(request, "edit_changes.html", v, "edit_changes", result=result, lines=editor.change_lines(v))


@login_required
def edit_publish(request, code: str, slug: str):
    v, away = _editable(request, code, slug, proposed=True)
    if away:
        return away
    draft = v.status == SpecificationVersion.Status.DRAFT
    if draft and request.method != "POST" and examples.stale(v):  # the checks below include the examples, as the draft now stands
        examples.check_all(v)
    initial = {"effective_on": editor.earliest_day(), "changes": " ".join(editor.change_lines(v))}
    if request.method == "POST":
        form = forms.PublishForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            try:
                v = editor.publish(
                    request.user,
                    v,
                    effective_on=d["effective_on"],
                    short_notice_reason=d["short_notice_reason"],
                    changes=d["changes"],
                    action=d["action"],
                )
            except (AccessDenied, editor.EditError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(
                    request,
                    f"Published {v.name}. Filing sets set up from {v.effective_on:%B} {v.effective_on.day}, {v.effective_on.year} use it, "
                    "and a release note announces it.",
                )
                return redirect("specifications:version", code, v.slug)
    else:
        form = forms.PublishForm(initial=initial)
    from cedars.services.releases import service as releases

    return _page(
        request, "edit_publish.html", v, "edit_publish", form=form, earliest=editor.earliest_day(), notice_days=releases.notice_days(),
        impact_test=impact.latest(v), impact_stale=impact.stale(v),
        propose_form=forms.ProposeForm(initial={"last_day": editor.earliest_day()}, auto_id="propose_%s") if draft else None,
    )  # fmt: skip


@login_required
@require_POST
def propose(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    form = forms.ProposeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the last day comments are taken.")
        return redirect("specifications:edit_publish", code, slug)
    try:
        v = editor.propose(request.user, v, last_day=form.cleaned_data["last_day"], note=form.cleaned_data["note"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
        return redirect("specifications:edit_publish", code, slug)
    d = v.comments_last_day
    messages.success(
        request,
        f"Proposed {v.name} for comment through {d:%B} {d.day}, {d.year}. Anyone signed in can read it, check a file against it and "
        "comment; it does not change until you return it to draft.",
    )
    return redirect("specifications:version", code, v.slug)


@login_required
@require_POST
def comment_period(request, code: str, slug: str):
    _admin(request)
    v = _get(code, slug)
    form = forms.CommentPeriodForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the new last day for comments and say why it changes.")
        return redirect(reverse("specifications:version", args=[code, slug]) + "#proposal-h")
    try:
        v = editor.change_comment_period(request.user, v, last_day=form.cleaned_data["last_day"], reason=form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
    else:
        d = v.comments_last_day
        messages.success(request, f"Comments on {v.name} are now open through {d:%B} {d.day}, {d.year}.")
    return redirect(reverse("specifications:version", args=[code, slug]) + "#proposal-h")


@login_required
@require_POST
def end_comments(request, code: str, slug: str):
    _admin(request)
    v = _get(code, slug)
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why comments end early.")
        return redirect(reverse("specifications:version", args=[code, slug]) + "#proposal-h")
    try:
        editor.end_comments(request.user, v, form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Comments on {v.name} are closed. Publish it as proposed, or return it to draft to change it.")
    return redirect(reverse("specifications:version", args=[code, slug]) + "#proposal-h")


@login_required
@require_POST
def return_to_draft(request, code: str, slug: str):
    _admin(request)
    v = _get(code, slug)
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why it goes back to draft.")
        return redirect(reverse("specifications:version", args=[code, slug]) + "#proposal-h")
    try:
        v = editor.return_to_draft(request.user, v, form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
        return redirect(reverse("specifications:version", args=[code, slug]) + "#proposal-h")
    messages.success(request, f"{v.name} is a draft again: change it, then propose it again or publish it. Its comments stay with it.")
    return redirect("specifications:edit_comments", code, v.slug)


@login_required
def edit_comments(request, code: str, slug: str):
    v, away = _editable(request, code, slug, proposed=True)
    if away:
        return away
    return _page(
        request, "edit_comments.html", v, "edit_comments", thread=comments.thread(v), reply_form=forms.ReplyForm(),
        can_answer=comments.can_answer(request.user, v),
    )  # fmt: skip


@login_required
@require_POST
def discard(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why the draft is discarded.")
        return redirect("specifications:edit_publish", code, slug)
    based_on = v.based_on
    try:
        editor.discard(request.user, v, form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
        return redirect("specifications:edit_publish", code, slug)
    messages.success(request, f"Discarded the draft {v.name}. Nothing a filing uses changed.")
    if based_on is not None:
        return redirect("specifications:version", code, based_on.slug)
    return redirect("specifications:index")


def _table(v, name: str):
    t = v.tables.filter(name=name).prefetch_related("fields").first()
    if t is None:
        raise Http404("No such table.")
    return t


@login_required
def edit_table(request, code: str, slug: str, table: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    t = _table(v, table)
    initial = {"other_names": ", ".join(t.other_names), "guidance": t.guidance}
    if request.method == "POST":
        form = forms.TableForm(request.POST, initial=initial)
        if form.is_valid():
            d = form.cleaned_data
            try:
                editor.change_table(
                    request.user, t, other_names=forms.names_list(d["other_names"]), guidance=d["guidance"], reason=d["reason"]
                )
            except (AccessDenied, editor.EditError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"Saved the {t.name} table.")
                return redirect(f"{_edit_url(v)}#table-{t.name}")
    else:
        form = forms.TableForm(initial=initial)
    return _page(request, "edit_table.html", v, "edit", sub=True, t=t, form=form)


def _edit_url(v) -> str:
    from django.urls import reverse

    return reverse("specifications:edit", args=[v.specification.code, v.slug])


@login_required
def add_field(request, code: str, slug: str, table: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    t = _table(v, table)
    if request.method == "POST":
        form = forms.AddFieldForm(request.POST, table=t)
        if form.is_valid():
            attrs = form.attrs()
            position = attrs.pop("position", None)
            try:
                f = editor.add_field(request.user, v, t.name, form.name(), position=position, reason=form.cleaned_data["reason"], **attrs)
            except (AccessDenied, editor.EditError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"Added {f.name} to the {t.name} table.")
                return redirect(f"{_edit_url(v)}#field-{t.name}-{f.name}")
    else:
        form = forms.AddFieldForm(table=t)
    return _page(request, "edit_field.html", v, "edit", sub=True, t=t, form=form, f=None, adding=True)


def _field(t, name: str):
    f = next((x for x in t.fields.all() if x.name == name), None)
    if f is None:
        raise Http404("No such field.")
    return f


@login_required
def edit_field(request, code: str, slug: str, table: str, field: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    t = _table(v, table)
    f = _field(t, field)
    lock = editor.locks(v).get((t.name, f.name), "")
    place = f.position  # the choice "after" the field before it (0: first)
    initial = {
        **{k: getattr(f, k) for k in forms.FieldForm.base_fields if hasattr(f, k) and k not in ("other_names", "position")},
        "other_names": ", ".join(f.other_names),
        "position": place,
    }
    if request.method == "POST":
        form = forms.FieldForm(request.POST, initial=initial, table=t, field=f, locked=lock)
        if form.is_valid():
            attrs = form.attrs()
            position = attrs.pop("position", None)
            try:
                editor.change_field(
                    request.user, f, position=position if position != place else None, reason=form.cleaned_data["reason"], **attrs
                )
            except (AccessDenied, editor.EditError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"Saved {t.name}.{f.name}.")
                return redirect(f"{_edit_url(v)}#field-{t.name}-{f.name}")
    else:
        form = forms.FieldForm(initial=initial, table=t, field=f, locked=lock)
    return _page(
        request, "edit_field.html", v, "edit", sub=True, t=t, form=form, f=f, lock=lock, adding=False,
        remove_form=forms.ReasonForm(auto_id="remove_%s"), rename_form=forms.RenameForm(auto_id="rename_%s"),
        list_form=forms.NewListForm(auto_id="list_%s"), made=next((m for m in editor.made_lists(v) if m.value_list.name == f.value_list),
                                                                  None),
    )  # fmt: skip


@login_required
@require_POST
def new_value_list(request, code: str, slug: str, table: str, field: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    t = _table(v, table)
    f = _field(t, field)
    form = forms.NewListForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the new list's name and say why the field needs it.")
        return redirect("specifications:edit_field", code, slug, t.name, f.name)
    d = form.cleaned_data
    try:
        vl = editor.new_value_list(request.user, f, d["name"], codes=d["codes"], reason=d["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
        return redirect("specifications:edit_field", code, slug, t.name, f.name)
    messages.success(
        request,
        f"Made the value list {vl.name} for {t.name}.{f.name}. It is published with this version; change its codes on its page.",
    )
    return redirect(f"{_edit_url(v)}#made-lists")


@login_required
@require_POST
def rename_field(request, code: str, slug: str, table: str, field: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    t = _table(v, table)
    f = _field(t, field)
    form = forms.RenameForm(request.POST)
    back = request.POST.get("back") == "fields"
    if not form.is_valid():
        messages.error(request, "Give the new name and say why the field is renamed.")
        return redirect(f"{_edit_url(v)}#pending-renames") if back else redirect("specifications:edit_field", code, slug, t.name, f.name)
    try:
        f = editor.rename_field(request.user, f, form.cleaned_data["new_name"], form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
        return redirect(f"{_edit_url(v)}#pending-renames") if back else redirect("specifications:edit_field", code, slug, t.name, f.name)
    messages.success(request, f"Renamed {t.name}.{f.renamed_from or field} to {f.name}; the old name is still accepted on upload.")
    return redirect(f"{_edit_url(v)}#field-{t.name}-{f.name}")


@login_required
@require_POST
def remove_field(request, code: str, slug: str, table: str, field: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    t = _table(v, table)
    f = _field(t, field)
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why the field is removed.")
        return redirect("specifications:edit_field", code, slug, t.name, f.name)
    try:
        editor.remove_field(request.user, f, form.cleaned_data["reason"])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, str(e))
        return redirect("specifications:edit_field", code, slug, t.name, f.name)
    messages.success(request, f"Removed {t.name}.{f.name} from this draft.")
    return redirect(f"{_edit_url(v)}#table-{t.name}")


# --- moving a filing set to a later version


@login_required
def move_filing_set(request, deadline: int):
    """A filing set's specification version and the later ones it can move to: choose one to see its impact test on the set's
    confirmed filings, then confirm the move with a reason."""
    from cedars.services.accounts import service as access
    from cedars.services.submissions import service as submissions
    from cedars.services.submissions.models import Deadline

    if not access.is_system_admin(request.user):
        raise PermissionDenied("Only a CPUC system administrator moves a filing set to another specification version.")
    d = Deadline.objects.select_related("specification_version__specification", "period", "cycle").filter(pk=deadline).first()
    if d is None:
        raise Http404("No such filing.")
    what, deadlines = submissions.filing_set_of(d)
    options = moving.later(deadlines)
    choose = forms.MoveChoiceForm(request.GET or None, versions=options)
    chosen = choose.cleaned_data["to"] if request.GET and choose.is_valid() else None
    confirm = forms.MoveForm(initial={"to": chosen.pk} if chosen else None)
    if request.method == "POST":
        confirm = forms.MoveForm(request.POST)
        chosen = next((v for v in options if str(v.pk) == request.POST.get("to", "")), None)  # kept on the page when refused
        if confirm.is_valid():
            if chosen is None:
                messages.error(request, "Choose a later version to move to.")
            else:
                try:
                    done = moving.move(request.user, deadlines, chosen, confirm.cleaned_data["reason"])
                except AccessDenied as err:
                    messages.error(request, str(err))
                else:
                    n = done["rechecked"]
                    messages.success(
                        request,
                        f"Moved {what} to {chosen.name}: {done['moved']} filing{'s' if done['moved'] != 1 else ''}, and "
                        f"{n} open version{'s' if n != 1 else ''} checked again." + "".join(f" {x}" for x in done["notes"]),
                    )
                    return redirect(submissions.setup_link(d) or reverse("submissions:filing_sets"))
    preview = moving.preview(chosen, deadlines) if chosen else None
    problems = moving.setup_problems(chosen, d.module, moving.accs_of(deadlines)) if chosen else []
    return render(
        request,
        "specifications/move_filing_set.html",
        {
            "d": d,
            "what": what,
            "deadlines": deadlines,
            "pinned": moving.current_of(deadlines),
            "options": options,
            "choose": choose,
            "chosen": chosen,
            "confirm": confirm,
            "report": preview,
            "problems": problems,
            "setup_url": submissions.setup_link(d),
            "open_count": submissions.open_versions_count(deadlines),
        },
    )


# --- export and import between environments

PACKAGE_SESSION = "specification_package"  # the package loaded for an import's preview, until it is confirmed: {sha256, name, data}


@login_required
@require_POST
def export_version(request, code: str, slug: str):
    """The version as a package to import into another environment (audited)."""
    from django.http import HttpResponse

    _admin(request)
    v = _get(code, slug)
    content = transfer.export(request.user, v)
    response = HttpResponse(content, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{transfer.file_name(v)}"'
    return response


def _held_package(request) -> tuple[bytes, str] | None:
    import base64

    held = request.session.get(PACKAGE_SESSION) or {}
    if not held.get("data"):
        return None
    return base64.b64decode(held["data"]), held.get("name", "")


@login_required
def import_version(request):
    """Load a package exported from another environment and see what it would make before importing it as a draft."""
    import base64

    _admin(request)
    form, preview, held_name = forms.PackageForm(), None, ""
    if request.method == "POST":
        form = forms.PackageForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            content = f.read()
            try:
                preview = transfer.preview(request.user, content)
            except (AccessDenied, editor.EditError) as e:
                form.add_error("file", str(e))
                request.session.pop(PACKAGE_SESSION, None)
            else:
                held_name = f.name
                request.session[PACKAGE_SESSION] = {
                    "sha256": preview.package.sha256, "name": f.name, "data": base64.b64encode(content).decode("ascii"),
                }  # fmt: skip
    elif request.GET.get("held") and _held_package(request):
        content, held_name = _held_package(request)
        preview = transfer.preview(request.user, content)
    import_form = forms.ImportForm(initial={"sha256": preview.package.sha256}, auto_id="import_%s") if preview else None
    return render(
        request,
        "specifications/import_version.html",
        {"form": form, "preview": preview, "held_name": held_name, "import_form": import_form, "environment": transfer.environment()},
    )


@login_required
@require_POST
def confirm_import(request):
    _admin(request)
    held = _held_package(request)
    back = f"{reverse('specifications:import_version')}?held=1#import-preview-h"
    if held is None or transfer.sha256(held[0]) != request.POST.get("sha256"):
        messages.error(request, "Load the package again: the one shown is no longer held.")
        return redirect("specifications:import_version")
    form = forms.ImportForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why the version is imported: the reason is kept with the draft.")
        return redirect(back)
    try:
        v = transfer.import_package(request.user, held[0], form.cleaned_data["reason"], held[1])
    except (AccessDenied, editor.EditError) as e:
        messages.error(request, f"Nothing was imported: {e}")
        return redirect(back)
    request.session.pop(PACKAGE_SESSION, None)
    messages.success(request, f"Imported {v.name} as a draft of {v.specification.name}. It is checked again here before it is published.")
    return redirect("specifications:edit", v.specification.code, v.slug)
