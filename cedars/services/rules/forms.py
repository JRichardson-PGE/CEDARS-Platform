from django import forms

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


class RuleForm(forms.Form):
    """A rule's test and wording. Both kinds of test are on the form (it works without scripts); the chosen kind decides which part
    is read."""

    kind = forms.ChoiceField(
        label="Kind of rule",
        widget=forms.RadioSelect,
        choices=[
            ("rule", "A test on a record: when some fields hold, another must pass a test"),
            ("spec", "A constraint on one field: filled in, a number, a code of a value list ..."),
        ],
        initial="rule",
    )
    severity = forms.ChoiceField(label="When the record fails", widget=forms.RadioSelect, choices=Rule.Severity.choices, initial="reject")
    table = forms.ChoiceField(label="Table", choices=[(t, t) for t in service.TABLES])
    field = forms.CharField(label="Field tested", max_length=200, help_text="Its canonical name or any name the data dictionary lists.")

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

    def __init__(self, *args, rule: Rule | None = None, **kwargs):
        self.original = rule
        if rule is not None and "initial" not in kwargs:
            kwargs["initial"] = initial_for(rule)
        super().__init__(*args, **kwargs)
        for i in range(1, CONDITIONS + 1):
            self.fields[f"cond{i}_field"] = forms.CharField(label=f"Condition {i}: field", required=False, max_length=200)
            self.fields[f"cond{i}_rule"] = forms.ChoiceField(
                label=f"Condition {i}: test", choices=[("", "Choose")] + service.OPERATORS, required=False
            )
            self.fields[f"cond{i}_value"] = forms.CharField(label=f"Condition {i}: value", required=False, max_length=500)
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

    def conditions(self) -> list[int]:
        return list(range(1, CONDITIONS + 1))

    def condition_fields(self):
        return [(i, self[f"cond{i}_field"], self[f"cond{i}_rule"], self[f"cond{i}_value"]) for i in self.conditions()]

    def clean(self):
        d = super().clean()
        if self.errors:
            return d
        kind, field = d["kind"], d["field"].strip()
        try:
            if kind == "rule":
                if not d.get("rule"):
                    raise service.RuleError("Choose the test.")
                conds = [(d.get(f"cond{i}_field", ""), d.get(f"cond{i}_rule", ""), d.get(f"cond{i}_value", "")) for i in self.conditions()]
                for f, r, _ in conds:
                    if f.strip() and not r:
                        raise service.RuleError(f"Choose the test for the condition on {f.strip()}.")
                definition = service.rule_definition(field, d["rule"], d.get("value", ""), conds, d.get("connector") or "all", d["table"])
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
            service.validate(kind, d["severity"], d["table"], field, definition)
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
        out |= {"rule": d.get("rule", ""), "value": d.get("value", ""), "connector": d.get("connector", "all")}
        for i, c in enumerate(d.get("conds", [])[:CONDITIONS], start=1):
            out |= {f"cond{i}_field": c[1], f"cond{i}_rule": c[2], f"cond{i}_value": c[3]}
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
