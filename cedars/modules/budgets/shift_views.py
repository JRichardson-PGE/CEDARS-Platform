"""Fund shift and operating budget pages."""

import csv
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry import service as registry

from . import forms, shifts
from .models import FundShift

BLANK_ROWS = 4


def _shift(request, pk) -> FundShift:
    s = get_object_or_404(FundShift.objects.select_related("organisation", "cycle", "created_by", "submitted_by"), pk=pk)
    if not shifts.can_read(request.user, s):
        raise Http404
    return s


@login_required
def shift_list(request):
    org = request.filing_organisation  # the CPUC has no operating budget of its own: it sees every PA's (regulator wording)
    confirms = org is not None and shifts.access.has_permission(request.user, org, "budgets.confirm")
    return render(
        request,
        "budgets/shifts.html",
        {
            "org": org,
            "shifts": [(s, _waiting_on(s), _asked(_waits(s))) for s in shifts.readable(request.user).order_by("-created_at")[:200]],
            # each shift waiting for the acting PA, with what it has to cover (and the statewide programs it confirms as their lead)
            "waiting": [
                (s, shifts.portion(s, org.code), shifts.cover_words(org.code, shifts.portion(s, org.code), s.leads.get(org.code, ())))
                for s in shifts.waiting_for(org)
            ]
            if confirms
            else [],
            "can_submit": shifts.can_submit(request.user, org),
            "cycles": shifts.cycles(),
            # Only offered to those who may read the acting PA's operating budget (UAT 2, U03), or every PA's (CPUC staff).
            "can_read_operating": org is not None and shifts.can_read_operating(request.user, org),
            "can_read_every_operating": org is None and shifts.can_read_every_operating(request.user),
        },
    )


def _amounts(pairs) -> str:
    """PAs each with its amount to cover, "SCE ($35,000.00), SCG ($10,000.00)"; a lead PA with nothing to cover, "SCG (as a lead PA)"."""
    return ", ".join(f"{code} ({shifts.service_money(abs(amount)) if amount else 'as a lead PA'})" for code, amount in pairs)


def _asked(pairs) -> str:
    """What the PAs a shift waits for are asked to do, to follow their names: cover a portion, or (a lead PA with nothing to cover)
    confirm the shift."""
    amounts = [amount for _, amount in pairs]
    if not amounts:
        return ""
    if all(amounts):
        return "cover its portion" if len(amounts) == 1 else "cover their portions"
    return "cover their portions or confirm it" if any(amounts) else "confirm it"


def _waits(s) -> list[tuple]:
    """(PA, its amount to cover) for each PA a waiting shift still waits for."""
    if s.status != FundShift.Status.WAITING:
        return []
    return [(code, s.net_of(code)) for code in shifts.outstanding(s)]


def _waiting_on(s) -> str:
    """The PAs a waiting shift still waits for, each with its amount."""
    return _amounts(_waits(s))


@login_required
def shift_new(request):
    org = request.organisation
    if not shifts.can_submit(request.user, org):
        raise Http404
    cycles = shifts.cycles()
    leads = shifts.leads_statewide(org)
    form = forms.ShiftForm(
        request.POST or None, cycles=cycles, pa=org.code, leads=leads, initial={"cycle": shifts.current_cycle(cycles), "moves": shifts.OWN}
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            s = shifts.create(request.user, org, d["cycle"], title=d["title"], reason=d["reason"])
        except (AccessDenied, shifts.ShiftInvalid) as e:
            messages.error(request, str(e))
        else:
            if leads and d.get("moves") == shifts.STATEWIDE:
                messages.success(request, "Fund shift started. Enter the changes to statewide programs' budgets.")
                return redirect("budgets:shift_statewide", s.pk)
            messages.success(request, "Fund shift started. Enter the amounts it moves.")
            return redirect("budgets:shift_lines", s.pk)
    return render(request, "budgets/shift_new.html", {"form": form, "org": org, "leads": leads})


def _editable(request, s) -> bool:
    return s.status == FundShift.Status.DRAFT and shifts.can_submit(request.user, s.organisation) and request.organisation == s.organisation


def _covers(s, parts, acks) -> list[dict]:
    """The review table's rows, one per funding PA of the shift's statewide changes and per other lead PA of one: its portions, what
    it covers, how much its own programs' lines cover so far, what is left, and where that stands (`state`, which the page puts into
    words). Another lead PA confirms even with nothing to cover."""
    out = []
    for p in parts:
        if not p.portions and not p.leads:
            continue
        own, ack = p.pa == s.organisation.code, acks.get(p.pa)
        if p.portion == 0 and p.net == 0 and not p.leads:
            state = "nothing"
        elif own:
            state = "covered" if p.net == 0 else "left"
        elif ack is not None:
            state = "covered"
        else:
            state = {"draft": "on_submit", "waiting": "waiting"}.get(s.status, "never")
        out.append({"part": p, "own": own, "ack": ack, "state": state})
    return out


def _statewide_lines(s, mine, entry) -> list[dict]:
    """The shift's statewide program changes, each with the program's lead PA (as recorded on submission; a draft's, today's); for a
    draft, with the whole program's operating budget now and after."""
    leads = shifts.leads_now(s) if s.status == FundShift.Status.DRAFT else (s.leads or {})
    lead = {prg: code for code, led in leads.items() for prg in led}
    out = []
    for ln in mine.statewide_lines:
        e = entry.get(f"{ln.prg_id} {ln.year}")
        whole = Decimal(e["whole"]) if e else None
        after = whole + ln.amount if whole is not None else None
        out.append({"line": ln, "lead": lead.get(ln.prg_id, ""), "whole": whole, "after": after})
    return out


@login_required
def shift(request, pk):
    s = _shift(request, pk)
    org = request.organisation
    acks = {a.organisation.code: a for a in s.acknowledgements.select_related("organisation", "acknowledged_by")}
    editable = _editable(request, s)
    draft = s.status == FundShift.Status.DRAFT
    parts = shifts.parts(s)
    mine = parts[0]
    can_acknowledge = shifts.can_acknowledge(request.user, s, org)
    # a draft: who is asked to cover a portion, or to confirm as another statewide program's lead, once it is submitted
    others = [(p.pa, p.portion) for p in parts[1:] if p.portion != 0 or p.leads]
    return render(
        request,
        "budgets/shift.html",
        {
            "s": s,
            "steps": shifts.steps(s),
            "parts": parts,
            "mine": mine,
            "has_lines": any(p.lines for p in parts),
            "own_lines": [ln for p in parts for ln in p.own_lines],
            "statewide_lines": _statewide_lines(s, mine, shifts.statewide_entry(s) if draft and mine.statewide_lines else {}),
            "covers": _covers(s, parts, acks),
            "amounts": sorted(shifts.frozen(s).amounts.items(), key=lambda kv: shifts._key(kv[0])),
            "problems": shifts.problems(s) if draft else [],
            "editable": editable,
            "leads": editable and shifts.leads_statewide(s.organisation),
            "can_read_operating": shifts.can_read_operating(request.user, s.organisation),
            "can_withdraw": s.status in (FundShift.Status.DRAFT, FundShift.Status.WAITING)
            and shifts.can_submit(request.user, s.organisation)
            and org == s.organisation,
            "can_acknowledge": can_acknowledge,
            "my_cover": next((p for p in parts if org is not None and p.pa == org.code), None) if can_acknowledge else None,
            "waiting_on": _waiting_on(s),
            "waiting_asked": _asked(_waits(s)),
            "waiting_many": len(_waits(s)) > 1,
            "waiting_for_others": _amounts(others),
            "waiting_for_others_asked": _asked(others),
            "waiting_for_others_many": len(others) > 1,
            "other_leads": shifts.confirming(s),
            "org": org,
        },
    )


def _entered(request) -> list:
    """The lines typed in the table, then any pasted into the Paste lines box (one line each: PrgID, year, amount)."""
    typed = [
        shifts.LineIn(p, y, a)
        for p, y, a in zip(request.POST.getlist("prg_id"), request.POST.getlist("year"), request.POST.getlist("amount"), strict=False)
    ]
    return [ln for ln in typed if not ln.blank()] + shifts.paste_lines(request.POST.get("paste", ""))


def _amount(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _rows(request, entered, saved, budget=None) -> list[dict]:
    """The table's rows, each with what its program-year holds now and after its line (`budget`: by "PrgID year", a figure under
    "held" or "whole"), then blank ones to fill."""
    rows = (
        [{"prg_id": ln.prg_id, "year": ln.year, "amount": ln.amount} for ln in entered if not ln.blank()]
        if entered is not None
        else [{"prg_id": ln.prg_id, "year": str(ln.year), "amount": f"{ln.amount}"} for ln in saved]
    )
    for r in rows:
        b = (budget or {}).get(f"{r['prg_id'].strip()} {r['year'].strip()}")
        r["held"] = Decimal(b.get("held") or b.get("whole")) if b else None
        amount = _amount(r["amount"]) if r["amount"].strip() else None
        r["after"] = r["held"] + amount if r["held"] is not None and amount is not None else None
    extra = BLANK_ROWS + (int(request.GET.get("more", 0)) if str(request.GET.get("more", "")).isdigit() else 0)
    return rows + [{"prg_id": "", "year": "", "amount": "", "held": None, "after": None}] * extra


def _total(rows) -> Decimal | None:
    """What the lines add up to. None while a line's amount is not a figure."""
    total = Decimal(0)
    for r in rows:
        if not r["amount"].strip():
            continue
        amount = _amount(r["amount"])
        if amount is None:
            return None
        total += amount
    return total


def _table(request, s, organisation, entered, saved, step, portion=Decimal(0)) -> dict:
    """What `budgets/_shift_lines_table.html` needs: the kind of lines (`mode`), the rows, years, the programs for the picker, what
    each program-year holds, what the lines add up to and, for a PA's own programs, what is left of its portion to cover."""
    budget = shifts.statewide_entry(s) if step == shifts.STATEWIDE else shifts.entry_budget(organisation, s)
    rows = _rows(request, entered, saved, budget)
    total = _total(rows)
    return {
        "mode": step,
        "rows": rows,
        "years": _years(s),
        "programs": shifts.program_choices(s, organisation, step),
        "budget": budget,
        "total": total,
        "portion": portion,
        # the PA's own IDs of statewide programs, which no line may name: the page says why as one is typed
        "parts": {} if step == shifts.STATEWIDE else {k.lower(): v.prg_id for k, v in registry.statewide_parts(organisation).items()},
        "left": (portion + total if portion >= 0 else -(portion + total)) if total is not None else None,
        "pa": organisation.code,
    }


def _years(s) -> list[str]:
    return [str(y) for y in range(s.cycle.first_year, s.cycle.last_year + 1)]


def _save(request, s, step, then: str):
    """Save the lines posted for one step of a draft: the problems and the lines as entered when anything is wrong, else where to go
    next (`then`, or the same page with more blank lines for "Save and add more lines")."""
    entered = _entered(request)
    try:
        shifts.set_lines(request.user, s, entered, step)
    except shifts.ShiftInvalid as e:
        return e.problems, entered, None
    except AccessDenied as e:
        messages.error(request, str(e))
        return [], entered, redirect("budgets:shift", s.pk)
    if "more" in request.POST:
        return [], entered, redirect(f"{request.path}?more={int(request.GET.get('more', 0) or 0) + BLANK_ROWS}")
    return [], entered, redirect(then, s.pk)


@login_required
def shift_statewide(request, pk):
    """The changes to whole statewide programs' budgets (at least one of them a program the shift's PA leads), what every funding PA
    would cover, and which other lead PAs would confirm."""
    s = _shift(request, pk)
    if not _editable(request, s):
        raise Http404
    if not shifts.leads_statewide(s.organisation):
        messages.info(
            request,
            f"{s.organisation.code} leads no statewide program. A fund shift that changes statewide programs' budgets is made by the "
            f"lead PA of one of them, and {s.organisation.code} is then asked to cover its portion.",
        )
        return redirect("budgets:shift", pk)
    problems, entered = [], None
    if request.method == "POST":
        problems, entered, done = _save(request, s, shifts.STATEWIDE, "budgets:shift_lines")
        if done is not None:
            if "more" not in request.POST and not problems:
                messages.success(request, "Statewide changes saved.")
            return done
    parts = shifts.parts(s)
    return render(
        request,
        "budgets/shift_statewide.html",
        {
            "s": s,
            "steps": shifts.steps(s, on=shifts.STATEWIDE),
            "problems": problems,
            "split": [] if problems else [p for p in parts if p.portions or p.leads],
            **_table(request, s, s.organisation, entered, parts[0].statewide_lines, shifts.STATEWIDE),
        },
    )


@login_required
def shift_lines(request, pk):
    """The shift's PA's lines in its own programs: with a statewide change they cover its portion, without one they add up to zero."""
    s = _shift(request, pk)
    if not _editable(request, s):
        raise Http404
    problems, entered = [], None
    if request.method == "POST":
        problems, entered, done = _save(request, s, shifts.OWN, "budgets:shift")
        if done is not None:
            if "more" not in request.POST and not problems:
                messages.success(request, "Lines saved.")
            return done
    parts = shifts.parts(s)
    mine = parts[0]
    return render(
        request,
        "budgets/shift_lines.html",
        {
            "s": s,
            "steps": shifts.steps(s, on=shifts.OWN),
            "problems": problems,
            "mine": mine,
            "owed": -mine.portion,
            "leads": shifts.leads_statewide(s.organisation),
            **_table(request, s, s.organisation, entered, mine.own_lines, shifts.OWN, mine.portion),
        },
    )


@login_required
def shift_acknowledge(request, pk):
    """A co-funder covers its portion from its own programs and confirms the shift for its part; another statewide program's lead PA
    with nothing to cover confirms it."""
    s = _shift(request, pk)
    org = request.organisation
    if not shifts.can_acknowledge(request.user, s, org):
        raise Http404
    problems, entered = [], None
    if request.method == "POST":
        entered = _entered(request)
        try:
            shifts.acknowledge(request.user, s, org, entered, request.POST.get("note", ""))
        except shifts.ShiftInvalid as e:
            problems = e.problems
        except AccessDenied as e:
            messages.error(request, str(e))
            return redirect("budgets:shift", pk)
        else:
            s.refresh_from_db()
            done = " The shift is now in effect." if s.status == FundShift.Status.EFFECTIVE else ""
            covered = f"{org.code}'s portion is covered." if shifts.portion(s, org.code) else f"{org.code} confirmed the shift."
            messages.success(request, f"{covered}{done}")
            return redirect("budgets:shift", pk)
    part = next(p for p in shifts.parts(s) if p.pa == org.code)
    return render(
        request,
        "budgets/shift_acknowledge.html",
        {
            "s": s,
            "org": org,
            "steps": shifts.steps(s, on="cover"),
            "part": part,
            "owed": -part.portion,
            "problems": problems,
            **_table(request, s, org, entered, [], shifts.OWN, part.portion),
            "note": request.POST.get("note", ""),
        },
    )


def _act(request, pk, fn, done: str):
    s = _shift(request, pk)
    try:
        fn(s)
    except (AccessDenied, shifts.ShiftInvalid) as e:
        for p in getattr(e, "problems", None) or [str(e)]:
            messages.error(request, p)
    else:
        messages.success(request, done)
    return redirect("budgets:shift", pk)


@login_required
@require_POST
def shift_submit(request, pk):
    def do(s):
        s = shifts.submit(request.user, s)
        if s.status == FundShift.Status.WAITING:
            messages.info(request, f"Waiting for {_waiting_on(s)} to {_asked(_waits(s))}.")

    return _act(request, pk, do, "Fund shift submitted.")


@login_required
@require_POST
def shift_withdraw(request, pk):
    return _act(request, pk, lambda s: shifts.withdraw(request.user, s, request.POST.get("reason", "")), "Fund shift withdrawn.")


# --- the operating budget


def _operating(request, cycle):
    """The operating budget of the PA chosen with ?org=, else the acting PA's; None when neither names a PA (the CPUC's own page, which
    lists the PAs to choose from)."""
    cycle = registry.cycle_from_slug(cycle)
    if cycle is None:
        raise Http404
    code = request.GET.get("org")
    org = access.by_code_or_404(code) if code else request.filing_organisation
    if org is None and not code and shifts.can_read_every_operating(request.user):
        return cycle, None
    if org is None or not org.files or not shifts.can_read_operating(request.user, org):
        raise Http404
    return cycle, shifts.operating_budget(org, cycle)


def _cycle_pas(cycle):
    """The PAs with a budget filing or a fund shift in the cycle: the operating budgets there are to choose from."""
    pas = Organisation.objects.filter(fund_shifts__cycle=cycle) | Organisation.objects.filter(
        submissions__deadline__module="budgets", submissions__deadline__cycle=cycle
    )
    return [o for o in pas.distinct().order_by("code") if o.files]


def _year(request, ob) -> int | None:
    y = request.GET.get("year", "")
    return int(y) if y.isdigit() and int(y) in ob.years else None


@login_required
def operating(request, cycle):
    cycle, ob = _operating(request, cycle)
    if ob is None:
        return render(request, "budgets/operating.html", {"ob": None, "cycle": cycle, "orgs": _cycle_pas(cycle)})
    year = _year(request, ob)
    rows = [r for r in ob.rows if year is None or r.year == year]
    by_year = [(y, ob.total([y])) for y in ob.years]
    return render(
        request,
        "budgets/operating.html",
        {
            "ob": ob,
            "rows": rows,
            "year": year,
            "total": ob.total([year] if year else None),
            "by_year": by_year,
            "cycle": ob.cycle,
            "orgs": _cycle_pas(ob.cycle),
        },
    )


@login_required
def operating_csv(request, cycle):
    ob = _operating(request, cycle)[1]
    if ob is None:
        raise Http404
    year = _year(request, ob)
    resp = HttpResponse(content_type="text/csv")
    name = f"operating_budget_{ob.organisation.code}_{ob.cycle}{'_' + str(year) if year else ''}.csv"
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    w = csv.writer(resp)
    w.writerow(
        [
            "PA",
            "PrgID",
            "Program",
            "Year",
            "Approved budget",
            "Fund shifts",
            "Operating budget",
            "Statewide program budget",
            "Allocation factor",
        ]
    )
    for r in ob.rows:
        if year is None or r.year == year:
            w.writerow(
                [ob.organisation.code, r.prg_id, r.name, r.year, r.approved, r.shifted, r.operating]
                + ([r.statewide_total, r.factor] if r.statewide else ["", ""])
            )
    t = ob.total([year] if year else None)
    w.writerow([ob.organisation.code, "Total", "", year or "", t.approved, t.shifted, t.operating, "", ""])
    return resp
