"""The set builder's pages (refdata/builder.py): start a set, one page per step, the set's file. Every page posts and redirects; problems
are shown at the top of the step they belong to, beside the field they name (in the form's words, builder_forms.problem) and beside the
step in the navigation."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from cedars.core.templatetags.ui import person
from cedars.services.accounts.service import AccessDenied

from . import builder, cost_tests, formula
from . import builder_forms as bf
from .models import CostTestBuild


def _org(request):
    """The organization a set may be shared with: the one the user is acting for, when they may start runs there."""
    org = getattr(request, "organisation", None)
    return org if builder.can_submit(request.user, org) else None


def _build_or_404(request, pk) -> CostTestBuild:
    return get_object_or_404(builder.visible(request.user), pk=pk)


def problem_link(build: CostTestBuild, p: bf.Problem) -> str:
    """Where a problem is fixed: the field on its step's page, or on the page of the stream, aggregate or test it is about."""
    data = build.data
    if p.step == "pricings":
        ids = _pricing_ids(build)
        frag = f"id_p-{ids.index(p.name)}-{p.field}" if p.field and p.name in ids else f"pricing-{p.name}" if p.name else ""
    elif p.step == "engine":
        frag = f"stream-{p.name}" if p.name else ""
    else:
        frag = f"id_{p.field}" if p.field else ""
    own = {"inputs": ("streams", "refdata:build_input"), "aggregates": ("aggregates", "refdata:build_aggregate"),
           "tests": ("tests", "refdata:build_test")}.get(p.step)  # fmt: skip
    if own and p.name and isinstance((data.get(own[0]) or {}).get(p.name), dict):
        url = reverse(own[1], args=[build.pk, p.name])
    else:
        url = reverse("refdata:build_step", args=[build.pk, p.step])
    return f"{url}#{frag}" if frag else url


def _found(build: CostTestBuild, step: str) -> list:
    return bf.problems(build).get(step, [])


def by_name(build: CostTestBuild, step: str) -> dict[str, list]:
    """A step's problems by the stream, aggregate or test they are about, each with its link, for that part's row in the step's list."""
    out: dict[str, list] = {}
    for p in _found(build, step):
        if p.name:
            out.setdefault(p.name, []).append((p, problem_link(build, p)))
    return out


def _with_problems(build: CostTestBuild, step: str, rows: list[tuple[str, dict]]) -> list[tuple[str, dict, list]]:
    found = by_name(build, step)
    return [(n, x, found.get(n, [])) for n, x in rows]


def _ctx(request, build: CostTestBuild, step: str, **extra) -> dict:
    found = bf.problems(build).get(step, []) if step != "review" else []
    nav = [
        (key, builder.STEP_LABELS[key], len((build.problems or {}).get(key, [])) if key != "review" else build.open_problems)
        for key in builder.PAGES
    ]
    return {
        "build": build,
        "step": step,
        "step_label": builder.STEP_LABELS[step],
        "nav": nav,
        "problems": [(p, problem_link(build, p)) for p in found],
        "found": found,
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
    engine_sets = builder.engine_sets()
    picked = request.POST.get("start") or request.GET.get("start") or ""
    if request.method == "POST" and "next" in request.POST:
        # Without scripts: Next shows the field the choice needs, without checking anything yet
        form = bf.StartForm(initial={"start": picked}, builds=builds, engine_sets=engine_sets, organisation=org)
    else:
        form = bf.StartForm(request.POST or None, request.FILES or None, builds=builds, engine_sets=engine_sets, organisation=org)
    if picked not in bf.StartForm.FOLLOW:
        picked = ""
    if request.method == "POST" and "next" not in request.POST and form.is_valid():
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
    follow = [(k, form[f]) for k, f in bf.StartForm.FOLLOW.items() if f and k in dict(form.fields["start"].choices)]
    return render(request, "refdata/builder/start.html", {"form": form, "picked": picked, "follow": follow})


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
    bf.show_beside(form, _found(build, "set"))
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
    for i, f in enumerate(formset.forms):
        if i < len(ids):
            bf.show_beside(f, _found(build, "pricings"), part="pricings", name=ids[i])
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
    found, wording = by_name(build, "engine"), set()
    for r in rows:  # a problem with a stream's unit or definition sits beside that field, with its wording open
        n = r["name"]
        fields = {"unit": f"unit_{n}", "definition": f"definition_{n}"}
        if bf.show_beside(form, _found(build, "engine"), part="streams", name=n, fields=fields):
            wording.add(n)
    shown = [
        (r, form[f"include_{r['name']}"], form[f"pricings_{r['name']}"], form[f"unit_{r['name']}"], form[f"definition_{r['name']}"],
         [x for x in found.get(r["name"], []) if x[0].field not in ("unit", "definition")],
         r["name"] in wording or bool(form[f"unit_{r['name']}"].errors or form[f"definition_{r['name']}"].errors))
        for r in rows
    ]  # fmt: skip
    included = [x for x in shown if x[0]["included"]]
    left_out = [x for x in shown if not x[0]["included"]]
    program = [(n, s) for n, s in (build.data.get("streams") or {}).items() if isinstance(s, dict) and s.get("source") == "program"]
    return render(
        request,
        "refdata/builder/engine.html",
        _ctx(
            request,
            build,
            "engine",
            included=included,
            left_out=left_out,
            left_out_open=any(x[5] or x[6] for x in left_out),
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
    streams = _with_problems(build, "inputs", builder.input_streams(build.data))
    return render(request, "refdata/builder/inputs.html", _ctx(request, build, "inputs", streams=streams, error=error))


def _measure_columns(build) -> tuple[list[str], list[str]]:
    """The Measure fields an input stream can add up: the dictionary's per-unit dollar values (bf.adds_up). Counts, rates, years and
    energy savings (NumUnits, NTGRkW, EUL_Yrs, UnitkWh1stBaseline ...) are not offered; a column the set already names is kept in the
    box for other columns. Returns the columns offered and the ones that are not."""
    from cedars.services.dictionary import service as dictionary

    try:
        numbers = [f.name for f, _ in dictionary.table_fields("calculations", "Measure") if f.data_type == "number"]
    except Exception:  # noqa: BLE001 - no catalog loaded: the box for column names still works
        numbers = []
    return [c for c in numbers if bf.adds_up(c)], [c for c in numbers if not bf.adds_up(c)]


@login_required
def build_input(request, pk, name=None):
    """Step 4: add or change one input stream."""
    build = _build_or_404(request, pk)
    streams = build.data.get("streams") or {}
    if name is not None and not (isinstance(streams.get(name), dict) and streams[name].get("source") == "input"):
        raise Http404("No such input stream")
    cols, not_offered = _measure_columns(build)
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
    form = bf.InputStreamForm(
        request.POST if _guard(request, build) else None, initial=initial, measure_columns=cols, not_offered=not_offered, pricings=pricings
    )
    if form.is_bound and form.is_valid():
        try:
            builder.put_input_stream(request.user, build, name, form.cleaned_data)
        except (builder.BuildError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            return _part_saved(request, build, "inputs", form.cleaned_data["name"])
    if name:
        bf.show_beside(form, _found(build, "inputs"), part="streams", name=name)
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
    aggregates = _with_problems(build, "aggregates", aggregates)
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
    if name:
        bf.show_beside(form, _found(build, "aggregates"), part="aggregates", name=name)
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
    found = by_name(build, "tests")
    tests = [(t, v, formula.render(t, v), found.get(t, [])) for t, v in (build.data.get("tests") or {}).items() if isinstance(v, dict)]
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
    if tid:
        bf.show_beside(form, _found(build, "tests"), part="tests", name=tid)
    listed = _streams_for(build, named)
    catalog = {"streams": [{"name": n, "pricings": list(s.get("pricings") or [])} for n, s in listed], "shares": form.share_tests}
    return render(request, "refdata/builder/test.html", _ctx(request, build, "tests", form=form, tid=tid, catalog=catalog))


def _part_saved(request, build, step, name):
    n = len((build.problems or {}).get(step, []))
    label = builder.STEP_LABELS[step]
    (messages.warning if n else messages.success)(
        request, f"{name} saved." + (f" {label} has {n} problem{'s' if n != 1 else ''}, listed at the top." if n else "")
    )
    return redirect("refdata:build_step", pk=build.pk, step=step)


def _review(request, build):
    register = bf.RegisterForm() if builder.is_manager(request.user) else None
    return render(
        request,
        "refdata/builder/review.html",
        _ctx(
            request,
            build,
            "review",
            all_problems=[
                (builder.STEP_LABELS[k], k if k in builder.PAGES else "", [(p, problem_link(build, p)) for p in v])
                for k, v in bf.problems(build).items()
            ],
            words=builder.words(build),
            yaml=builder.to_yaml(build.data).decode("utf-8"),
            file_name=builder.file_name(build),
            register=register,
            manager=builder.is_manager(request.user),
            can_discard=builder.can_discard(request.user, build),
            set_aside=sorted(build.set_aside.items()),
            changes=builder.changes(build),
            offer=bf.OfferForm(),
            decline=bf.DeclineForm() if builder.is_manager(request.user) and build.status == "offered" else None,
            parent=build.parent or build.parent_build,
            grid=formula.grid(build.data),
        ),
    )


@login_required
def build_file(request, pk):
    build = _build_or_404(request, pk)
    response = HttpResponse(builder.to_yaml(build.data), content_type="application/yaml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{builder.file_name(build)}"'
    return response


@login_required
def build_discard(request, pk):
    """Discard a set: GET asks first, saying what goes; the confirmation's POST discards it (as deleting a run does)."""
    build = _build_or_404(request, pk)
    if not builder.can_discard(request.user, build):
        messages.error(request, "Only the person who started the set, or the organization's administrator, discards it.")
        return redirect("refdata:build_step", pk=build.pk, step="review")
    if request.method == "POST":
        if not request.POST.get("sure"):
            messages.error(request, "Confirm that the set goes for good.")
            return redirect("refdata:build_discard", pk=build.pk)
        try:
            builder.discard(request.user, build)
        except (builder.BuildError, AccessDenied) as e:
            messages.error(request, str(e))
            return redirect("refdata:build_step", pk=build.pk, step="review")
        messages.success(request, f"{build} discarded.")
        return redirect("refdata:cost_tests")
    return render(request, "refdata/builder/discard.html", {"build": build, "file_name": builder.file_name(build)})


@login_required
@require_POST
def build_action(request, pk):
    build = _build_or_404(request, pk)
    action = request.POST.get("action")
    try:
        if action == "discard":  # asks first (build_discard)
            return redirect("refdata:build_discard", pk=build.pk)
        if action == "copy":
            shared = build.organisation if builder.can_submit(request.user, build.organisation) else None
            copy = builder.start_from_build(request.user, build, organisation=shared)
            messages.success(request, f"Started {copy} as a copy of {build}.")
            return redirect("refdata:build_step", pk=copy.pk, step="set")
        if action == "offer":
            builder.offer(request.user, build, note=request.POST.get("note", ""))
            messages.success(request, f"{build} offered to the CPUC. You can still change it, or withdraw the offer.")
            return redirect("refdata:build_step", pk=build.pk, step="review")
        if action == "withdraw_offer":
            builder.withdraw_offer(request.user, build)
            messages.success(request, f"The offer of {build} is withdrawn.")
            return redirect("refdata:build_step", pk=build.pk, step="review")
        if action == "decline":
            builder.decline(request.user, build, reason=request.POST.get("reason", ""))
            messages.success(request, f"{build} declined; {person(build.offered_by or build.owner)} is sent the reason.")
            return redirect("refdata:cost_tests")
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
