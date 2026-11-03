"""The set builder's pages (refdata/builder.py): start a set, one page per step, the set's file. Every page posts and redirects; problems
are shown at the top of the step they belong to and beside the step in the navigation."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import builder, cost_tests
from . import builder_forms as bf
from .models import CostTestBuild


def _org(request):
    """The organization a set may be shared with: the one the user is acting for, when they may start runs there."""
    org = getattr(request, "organisation", None)
    return org if builder.can_submit(request.user, org) else None


def _build_or_404(request, pk) -> CostTestBuild:
    return get_object_or_404(builder.visible(request.user), pk=pk)


def _ctx(request, build: CostTestBuild, step: str, **extra) -> dict:
    nav = [
        (key, builder.STEP_LABELS[key], len((build.problems or {}).get(key, [])) if key != "review" else build.open_problems)
        for key in builder.PAGES
    ]
    return {
        "build": build,
        "step": step,
        "step_label": builder.STEP_LABELS[step],
        "nav": nav,
        "problems": (build.problems or {}).get(step, []) if step != "review" else [],
        "can_change": builder.can_change(request.user, build),
        "engine_here": builder.engine() is not None,
        "checked_by": build.checked_by,
        **extra,
    }


def _next(build, step: str):
    i = builder.PAGES.index(step)
    return reverse("refdata:build_step", args=[build.pk, builder.PAGES[min(i + 1, len(builder.PAGES) - 1)]])


def _saved(request, build, step):
    n = len((build.problems or {}).get(step, []))
    if n:
        messages.warning(request, f"Saved. This step has {n} problem{'s' if n != 1 else ''}, listed at the top.")
    else:
        messages.success(request, "Saved.")
    if request.POST.get("then") == "next":
        return redirect(_next(build, step))
    return redirect("refdata:build_step", pk=build.pk, step=step)


@login_required
def build_start(request):
    """Build a set: start from a copy of a registered set, one of the user's own, the engine's, a file, or nothing."""
    org = _org(request)
    builds = list(builder.visible(request.user))
    form = bf.StartForm(request.POST or None, request.FILES or None, builds=builds, engine_sets=builder.engine_sets(), organisation=org)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        share = org if d.get("share") else None
        try:
            if d["start"] == "pack":
                build = builder.start_from_pack(request.user, d["pack"], organisation=share)
            elif d["start"] == "build":
                build = builder.start_from_build(request.user, _build_or_404(request, int(d["build"])), organisation=share)
            elif d["start"] == "engine":
                build = builder.start_from_engine(request.user, d["engine_set"], organisation=share)
            elif d["start"] == "file":
                f = d["file"]
                build = builder.start_from_file(request.user, f.read(), f.name, organisation=share)
            else:
                build = builder.start_blank(request.user, organisation=share)
        except (builder.BuildError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Started {build} from {build.source}.")
            return redirect("refdata:build_step", pk=build.pk, step="set")
    return render(request, "refdata/builder/start.html", {"form": form, "engine_here": builder.engine() is not None})


@login_required
def build_step(request, pk, step):
    build = _build_or_404(request, pk)
    if step not in builder.PAGES:
        raise Http404("No such step")
    pages = {"set": _set, "pricings": _pricings, "engine": _engine, "inputs": _inputs, "aggregates": _aggregates, "tests": _tests}
    return {**pages, "review": _review}[step](request, build)


def _guard(request, build) -> bool:
    if request.method != "POST":
        return False
    if not builder.can_change(request.user, build):
        messages.error(request, "You cannot change this set.")
        return False
    return True


def _set(request, build):
    org = _org(request) or build.organisation
    form = bf.HeaderForm(request.POST if _guard(request, build) else None, initial=bf.HeaderForm.initial_for(build), organisation=org)
    if form.is_bound and form.is_valid():
        d = form.cleaned_data
        try:
            builder.set_header(
                request.user,
                build,
                workstream=d["workstream"],
                first_year=d["first_year"],
                tail=d["tail"],
                label=d["label"],
                description=d["description"],
                version=d["version"],
                draft=d["draft"],
                years_from=d["years_from"],
                years_to=d["years_to"],
                share=bool(d.get("share")),
                organisation=org,
            )
        except (builder.BuildError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            return _saved(request, build, "set")
    return render(request, "refdata/builder/set.html", _ctx(request, build, "set", form=form))


def _pricings(request, build):
    pricings = build.data.get("pricings") if isinstance(build.data.get("pricings"), dict) else {}
    initial = [{"id": pid, **{k: (p or {}).get(k, "") for k in builder.PRICING_KEYS}} for pid, p in pricings.items()]
    bound = _guard(request, build)
    formset = bf.PricingFormSet(request.POST if bound else None, initial=initial, prefix="p")
    new = bf.NewPricingForm(request.POST if bound else None, prefix="new", initial={"acc_rows": "trc"})
    error = ""
    if bound and formset.is_valid() and new.is_valid():
        rows = [f.cleaned_data for f in formset.forms]
        if [r["id"] for r in rows] != list(pricings):
            error = "The set's pricings changed while you were editing; here they are as they are now."
        else:
            try:
                builder.set_pricings(request.user, build, rows, new.cleaned_data if new.cleaned_data.get("id") else None)
            except (builder.BuildError, AccessDenied) as e:
                error = str(e)
            else:
                return _saved(request, build, "pricings")
    ids = list(pricings)
    rows = [(f, builder.uses_of_pricing(build.data, ids[i]) if i < len(ids) else []) for i, f in enumerate(formset.forms)]
    return render(
        request, "refdata/builder/pricings.html", _ctx(request, build, "pricings", formset=formset, rows=rows, new=new, error=error)
    )


def _engine(request, build):
    if builder.catalog() is None:
        return render(request, "refdata/builder/engine.html", _ctx(request, build, "engine", rows=[], no_catalog=True))
    rows = builder.engine_rows(build.data)
    form = bf.engine_form(rows, request.POST if _guard(request, build) else None)
    error = ""
    if form.is_bound and form.is_valid():
        try:
            builder.set_engine_streams(request.user, build, bf.engine_choices(form, rows))
        except (builder.BuildError, AccessDenied) as e:
            error = str(e)
        else:
            return _saved(request, build, "engine")
    shown = [(r, form[f"include_{r['name']}"], form[f"pricings_{r['name']}"], form[f"unit_{r['name']}"], form[f"definition_{r['name']}"])
             for r in rows]  # fmt: skip
    program = [(n, s) for n, s in (build.data.get("streams") or {}).items() if isinstance(s, dict) and s.get("source") == "program"]
    return render(
        request,
        "refdata/builder/engine.html",
        _ctx(
            request,
            build,
            "engine",
            rows=shown,
            form=form,
            error=error,
            program=program,
            unknown=builder.unknown_engine_streams(build.data),
        ),  # fmt: skip
    )


def _inputs(request, build):
    error = ""
    if _guard(request, build) and request.POST.get("remove"):
        try:
            builder.remove_input_stream(request.user, build, request.POST["remove"])
        except (builder.BuildError, AccessDenied) as e:
            error = str(e)
        else:
            messages.success(request, f"{request.POST['remove']} removed.")
            return redirect("refdata:build_step", pk=build.pk, step="inputs")
    return render(
        request, "refdata/builder/inputs.html", _ctx(request, build, "inputs", streams=builder.input_streams(build.data), error=error)
    )


def _measure_columns(build) -> list[str]:
    """The dictionary's numeric Measure fields, for an input stream's columns (and any the set already names)."""
    from cedars.services.dictionary import service as dictionary

    try:
        cols = [f.name for f, _ in dictionary.table_fields("calculations", "Measure") if f.data_type == "number"]
    except Exception:  # noqa: BLE001 - no catalog loaded: the box for column names still works
        cols = []
    return cols


@login_required
def build_input(request, pk, name=None):
    """Step 4: add or change one input stream."""
    build = _build_or_404(request, pk)
    streams = build.data.get("streams") or {}
    if name is not None and not (isinstance(streams.get(name), dict) and streams[name].get("source") == "input"):
        raise Http404("No such input stream")
    cols = _measure_columns(build)
    pricings = list((build.data.get("pricings") or {}).keys()) if isinstance(build.data.get("pricings"), dict) else []
    initial = (
        bf.InputStreamForm.initial_for(name, streams[name], cols)
        if name
        else {
            "kind": "benefit",
            "basis": "one_time",
            "ntg": "kwh",
            "market_effects": "benefits",
            "naming": "none",
            "gross": "divide_ntg",
            "unit": "$",
            "pricings": pricings[:1],
        }  # fmt: skip
    )
    form = bf.InputStreamForm(request.POST if _guard(request, build) else None, initial=initial, measure_columns=cols, pricings=pricings)
    if form.is_bound and form.is_valid():
        try:
            builder.put_input_stream(request.user, build, name, form.cleaned_data)
        except (builder.BuildError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            return _part_saved(request, build, "inputs", form.cleaned_data["name"])
    return render(request, "refdata/builder/input_stream.html", _ctx(request, build, "inputs", form=form, name=name))


def _streams_for(build, named=()) -> list[tuple[str, dict]]:
    """The streams an aggregate or test can name, and any it names that the set does not have (so a save keeps them, as problems)."""
    streams = builder.readable_streams(build.data)
    have = {n for n, _ in streams}
    return streams + [(n, {}) for n in dict.fromkeys(named) if n not in have]


def _pricing_ids(build) -> list[str]:
    return list(build.data.get("pricings") or {}) if isinstance(build.data.get("pricings"), dict) else []


def _aggregates(request, build):
    error = ""
    if _guard(request, build) and request.POST.get("remove"):
        try:
            builder.remove_aggregate(request.user, build, request.POST["remove"])
        except (builder.BuildError, AccessDenied) as e:
            error = str(e)
        else:
            messages.success(request, f"{request.POST['remove']} removed.")
            return redirect("refdata:build_step", pk=build.pk, step="aggregates")
    aggregates = [(n, a) for n, a in (build.data.get("aggregates") or {}).items() if isinstance(a, dict)]
    return render(request, "refdata/builder/aggregates.html", _ctx(request, build, "aggregates", aggregates=aggregates, error=error))


@login_required
def build_aggregate(request, pk, name=None):
    """Step 5: add or change one aggregate."""
    build = _build_or_404(request, pk)
    aggregates = build.data.get("aggregates") or {}
    if name is not None and not isinstance(aggregates.get(name), dict):
        raise Http404("No such aggregate")
    a = aggregates.get(name) or {}
    pricings = _pricing_ids(build)
    initial = bf.AggregateForm.initial_for(name, a) if name else {"pricing": pricings[0] if pricings else None, "unit": "$ (PV)"}
    form = bf.AggregateForm(
        request.POST if _guard(request, build) else None,
        initial=initial,
        pricings=pricings,
        streams=_streams_for(build, [*(a.get("plus") or []), *(a.get("minus") or [])]),
    )
    if form.is_bound and form.is_valid():
        try:
            builder.put_aggregate(request.user, build, name, form.cleaned_data)
        except (builder.BuildError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            return _part_saved(request, build, "aggregates", form.cleaned_data["name"])
    return render(request, "refdata/builder/aggregate.html", _ctx(request, build, "aggregates", form=form, name=name))


def _tests(request, build):
    error = ""
    if _guard(request, build):
        try:
            if request.POST.get("remove"):
                builder.remove_test(request.user, build, request.POST["remove"])
                messages.success(request, f"{request.POST['remove']} removed.")
            elif request.POST.get("up") or request.POST.get("down"):
                tid = request.POST.get("up") or request.POST.get("down")
                builder.move_test(request.user, build, tid, -1 if request.POST.get("up") else 1)
                messages.success(request, f"{tid} moved {'up' if request.POST.get('up') else 'down'}.")
        except (builder.BuildError, AccessDenied) as e:
            error = str(e)
        else:
            return redirect("refdata:build_step", pk=build.pk, step="tests")
    tests = [(t, v) for t, v in (build.data.get("tests") or {}).items() if isinstance(v, dict)]
    return render(request, "refdata/builder/tests.html", _ctx(request, build, "tests", tests=tests, error=error))


@login_required
def build_test(request, pk, tid=None):
    """Step 6: add or change one test."""
    build = _build_or_404(request, pk)
    tests = build.data.get("tests") or {}
    if tid is not None and not isinstance(tests.get(tid), dict):
        raise Http404("No such test")
    t = tests.get(tid) or {}
    pricings = _pricing_ids(build)
    initial = (
        bf.TestForm.initial_for(tid, t)
        if tid
        else {
            "pricing": pricings[0] if pricings else None,
            "share": "positive_benefits" if builder.has_program_costs(build.data) else "",
            "filing": True,
        }  # fmt: skip
    )
    named = [*(t.get("benefits") or []), *(t.get("numerator_adders") or []), *(t.get("costs") or [])]
    form = bf.TestForm(
        request.POST if _guard(request, build) else None,
        initial=initial,
        pricings=pricings,
        streams=_streams_for(build, named),
        shares=builder.share_choices(build.data, tid),
    )
    if form.is_bound and form.is_valid():
        try:
            builder.put_test(request.user, build, tid, form.cleaned_data)
        except (builder.BuildError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            return _part_saved(request, build, "tests", form.cleaned_data["id"])
    return render(request, "refdata/builder/test.html", _ctx(request, build, "tests", form=form, tid=tid))


def _part_saved(request, build, step, name):
    n = len((build.problems or {}).get(step, []))
    label = builder.STEP_LABELS[step]
    (messages.warning if n else messages.success)(
        request, f"{name} saved." + (f" {label} has {n} problem{'s' if n != 1 else ''}, listed at the top." if n else "")
    )
    return redirect("refdata:build_step", pk=build.pk, step=step)


def _review(request, build):
    register = bf.RegisterForm(request.POST or None) if builder.is_manager(request.user) else None
    return render(
        request,
        "refdata/builder/review.html",
        _ctx(
            request,
            build,
            "review",
            all_problems=[(builder.STEP_LABELS[k], k if k in builder.PAGES else "", v) for k, v in (build.problems or {}).items()],
            words=builder.words(build),
            yaml=builder.to_yaml(build.data).decode("utf-8"),
            file_name=builder.file_name(build),
            register=register,
            manager=builder.is_manager(request.user),
            set_aside=sorted(build.set_aside.items()),
            changes=builder.changes(build),
            parent=build.parent or build.parent_build,
        ),
    )


@login_required
def build_file(request, pk):
    build = _build_or_404(request, pk)
    response = HttpResponse(builder.to_yaml(build.data), content_type="application/yaml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{builder.file_name(build)}"'
    return response


@login_required
@require_POST
def build_action(request, pk):
    build = _build_or_404(request, pk)
    action = request.POST.get("action")
    try:
        if action == "discard":
            builder.discard(request.user, build)
            messages.success(request, f"{build} discarded.")
            return redirect("refdata:cost_tests")
        if action == "copy":
            shared = build.organisation if builder.can_submit(request.user, build.organisation) else None
            copy = builder.start_from_build(request.user, build, organisation=shared)
            messages.success(request, f"Started {copy} as a copy of {build}.")
            return redirect("refdata:build_step", pk=copy.pk, step="set")
        if action == "register":
            form = bf.RegisterForm(request.POST)
            form.is_valid()
            pack = builder.register(request.user, build, notes=form.cleaned_data.get("notes", ""))
            check = pack.tie_outs.filter(name="engine_check").order_by("-recorded_at", "-id").first()
            if check is None:
                messages.success(
                    request, f"{pack.label} {pack.version} registered as a draft pack. Record its engine check, then publish it."
                )
            elif check.passed:
                messages.success(
                    request,
                    f"{pack.label} {pack.version} registered as a draft pack, and the engine check passed. Publish it when it is due.",
                )
            else:
                messages.error(
                    request, f"{pack.label} {pack.version} registered as a draft pack, but the engine check failed: {check.summary}"
                )
            return redirect("refdata:detail", pk=pack.pk)
    except (builder.BuildError, AccessDenied, cost_tests.SetError) as e:
        messages.error(request, str(e))
    return redirect("refdata:build_step", pk=build.pk, step="review")
