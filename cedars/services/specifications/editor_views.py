"""The specification editor's pages, for CPUC system administrators: start a draft from a published version, then a page per part
of the draft (Settings, Tables and fields, Rules, Built-in checks, Changes, Publish), each field's and table's own form, and discard.
Every page renders on the server and works without scripts."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import editor, forms, service
from .models import SpecificationVersion

TABS = [
    ("edit_settings", "Settings"),
    ("edit", "Tables and fields"),
    ("edit_rules", "Rules"),
    ("edit_checks", "Built-in checks"),
    ("edit_changes", "Changes"),
    ("edit_publish", "Publish"),
]


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


def _editable(request, code: str, slug: str):
    """The draft, or a redirect to the version's page when it is not one."""
    _admin(request)
    v = _get(code, slug)
    if v.status != SpecificationVersion.Status.DRAFT:
        messages.info(request, "A published version never changes: start a new version from it.")
        return v, redirect("specifications:version", code, v.slug)
    return v, None


def _page(request, template: str, v, tab: str, *, sub: bool = False, **context):
    """A page of the draft: `tab` is the part it belongs to (`sub`: a field's or table's own page under it)."""
    found = editor.problems(v)
    return render(
        request,
        f"specifications/{template}",
        {"v": v, "tab": tab, "tabs": TABS, "here_is_tab": not sub, "problem_count": len(found), "problems": found, **context},
    )


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
    return _page(request, "edit_fields.html", v, "edit", tables=tables, engine=editor.engine_available())


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
    labels = service.check_group_labels()
    groups: dict[str, list] = {}
    for c in v.checks.all():
        groups.setdefault(c.group, []).append(c)
    return _page(request, "edit_checks.html", v, "edit_checks", groups=[(g, labels.get(g, g), cs) for g, cs in groups.items()])


@login_required
def edit_changes(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
    result = service.compare(v.based_on, v) if v.based_on_id else None
    return _page(request, "edit_changes.html", v, "edit_changes", result=result, lines=editor.change_lines(v))


@login_required
def edit_publish(request, code: str, slug: str):
    v, away = _editable(request, code, slug)
    if away:
        return away
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
        request, "edit_publish.html", v, "edit_publish", form=form, earliest=editor.earliest_day(), notice_days=releases.notice_days()
    )


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
        remove_form=forms.ReasonForm(auto_id="remove_%s"),
    )  # fmt: skip


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
