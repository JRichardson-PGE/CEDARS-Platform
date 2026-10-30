"""The output templates pages (BR-06.10): the list with the default for new runs, and the page that makes or changes one."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import output_templates
from .forms import OutputTemplateForm


@login_required
def templates(request):
    mine = list(output_templates.visible(request.user).select_related("owner"))
    default = output_templates.default_for(request.user)
    if request.method == "POST":
        pk = request.POST.get("template", "")
        template = None if pk in ("", "all") else next((t for t in mine if str(t.pk) == pk), None)
        if pk not in ("", "all") and template is None:
            messages.error(request, "Unknown output template.")
        else:
            output_templates.set_default(request.user, template)
            messages.success(request, f"New runs now start with {f'“{template.name}”' if template else 'all columns'}.")
        return redirect("calculations:templates")
    rows = [(t, output_templates.can_change(request.user, t)) for t in mine]
    return render(request, "calculations/templates.html", {"rows": rows, "default": default})


@login_required
def template(request, pk=None):
    """New template (optionally a copy: ?from=<id>), or change one the person may change."""
    visible = output_templates.visible(request.user)
    obj = get_object_or_404(visible, pk=pk) if pk is not None else None
    if obj is not None and not output_templates.can_change(request.user, obj):
        return redirect(f"{reverse('calculations:template_new')}?from={obj.pk}")  # a template offered to everyone: copy it
    source = obj
    if obj is None and request.GET.get("from", "").isdigit():
        source = visible.filter(pk=request.GET["from"]).first()
    cat = output_templates.catalog()
    can_share = obj is None and access.is_system_admin(request.user)  # chosen when the template is made
    initial = {}
    if source is not None:
        initial = {"name": source.name if obj else "", "columns": "\n".join(source.columns), "shared": obj.shared if obj else False}
        initial["make_default"] = obj is not None and output_templates.default_for(request.user) == obj
    form = OutputTemplateForm(request.POST or None, initial=initial, catalog=cat, can_share=can_share)
    if request.method == "POST":
        if request.POST.get("action") == "delete" and obj is not None:
            try:
                output_templates.delete(request.user, obj)
            except AccessDenied as e:
                messages.error(request, str(e))
            else:
                messages.success(request, f"Deleted “{obj.name}”.")
            return redirect("calculations:templates")
        if form.is_valid():
            try:
                saved = output_templates.save(
                    request.user,
                    form.cleaned_data["name"],
                    form.picked(),
                    template=obj,
                    shared=bool(form.cleaned_data.get("shared")) and obj is None,
                )
                if form.cleaned_data.get("make_default"):
                    output_templates.set_default(request.user, saved)
                elif initial.get("make_default"):  # it was the default and the box was cleared
                    output_templates.set_default(request.user, None)
            except AccessDenied as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"Saved “{saved.name}” ({len(saved.columns)} columns).")
                return redirect("calculations:template", pk=saved.pk)
    current = output_templates.parse(form.data.get("columns", "") if form.is_bound else initial.get("columns", ""))
    known, unknown = output_templates.check(current, cat) if cat else ([], [])
    return render(
        request,
        "calculations/template.html",
        {
            "form": form,
            "template": obj,
            "source": source,
            "catalog": cat,
            "in_template": set(known),
            "count": len(known),
            "unknown": unknown,
            "ticked": set(form.data.getlist("add")) if form.is_bound else set(),
        },
    )
