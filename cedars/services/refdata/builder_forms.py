"""The set builder's forms (refdata/builder.py does the work). Plain Django forms: every control is a native input, and a list that
depends on another choice (the pricings a stream can be read in) is built on the server."""

import re
from dataclasses import dataclass

from django import forms

from cedars.core import uploads

from . import builder, formula
from .models import PackVersion

KINDS = [("benefit", "A benefit"), ("cost", "A cost")]
BASES = [
    ("one_time", "Once: NumUnits x (NTG + market effects) x the per-unit values, not discounted"),
    ("annual_over_life", "Each year of the measure's life, discounted at the pricing's rates"),
]
NTGS = [("kwh", "NTGRkWh"), ("therm", "NTGRTherm"), ("none", "None (1)")]
MARKET_EFFECTS = [("benefits", "The run's market effects on benefits"), ("costs", "The run's market effects on costs"), ("none", "None")]
NAMINGS = [
    ("none", "One column for every pricing (e.g. HealthNEB)"),
    ("suffix", "A column per pricing, with the pricing's suffix (e.g. HealthNEB_SB)"),
    ("participant_suffix", "A column per pricing, with the pricing's participant suffix (e.g. HealthNEB_SCT)"),
]
GROSS = [
    ("", "No gross form (a test with the Gross variant cannot use it)"),
    ("divide_ntg", "Computed: the same values with NTG 1, as <name>Gross"),
    ("same", "The same column (the values are already gross)"),
    ("column", "Computed with NTG 1, under the column name below"),
]


# An input stream adds up per-unit values times NumUnits, the NTG and market effects, in dollars. Of CET's Measure columns those are the
# Unit... columns that are not energy savings (UnitMeaCost1stBaseline, UnitMiscBens, UnitTaxCredits ...); NumUnits, the NTG ratios,
# realization and installation rates, EUL_Yrs, RUL_Yrs and MeasInflation are counts, rates and years, and the kW, kWh and therm
# savings are priced by the engine's own streams.
PER_UNIT_DOLLARS = re.compile(r"^Unit(?!(kW|kWh|Therm|kWhIOUWater)(1st|2nd)Baseline$)[A-Za-z0-9_]+$")


def adds_up(column: str) -> bool:
    """Whether a Measure column is a per-unit dollar value an input stream can add up."""
    return bool(PER_UNIT_DOLLARS.match(column))


def _names(text: str) -> list[str]:
    return [x for x in (p.strip() for p in (text or "").replace(";", ",").replace("\n", ",").split(",")) if x]


class StartForm(forms.Form):
    START = [
        ("pack", "A copy of a registered set"),
        ("build", "A copy of one of my sets"),
        ("engine", "A copy of a set the engine ships"),
        ("file", "A set file (.yaml) I have"),
        ("blank", "A blank set"),
    ]
    # The next field shown depends on this choice (FOLLOW): the page shows only that field, at once with scripts (refdata/builder_start.js)
    # and after Next without them.
    FOLLOW = {"pack": "pack", "build": "build", "engine": "engine_set", "file": "file", "blank": None}
    start = forms.ChoiceField(label="Start from", choices=START)
    pack = forms.ModelChoiceField(
        label="Registered set",
        required=False,
        queryset=PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS).exclude(status=PackVersion.Status.WITHDRAWN),
        help_text="Its file is copied, with today's date as the new version",
    )
    build = forms.ChoiceField(label="My set", required=False)
    engine_set = forms.ChoiceField(label="The engine's set", required=False)
    file = forms.FileField(
        label="Set file (.yaml)",
        required=False,
        validators=[uploads.check_size],
        help_text="Written by hand or downloaded from a set's page. A file with problems is still read in; they are shown on the steps",
    )
    share = forms.BooleanField(label="Share it with my organization", required=False)

    def __init__(self, *args, builds=(), engine_sets=(), organisation=None, **kwargs):
        super().__init__(*args, **kwargs)
        offered = [(k, v) for k, v in self.START if (k != "build" or builds) and (k != "engine" or engine_sets)]
        self.fields["start"].choices = [("", "Choose what to start from")] + offered
        self.fields["build"].choices = [("", "Choose a set")] + [(str(b.pk), f"{b} {b.label}".strip()) for b in builds]
        self.fields["engine_set"].choices = [("", "Choose a set")] + list(engine_sets)
        if organisation is None:
            del self.fields["share"]
        else:
            self.fields["share"].label = f"Share it with {organisation.name}"
            self.fields["share"].help_text = "Its members who may start runs can open and change it, and use it for their analysis runs"

    def clean(self):
        d = super().clean()
        need = self.FOLLOW.get(d.get("start"))
        if need and not d.get(need):
            self.add_error(need, "Choose one to start from.")
        return d


class HeaderForm(forms.Form):
    workstream = forms.CharField(
        label="Workstream", max_length=16, help_text="Upper-case letters and digits, e.g. EE (energy efficiency) or IQP"
    )
    first_year = forms.IntegerField(
        label="First program year", min_value=1990, max_value=2100, help_text="The year in the set's id, e.g. 2028 in EE-2028"
    )
    tail = forms.CharField(
        label="Id ending (optional)", max_length=40, required=False, help_text="Lower-case letters and digits, e.g. neb gives EE-2028-neb"
    )
    label = forms.CharField(label="Name", max_length=200, help_text="What pages call it, e.g. EE cost tests 2028")
    description = forms.CharField(label="Description (optional)", required=False, widget=forms.Textarea(attrs={"rows": 4}))
    version = forms.CharField(label="Version", max_length=60, help_text="Usually the date it was written, e.g. 2026-11-03")
    years_from = forms.IntegerField(
        label="In force from program year (optional)",
        min_value=1990,
        max_value=2100,
        required=False,
        help_text="Blank: no earlier bound (the set counts for every earlier program year too)",
    )
    years_to = forms.IntegerField(
        label="In force to program year (optional)",
        min_value=1990,
        max_value=2100,
        required=False,
        help_text="Blank: until a later set of the workstream starts",
    )
    draft = forms.BooleanField(
        label="This is a draft", required=False, help_text="A draft is never published and never prices a filing; analysis runs may use it"
    )
    share = forms.BooleanField(label="Share it with my organization", required=False)

    def __init__(self, *args, organisation=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organisation is None:
            del self.fields["share"]
        else:
            self.fields["share"].label = f"Share it with {organisation.name}"

    def clean(self):
        d = super().clean()
        a, b = d.get("years_from"), d.get("years_to")
        if a is not None and b is not None and b < a:
            self.add_error("years_to", "The last program year is before the first.")
        return d

    @classmethod
    def initial_for(cls, build) -> dict:
        ws, year, tail = builder.id_parts(build.data)
        py = build.data.get("program_years") if isinstance(build.data.get("program_years"), dict) else {}
        return {
            "workstream": ws,
            "first_year": year,
            "tail": tail,
            "label": build.data.get("label") or "",
            "description": build.data.get("description") or "",
            "version": build.data.get("version") or "",
            "years_from": py.get("from"),
            "years_to": py.get("to"),
            "draft": bool(build.data.get("draft")),
            "share": build.organisation_id is not None,
        }


class PricingForm(forms.Form):
    id = forms.CharField(widget=forms.HiddenInput)
    remove = forms.BooleanField(label="Remove", required=False)
    label = forms.CharField(label="Label", max_length=200)
    acc_rows = forms.ChoiceField(label="ACC rows", choices=list(builder.ACC_ROWS.items()))
    discount_rate_quarterly = forms.ChoiceField(label="Quarterly discount rate", choices=list(builder.RATE_COLUMNS.items()))
    discount_rate_annual = forms.ChoiceField(label="Annual discount rate", choices=list(builder.RATE_COLUMNS.items()))
    suffix = forms.CharField(label="Column suffix", max_length=40, required=False, help_text="Blank, or _ and letters, e.g. _SB")
    participant_suffix = forms.CharField(
        label="Participant column suffix", max_length=40, required=False, help_text="For the participant costs, e.g. _SCT"
    )


class NewPricingForm(PricingForm):
    id = forms.CharField(label="Id", max_length=64, required=False, help_text="A name, e.g. sct_low; it cannot be changed later")
    remove = None
    label = forms.CharField(label="Label", max_length=200, required=False)

    def clean(self):
        d = super().clean()
        if d.get("id") and not d.get("label"):
            self.add_error("label", "Give the new pricing a label.")
        return d


PricingFormSet = forms.formset_factory(PricingForm, extra=0)


def engine_form(rows: list[dict], data=None) -> forms.Form:
    """Step 3's form: per engine stream, include it, the pricings to read it in (only those the engine can), its unit and definition."""
    f = forms.Form(data)
    for r in rows:
        n = r["name"]
        f.fields[f"include_{n}"] = forms.BooleanField(label=f"Include {n}", required=False, initial=r["included"])
        f.fields[f"pricings_{n}"] = forms.MultipleChoiceField(
            label=f"Pricings {n} is read in",
            required=False,
            choices=[(p, p) for p in r["can"]],
            initial=[p for p in r["pricings"] if p in r["can"]],
            widget=forms.CheckboxSelectMultiple,
        )
        f.fields[f"unit_{n}"] = forms.CharField(label=f"Unit of {n}", max_length=60, required=False, initial=r["unit"])
        f.fields[f"definition_{n}"] = forms.CharField(
            label=f"Definition of {n}", required=False, initial=r["definition"], widget=forms.Textarea(attrs={"rows": 2})
        )
    return f


def engine_choices(form, rows) -> dict:
    d = form.cleaned_data
    return {
        r["name"]: {
            "include": d.get(f"include_{r['name']}"),
            "pricings": d.get(f"pricings_{r['name']}") or [],
            "unit": d.get(f"unit_{r['name']}") or "",
            "definition": d.get(f"definition_{r['name']}") or "",
        }
        for r in rows
    }


class InputStreamForm(forms.Form):
    name = forms.CharField(
        label="Name", max_length=64, help_text="The column it writes and the name tests use, e.g. HealthNEB (letters, digits and _)"
    )
    kind = forms.ChoiceField(label="It is", choices=KINDS, widget=forms.RadioSelect)
    columns = forms.MultipleChoiceField(
        label="Measure columns it adds up",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="CET's per-unit dollar columns; counts, rates, years and energy savings cannot be added up and are not offered",
    )
    new_columns = forms.CharField(
        label="Other Measure columns (optional)",
        required=False,
        help_text="New column names of your own, separated by commas (e.g. UnitNEBHealth); a job without one prices it as 0",
    )
    columns_2nd = forms.MultipleChoiceField(
        label="2nd-baseline columns (optional)",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Each year of the life only: the per-unit values after the remaining useful life; none = the columns above",
    )
    new_columns_2nd = forms.CharField(label="Other 2nd-baseline columns (optional)", required=False, help_text="Separated by commas")
    basis = forms.ChoiceField(label="Counted", choices=BASES, widget=forms.RadioSelect)
    ntg = forms.ChoiceField(label="Net-to-gross ratio", choices=NTGS, widget=forms.RadioSelect)
    market_effects = forms.ChoiceField(label="Market effects added to it", choices=MARKET_EFFECTS, widget=forms.RadioSelect)
    pricings = forms.MultipleChoiceField(
        label="Pricings it is read in", widget=forms.CheckboxSelectMultiple, help_text="A test reads it only in its own pricing"
    )
    naming = forms.ChoiceField(label="Its columns", choices=NAMINGS, widget=forms.RadioSelect)
    gross = forms.ChoiceField(label="Gross form", choices=GROSS, widget=forms.RadioSelect, required=False)
    gross_column = forms.CharField(label="Gross column name", max_length=64, required=False, help_text="e.g. HealthNEBGross")
    unit = forms.CharField(label="Unit", max_length=60, required=False, initial="$")
    definition = forms.CharField(
        label="Definition",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown in the run's field definitions and on its pages, so say what the values are",
    )

    # The two-list column pickers (core/field_picker.js) send the columns chosen as one comma field each; without scripts the fields
    # are disabled, so the tick boxes alone are sent.
    PICKED = {"columns": "columns_picked", "columns_2nd": "columns_2nd_picked"}

    def picked(self, field: str) -> list[str]:
        """The offered columns chosen for `field` (columns or columns_2nd), as its picker starts: what was sent, else the initial ones."""
        if self.is_bound:
            sent = self.data.get(self.PICKED[field])
            return _names(sent) if sent is not None else list(self.data.getlist(field))
        return list(self.initial.get(field) or [])

    def __init__(self, *args, measure_columns=(), not_offered=(), pricings=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.not_offered = set(not_offered)  # CET's Measure columns that cannot be added up (adds_up), refused when typed in the box
        cols = [(c, c) for c in measure_columns]
        self.fields["columns"].choices = cols
        self.fields["columns_2nd"].choices = cols
        self.fields["pricings"].choices = [(p, p) for p in pricings]

    def clean(self):
        d = super().clean()
        picked = {f: _names(self.data.get(p)) for f, p in self.PICKED.items()}
        d["columns"] = list(dict.fromkeys([*(d.get("columns") or []), *picked["columns"], *_names(d.get("new_columns"))]))
        d["columns_2nd"] = list(dict.fromkeys([*(d.get("columns_2nd") or []), *picked["columns_2nd"], *_names(d.get("new_columns_2nd"))]))
        if not d["columns"]:
            self.add_error("columns", "Choose or name at least one Measure column.")
        for c in d["columns"] + d["columns_2nd"]:
            if not builder.NAME.match(c):
                self.add_error("new_columns", f"{c} is not a column name (letters, digits and _, starting with a letter).")
            elif c in self.not_offered:
                field = "new_columns" if c in d["columns"] else "new_columns_2nd"
                self.add_error(field, f"{c} is a count, rate, number of years or energy saving, not a per-unit dollar value, so it "
                               "cannot be added up.")  # fmt: skip
        g = d.get("gross") or ""
        if g == "column":
            col = (d.get("gross_column") or "").strip()
            if not builder.NAME.match(col):
                self.add_error("gross_column", "Name the gross column (letters, digits and _).")
            d["gross"] = col
        return d

    @classmethod
    def initial_for(cls, name: str, s: dict, measure_columns: list[str]) -> dict:
        cols, cols2 = list(s.get("columns") or []), list(s.get("columns_2nd") or [])
        g = s.get("gross")
        return {
            "name": name,
            "kind": s.get("kind"),
            "columns": [c for c in cols if c in measure_columns],
            "new_columns": ", ".join(c for c in cols if c not in measure_columns),
            "columns_2nd": [c for c in cols2 if c in measure_columns],
            "new_columns_2nd": ", ".join(c for c in cols2 if c not in measure_columns),
            "basis": s.get("basis"),
            "ntg": s.get("ntg"),
            "market_effects": s.get("market_effects"),
            "pricings": list(s.get("pricings") or []),
            "naming": s.get("naming", "none"),
            "gross": "" if g is None else g if g in ("divide_ntg", "same") else "column",
            "gross_column": g if g not in (None, "divide_ntg", "same") else "",
            "unit": s.get("unit", ""),
            "definition": s.get("definition", ""),
        }


class RegisterForm(forms.Form):
    notes = forms.CharField(label="Notes on the pack (optional)", required=False, widget=forms.Textarea(attrs={"rows": 2}))


def _stream_choices(streams) -> list[tuple[str, str]]:
    """A stream as a choice: its name and the pricings it is read in, so a choice outside the pricing chosen is easy to see."""
    return [(n, f"{n} ({', '.join(s.get('pricings') or []) or 'no pricing'})") for n, s in streams]


class AggregateForm(forms.Form):
    name = forms.CharField(label="Name", max_length=64, help_text="The column it writes, e.g. TotalSystemBenefit (letters, digits and _)")
    pricing = forms.ChoiceField(label="Pricing", widget=forms.RadioSelect, help_text="Every stream it adds is read in this pricing")
    plus = forms.MultipleChoiceField(label="Streams it adds", widget=forms.CheckboxSelectMultiple)
    minus = forms.MultipleChoiceField(label="Streams it takes away", required=False, widget=forms.CheckboxSelectMultiple)
    gross = forms.BooleanField(label="Also write it gross (<name>Gross, from each stream's gross form)", required=False)
    unit = forms.CharField(label="Unit", max_length=60, required=False, initial="$ (PV)")
    definition = forms.CharField(label="Definition", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, pricings=(), streams=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pricing"].choices = [(p, p) for p in pricings]
        self.fields["plus"].choices = self.fields["minus"].choices = _stream_choices(streams)

    @staticmethod
    def initial_for(name: str, a: dict) -> dict:
        return {"name": name, **{k: a.get(k, builder.AGGREGATE_DEFAULTS.get(k)) for k in builder.AGGREGATE_KEYS}}


class TestForm(forms.Form):
    VARIANTS = [
        ("NoAdmin", "NoAdmin: also without the program costs (<T>CostNoAdmin, <T>RatioNoAdmin)"),
        ("Gross", "Gross: also on the gross forms, with no net-to-gross (<T>BenGross, <T>CostGross, <T>RatioGross)"),
    ]
    LEVELIZED = [("", "Levelized on the test's cost"), ("NoAdmin", "Levelized on the cost without program costs (needs NoAdmin)")]

    id = forms.CharField(
        label="Test id",
        max_length=16,
        help_text="A capital letter, then letters and digits (TRC, PAC, JST1): it names <T>Ben, <T>Cost, <T>Ratio",
    )
    label = forms.CharField(label="Name", max_length=120, help_text="e.g. Total Resource Cost")
    pricing = forms.ChoiceField(label="Pricing", widget=forms.RadioSelect, help_text="Every stream it reads is read in this pricing")
    formula = forms.CharField(
        label="Formula",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "spellcheck": "false", "autocomplete": "off", "class": "formula"}),
        help_text="(benefits) / (costs): stream names joined by +, adder(TaxCredits) for an adder, share x ProgramCosts for this "
        "test's own share of the program costs or share(TRC) x ProgramCosts for TRC's. When you change it, it decides the streams below.",
    )
    benefits = forms.MultipleChoiceField(label="Benefits", required=False, widget=forms.CheckboxSelectMultiple)
    numerator_adders = forms.MultipleChoiceField(
        label="Added to the benefits (adders)",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="In <T>Ben and the ratio, but not in the benefits the test's share of the program costs is divided by, nor in "
        "its Gross form: e.g. TRC's TaxCredits, RIM's bill increases",
    )
    costs = forms.MultipleChoiceField(label="Costs", required=False, widget=forms.CheckboxSelectMultiple)
    share = forms.ChoiceField(label="Program costs", widget=forms.RadioSelect, required=False)
    variants = forms.MultipleChoiceField(label="Variants", choices=VARIANTS, required=False, widget=forms.CheckboxSelectMultiple)
    lifecycle_net_benefit = forms.BooleanField(label="Write its lifecycle net benefit (<T>LifecycleNetBen)", required=False)
    levelized = forms.MultipleChoiceField(label="Levelized costs", choices=LEVELIZED, required=False, widget=forms.CheckboxSelectMultiple)
    headline = forms.BooleanField(
        label="A headline test",
        required=False,
        help_text="A tile on the run page and a line in the run email, its NoAdmin ratio in the portfolio summary, and the key metrics' "
        "drivers. The first headline test with its own share also gives the Weighted and Wtd columns their share. No ratio changes.",
    )
    filing = forms.BooleanField(label="Priced on filings", required=False, initial=True)
    definition = forms.CharField(label="Definition", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, pricings=(), streams=(), shares=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pricing"].choices = [(p, p) for p in pricings]
        for k in ("benefits", "numerator_adders", "costs"):
            self.fields[k].choices = _stream_choices(streams)
        self.fields["share"].choices = list(shares)
        self.stream_names = [n for n, _ in streams]
        self.share_tests = [v for v, _ in shares if v not in ("", "positive_benefits")]

    def clean(self):
        d = super().clean()
        text = (d.get("formula") or "").strip()
        if text and "formula" in self.changed_data:
            p = formula.parse(text, self.stream_names, self.share_tests)
            tid = (d.get("id") or "").strip()
            if p.test and tid and p.test != tid:
                p.problems.append(f"The formula is named {p.test} but the test's id is {tid}: change one so they match.")
            for m in p.problems:
                self.add_error("formula", m)
            if not p.problems:
                d.update(benefits=p.benefits, numerator_adders=p.numerator_adders, costs=p.costs, share=p.share)
        elif not self.has_error("formula"):
            for k, what in (("benefits", "benefit"), ("costs", "cost")):
                if not d.get(k) and not self.has_error(k):
                    self.add_error(k, f"A test needs at least one {what} stream: tick one, or write the formula.")
        return d

    @staticmethod
    def initial_for(tid: str, t: dict) -> dict:
        pc = t.get("program_costs")
        return {
            "id": tid,
            "label": t.get("label", ""),
            "pricing": t.get("pricing"),
            "formula": formula.render(tid, t),
            "benefits": list(t.get("benefits") or []),
            "numerator_adders": list(t.get("numerator_adders") or []),
            "costs": list(t.get("costs") or []),
            "share": pc.get("share", "") if isinstance(pc, dict) else "",
            "variants": list(t.get("variants") or []),
            "lifecycle_net_benefit": bool(t.get("lifecycle_net_benefit")),
            "levelized": list(t.get("levelized") or []),
            "headline": bool(t.get("headline")),
            "filing": bool(t.get("filing", True)),
            "definition": t.get("definition", ""),
        }


# --- problems in the form's words
#
# The engine names the part of the set a problem is about (`tests.TRC.costs: ...`, `label: text is required`). A person sees the form, so
# each problem is said with the form's own label ("Name: text is required"), linked to the field, and shown beside it (UI review 3).

PATH = re.compile(r"^(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*): (?P<text>.+)$", re.S)
# the header's keys that are not a form field of the same name
HEADER_KEYS = {"id": ("workstream", "Id"), "program_years": ("years_from", "Program years")}
# the keys a part's form calls something else
PART_KEYS = {"tests": {"program_costs": "share"}}
PART_WORDS = {"pricings": "Pricing", "streams": "Stream", "aggregates": "Aggregate", "tests": "Test"}


@dataclass
class Problem:
    """One problem with the set: the step it belongs to, the part and form field it is about (as the step's form calls it), and the
    engine's words for what is wrong."""

    step: str
    part: str = ""  # pricings, streams, aggregates, tests; "" for the set's header
    name: str = ""  # the pricing, stream, aggregate or test
    field: str = ""  # the form field ("" when none fits)
    label: str = ""  # that field's label
    text: str = ""

    @property
    def where(self) -> str:
        """Where the problem is, as the step's page says it: "Name", "Test TRC, Costs", "Stream NEBHealth"."""
        part = f"{PART_WORDS[self.part]} {self.name}" if self.part else ""
        return ", ".join(x for x in (part, self.label) if x)

    def __str__(self) -> str:
        return f"{self.where}: {self.text}" if self.where else self.text


def _form_for(step: str):
    return {"set": HeaderForm, "pricings": PricingForm, "inputs": InputStreamForm, "aggregates": AggregateForm, "tests": TestForm}.get(step)


def problem(message: str, data: dict) -> Problem:
    """A validator message as the form says it. A message that names no part of the set (an unknown top-level key, say) keeps its
    words and belongs to the set step."""
    step = builder.step_of(message, data)
    m = PATH.match(message.strip())
    if not m:
        return Problem(step=step, text=message)
    keys, text = m["path"].split("."), m["text"]
    if keys[0] not in PART_WORDS:  # the header
        field, label = HEADER_KEYS.get(keys[0], (keys[0], ""))
        if keys[0] == "program_years" and "to is before from" in text:
            field = "years_to"
        f = HeaderForm.base_fields.get(field)
        if f is None:
            return Problem(step=step, text=message)
        return Problem(step=step, field=field, label=label or str(f.label), text=text)
    if len(keys) < 2:
        return Problem(step=step, text=message)
    part, name, key = keys[0], keys[1], (keys[2] if len(keys) > 2 else "")
    field = PART_KEYS.get(part, {}).get(key, key)
    if step == "engine":
        label = {"pricings": "Read in the pricings", "unit": "Unit", "definition": "Definition"}.get(key, "")
        return Problem(step=step, part=part, name=name, field=key if label else "", label=label, text=text)
    form = _form_for(step)
    f = form.base_fields.get(field) if form and key else None
    if f is None or field in ("id", "remove"):
        return Problem(step=step, part=part, name=name, text=text if not key else f"{key}: {text}")
    return Problem(step=step, part=part, name=name, field=field, label=str(f.label), text=text)


def problems(build) -> dict[str, list[Problem]]:
    """The build's problems by step, in the form's words."""
    return {step: [problem(m, build.data) for m in msgs] for step, msgs in (build.problems or {}).items()}


def show_beside(form, found: list[Problem], *, part: str = "", name: str = "", fields=None) -> list[Problem]:
    """Show each problem about one of the form's fields beside that field, as the form's own errors are (bound or not), and return
    them. `part` and `name` pick the problems of one pricing, stream, aggregate or test; `fields` limits them to some of the form's
    fields and names the form field for each ({problem field: form field}; step 3's form has a field per stream)."""
    to = fields if fields is not None else {f: f for f in form.fields}
    mine = [p for p in found if p.part == part and p.name == name and to.get(p.field) in form.fields]
    if not mine:
        return []
    form.errors  # noqa: B018 - makes the error list (an unbound form has an empty one)
    if not hasattr(form, "cleaned_data"):
        form.cleaned_data = {}
    for p in mine:
        if p.text not in form.errors.get(to[p.field], []):
            form.add_error(to[p.field], p.text)
    return mine


class OfferForm(forms.Form):
    note = forms.CharField(
        label="A note for the CPUC (optional)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="What the set is for and why everyone could use it",
    )


class DeclineForm(forms.Form):
    reason = forms.CharField(
        label="Why it is declined", widget=forms.Textarea(attrs={"rows": 2}), help_text="Sent to the person who offered it"
    )
