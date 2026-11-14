from django import forms

from cedars.core import pickers

from . import service
from .models import Rule

CONDITIONS = 5
MODULES = [("claims", "Claims"), ("budgets", "Budgets"), ("monthly", "Monthly reports"), ("calculations", "Calculations")]


class NewVersionForm(forms.Form):
    effective_from = forms.DateField(label="Takes effect on", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Why the rules change", help_text="For example, the decision or the CEDARS notice that changes them.")


def _value_list_choices(extra: str = "") -> list[tuple[str, str]]:
    from cedars.services.dictionary import value_lists

    try:
        names = value_lists.engine_lists()
    except value_lists.EngineMissing:
        names = []
    choices = [("", "Choose a list")] + [(n, n) for n in names]
    if extra and extra not in names:
        choices.append((extra, extra))
    if "PrgID" != extra:
        choices.append(("PrgID", "PrgID (the program registry)"))
    return choices


class RuleField(pickers.FieldField):
    """A field a rule names, picked from the data dictionary's input and record fields as you type. Any name the dictionary lists is
    taken too (CET's or E3's), and a key of several fields joined by +. A name the dictionary knows is kept as given, so saving a
    rule's wording never changes its test (and so its ID); text that names no field gets the near matches."""

    def __init__(self, also=(), **kwargs):
        self.also = set(also)
        super().__init__(kinds=["input", "record"], max_length=200, **kwargs)

    def clean(self, value):
        value = (forms.CharField.clean(self, value) or "").strip()
        if not value:
            return value
        if self.also and value in self.also:  # a field of the program record or of who files, which a condition may read
            return value
        out = []
        for part in (p.strip() for p in value.split("+")):
            if not part or service.known_field(part):
                out.append(part)
                continue
            found = pickers.find(part, self.choice_list())
            if found is None:
                raise forms.ValidationError(pickers.not_found(part, self.choice_list(), self.noun), code="not_found")
            out.append(found.value)
        return "+".join(out)


class RuleForm(forms.Form):
    """A rule's test and wording. Both kinds of test are on the form (it works without scripts); the chosen kind decides which part
    is read."""

    kind = forms.ChoiceField(
        label="Kind of rule",
        widget=forms.RadioSelect(attrs={"data-reveal": ""}),
        choices=[
            ("rule", "A test on a record: when some fields hold, another must pass a test"),
            ("spec", "A constraint on one field: filled in, a number, a code of a value list ..."),
        ],
        initial="rule",
    )
    severity = forms.ChoiceField(label="When the record fails", widget=forms.RadioSelect, choices=Rule.Severity.choices, initial="reject")
    table = forms.ChoiceField(label="Table", choices=[(t, t) for t in service.TABLES])
    field = RuleField(
        label="Field tested",
        help_text="Type part of its name, then pick it from the list. Any name the data dictionary lists works, such as CET's; join a "
        "key's fields with +.",
    )

    # a test on a record
    rule = forms.ChoiceField(label="Test", choices=service.OPERATORS, required=False)
    value = forms.CharField(
        label="Value",
        required=False,
        max_length=500,
        help_text="A number, a quoted text such as 'AR', another field's name, or for a list several values separated by commas.",
    )
    connector = forms.ChoiceField(
        label="Apply the test when", choices=[("all", "every condition holds"), ("any", "any condition holds")], required=False
    )
    effect_list = forms.ChoiceField(
        label="Value list the code is on",
        required=False,
        help_text='For "is a code in effect on the date in field": the list whose codes\' first and last years are read. Blank: the list '
        "named after the field.",
    )
    group1_connector = forms.ChoiceField(
        label="Group 1 holds when", choices=[("all", "every condition in it holds"), ("any", "any condition in it holds")], required=False
    )
    group2_connector = forms.ChoiceField(
        label="Group 2 holds when", choices=[("all", "every condition in it holds"), ("any", "any condition in it holds")], required=False
    )

    # a constraint on one field
    check = forms.ChoiceField(label="The field must be", choices=[("", "Choose")] + service.CHECKS, required=False)
    n = forms.IntegerField(label="Largest number of characters", required=False, min_value=1)
    value_list = forms.ChoiceField(label="Value list", required=False)
    lo = forms.CharField(label="Lowest value", required=False, max_length=40)
    lo_open = forms.BooleanField(label="The lowest value itself is not allowed", required=False)
    hi = forms.CharField(label="Highest value", required=False, max_length=40)
    hi_open = forms.BooleanField(label="The highest value itself is not allowed", required=False)

    # wording
    message = forms.CharField(
        label="Message a filer sees",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Leave blank to write it from the test.",
    )
    fix = forms.CharField(
        label="Suggested fix", required=False, widget=forms.Textarea(attrs={"rows": 2}), help_text="Leave blank to write it from the test."
    )
    difference = forms.CharField(label="Why it differs between modules (if it deliberately does)", required=False)
    modules = forms.MultipleChoiceField(label="Modules that apply it", choices=MODULES, widget=forms.CheckboxSelectMultiple, required=False)
    reason = forms.CharField(label="Why you are making this change", help_text="Kept in the audit trail.")

    def __init__(self, *args, rule: Rule | None = None, rule_set=None, **kwargs):
        self.original = rule
        self.rule_set = rule_set if rule_set is not None else (rule.rule_set if rule is not None else None)
        if rule is not None and "initial" not in kwargs:
            kwargs["initial"] = initial_for(rule)
        super().__init__(*args, **kwargs)
        tables = service.tables_for(self.rule_set)
        self.fields["table"].choices = [(t, t) for t in tables]
        where = (
            [("", "The record itself")]
            + [(t, f"Its {t} record") for t in tables]
            + [
                (service.PROGRAM, "Its program (the registry's record)"),
                (service.FILER, "Who files"),
            ]
        )
        also = set(service.PROGRAM_FIELDS) | set(service.FILER_FIELDS)
        for i in range(1, CONDITIONS + 1):
            self.fields[f"cond{i}_table"] = forms.ChoiceField(label=f"Condition {i}: what it reads", choices=where, required=False)
            self.fields[f"cond{i}_field"] = RuleField(label=f"Condition {i}: field", required=False, help_text="", also=also)
            self.fields[f"cond{i}_rule"] = forms.ChoiceField(
                label=f"Condition {i}: test", choices=[("", "Choose")] + service.OPERATORS, required=False
            )
            self.fields[f"cond{i}_value"] = forms.CharField(label=f"Condition {i}: value", required=False, max_length=500)
            self.fields[f"cond{i}_group"] = forms.ChoiceField(
                label=f"Condition {i}: group",
                choices=[("", "Not in a group")] + [(g, f"Group {g}") for g in service.GROUPS],
                required=False,
            )
        init = self.initial
        # a copied CEDARS rule may spell a test or a check the form does not offer: keep it, so saving the wording keeps the rule
        for name in ["rule"] + [f"cond{i}_rule" for i in range(1, CONDITIONS + 1)]:
            v = init.get(name)
            if v and v not in dict(self.fields[name].choices):
                self.fields[name].choices = list(self.fields[name].choices) + [(v, v)]
        if init.get("check") and init["check"] not in dict(self.fields["check"].choices):
            self.fields["check"].choices = list(self.fields["check"].choices) + [
                (init["check"], service.ALSO_CHECKED.get(init["check"], init["check"]))
            ]
        self.fields["value_list"].choices = _value_list_choices(init.get("value_list", ""))
        self.fields["effect_list"].choices = [("", "The list named after the field")] + _value_list_choices(init.get("effect_list", ""))[1:]

    def conditions(self) -> list[int]:
        return list(range(1, CONDITIONS + 1))

    def condition_fields(self):
        return [
            (i, self[f"cond{i}_table"], self[f"cond{i}_field"], self[f"cond{i}_rule"], self[f"cond{i}_value"], self[f"cond{i}_group"])
            for i in self.conditions()
        ]

    def program_fields(self):
        return service.PROGRAM_FIELDS.items()

    def filer_fields(self):
        return service.FILER_FIELDS.items()

    def first_condition(self):
        return self.condition_fields()[0]

    def more_conditions(self):
        """The second to the last condition, folded under More conditions."""
        return self.condition_fields()[1:]

    def more_conditions_used(self) -> int:
        """How many of the folded conditions are filled in (or have an error), so the fold opens on them."""
        used = 0
        for _, tb, f, t, v, g in self.more_conditions():
            if any(b.errors or (b.value() or "").strip() for b in (tb, f, t, v, g)):
                used += 1
        return used

    def rule_part(self) -> list[str]:
        return ["rule", "value", "connector", "effect_list", "group1_connector", "group2_connector"] + [
            f"cond{i}_{x}" for i in self.conditions() for x in ("table", "field", "rule", "value", "group")
        ]

    SPEC_PART = ["check", "n", "value_list", "lo", "lo_open", "hi", "hi_open"]

    def clean(self):
        d = super().clean()
        # only the chosen kind's part is read, so what is left in the other part (hidden by the page) never stops the save
        other = self.SPEC_PART if d.get("kind") == "rule" else self.rule_part() if d.get("kind") == "spec" else []
        for name in other:
            self._errors.pop(name, None)
        if self.errors:
            return d
        kind, field = d["kind"], d["field"].strip()
        try:
            if kind == "rule":
                if not d.get("rule"):
                    raise service.RuleError("Choose the test.")
                conds = [
                    (
                        d.get(f"cond{i}_table", "") or d["table"],
                        d.get(f"cond{i}_field", ""),
                        d.get(f"cond{i}_rule", ""),
                        d.get(f"cond{i}_value", ""),
                        d.get(f"cond{i}_group", ""),
                    )
                    for i in self.conditions()
                ]
                for _tb, f, r, _v, _g in conds:
                    if f.strip() and not r:
                        raise service.RuleError(f"Choose the test for the condition on {f.strip()}.")
                definition = service.rule_definition(
                    field,
                    d["rule"],
                    d.get("value", ""),
                    conds,
                    d.get("connector") or "all",
                    d["table"],
                    groups={"1": d.get("group1_connector") or "all", "2": d.get("group2_connector") or "all"},
                    value_list=d.get("effect_list", ""),
                )
            else:
                check = d.get("check")
                if not check:
                    raise service.RuleError("Choose what the field must be.")
                o = self.original
                if o is not None and o.kind == "spec" and check == o.definition.get("check") and check in service.ALSO_CHECKED:
                    definition = dict(o.definition)
                else:
                    definition = service.check_definition(
                        check,
                        n=d.get("n"),
                        list=d.get("value_list"),
                        lo=d.get("lo"),
                        hi=d.get("hi"),
                        lo_open=d.get("lo_open"),
                        hi_open=d.get("hi_open"),
                    )
            service.validate(kind, d["severity"], d["table"], field, definition, self.rule_set)
        except service.RuleError as e:
            raise forms.ValidationError(str(e)) from e
        d["definition"] = definition
        d["field"] = field
        return d


def initial_for(rule: Rule) -> dict:
    d = rule.definition or {}
    out = {
        "kind": rule.kind,
        "severity": rule.severity,
        "table": rule.table,
        "field": rule.field,
        "message": rule.message,
        "fix": rule.fix,
        "difference": rule.difference,
        "modules": rule.modules,
    }
    if rule.kind == "rule":
        out |= {
            "rule": d.get("rule", ""),
            "value": d.get("value", ""),
            "connector": d.get("connector", "all"),
            "effect_list": d.get("list", ""),
        }
        flat, n = [], 0
        for c in d.get("conds", []):
            if isinstance(c, dict):
                n += 1
                out[f"group{n}_connector"] = c.get("connector", "all")
                flat += [(x, str(n)) for x in c.get("conds") or []]
            else:
                flat.append((c, ""))
        for i, (c, g) in enumerate(flat[:CONDITIONS], start=1):
            own = not str(c[0]).strip() or str(c[0]).strip() == rule.table
            out |= {f"cond{i}_table": "" if own else c[0], f"cond{i}_field": c[1], f"cond{i}_rule": c[2], f"cond{i}_value": c[3],
                    f"cond{i}_group": g}  # fmt: skip
    else:
        out |= {
            "check": d.get("check", ""),
            "n": d.get("n"),
            "value_list": d.get("list", ""),
            "lo": _num(d.get("lo")),
            "hi": _num(d.get("hi")),
        }
        out |= {"lo_open": d.get("lo_open", False), "hi_open": d.get("hi_open", False)}
    return out


def _num(x) -> str:
    return "" if x is None else repr(x)
