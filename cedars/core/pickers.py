"""Pick, don't type: the shared pickers (UI review 3, 2026-11-11). A program, a data dictionary field or a member of an organization is
chosen from a list that narrows as you type, not typed from memory or pasted from an address.

Each picker is a text box with a native <datalist>, so the list narrows as you type with no script, and what was typed is read on the
server: the value itself in any case, or text naming exactly one choice (a program's name, a member's name). Text that names none
gets a sentence naming the near matches, so a typo is never silently dropped or silently filtered to nothing.

- `ProgramField`, `FieldField`, `MemberField`: Django form fields (one choice), drawn by `core/widgets/picker.html`.
- `program_picker()` with `core/_program_picker.html`: several programs at once for a filter (the records page and the dashboards).
  The programs chosen are ticked boxes (untick one to drop it) and one box adds another; `core/pickers.js` adds the program to the
  ticked ones as soon as it is picked from the list, and without scripts it is added when the form is sent.
- `resolve_programs()`: what a filter's program values name, as canonical PrgIDs (values that name no program are kept as typed, so
  the page can say they match nothing).

The choices come from the services (the program registry, the data dictionary, accounts); the pickers import no module.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from django import forms

NEAR = 3  # near matches named when nothing fits


@dataclass(frozen=True)
class Choice:
    value: str  # what the form sends: a PrgID, a field name, an email address
    label: str = ""  # shown beside it: the program's or the person's name, the field's kind and group
    note: str = ""  # a word more: the PA and whether the program is closed


# --- the choices, from the services


def program_choices(*, pa=None) -> list[Choice]:
    """Every program in the registry (not portfolio-cost IDs), with its name in force today; with `pa` (an organization or its code),
    that PA's own programs only. Statewide programs are listed under SW."""
    from cedars.services.registry import service as registry

    code = getattr(pa, "code", pa)
    out = []
    for p, v in registry.catalog(None):
        pa_code = registry.pa_code(p)
        if code and pa_code != code:
            continue
        closed = v is not None and v.status == "closed"
        out.append(Choice(p.prg_id, v.name if v else "", f"{pa_code}{', closed' if closed else ''}"))
    return out


def field_choices(*, kinds=None) -> list[Choice]:
    """The current data dictionary's fields in use (retired ones left out), with their kind and group; `kinds` limits them to
    input, output or record fields."""
    from django.utils import timezone

    from cedars.services.dictionary import service as dictionary
    from cedars.services.dictionary.models import Field

    v = dictionary.current()
    if v is None:
        return []
    qs = Field.objects.filter(version=v).exclude(state=Field.State.RETIRED, retired_from__lte=timezone.localdate()).order_by("name")
    if kinds:
        qs = qs.filter(kind__in=kinds)
    return [Choice(f.name, f.group, f.get_kind_display()) for f in qs]


def member_choices(organisation) -> list[Choice]:
    """The organization's current members, by email address, with their names."""
    from cedars.services.accounts.models import Membership

    members = Membership.objects.filter(organisation=organisation, ended_at__isnull=True).select_related("user").order_by("user__email")
    return [Choice(m.user.email, m.user.name or "") for m in members]


# --- reading what was typed


def find(text: str, choices: list[Choice]) -> Choice | None:
    """The choice the text names: its value in any case; or the value followed by its label, as a browser may fill it in; or a label
    (in any case) that only one choice has."""
    t = (text or "").strip()
    if not t:
        return None
    key = t.casefold()
    by_value = {c.value.casefold(): c for c in choices}
    if key in by_value:
        return by_value[key]
    first = key.split()[0]
    if first in by_value and (rest := key[len(first) :].strip(" -:·")) and rest == by_value[first].label.casefold():
        return by_value[first]
    named = [c for c in choices if c.label and c.label.casefold() == key]
    return named[0] if len(named) == 1 else None


def near_matches(text: str, choices: list[Choice], n: int = NEAR) -> list[Choice]:
    """The choices closest to text that names none: those whose value or label holds it, else the values most like it."""
    key = (text or "").strip().casefold()
    if not key:
        return []
    holding = [c for c in choices if key in c.value.casefold() or (c.label and key in c.label.casefold())]
    if holding:
        return holding[:n]
    values = {c.value.casefold(): c for c in choices}
    return [values[v] for v in difflib.get_close_matches(key, list(values), n=n, cutoff=0.6)]


def _named(c: Choice) -> str:
    return f"{c.value} ({c.label})" if c.label else c.value


def not_found(text: str, choices: list[Choice], noun: str) -> str:
    """The sentence for text that names no choice: what it is not, and the near matches."""
    near = near_matches(text, choices)
    said = f"No {noun} is “{text.strip()}”."
    if not near:
        return f"{said} Pick one from the list as you type."
    names = [_named(c) for c in near]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return f"{said} Did you mean {listed}?"


SPLIT = re.compile(r"[,\n]+")


def split(values) -> list[str]:
    """Values given one to a box or several to a box separated by commas or new lines (old links and pasted lists), in order, once."""
    out = []
    for v in values or []:
        for part in SPLIT.split(v or ""):
            part = part.strip()
            if part and part.casefold() not in {o.casefold() for o in out}:
                out.append(part)
    return out


def resolve_programs(values, choices: list[Choice] | None = None) -> list[str]:
    """A filter's program values as canonical PrgIDs: each value, or each of a list of values separated by commas, is the PrgID it
    names (`find`); several PrgIDs separated by spaces are each read; a value that names no program is kept as typed, so the page can
    say it matches nothing."""
    parts = split(values)
    if not parts:
        return []
    choices = program_choices() if choices is None else choices
    out = []
    for part in parts:
        c = find(part, choices)
        if c is None and " " in part and all(find(w, choices) for w in part.split()):
            out += [find(w, choices).value for w in part.split()]
            continue
        out.append(c.value if c else part)
    return list(dict.fromkeys(out))


# --- several programs at once, for a filter


@dataclass
class ProgramPicker:
    name: str  # the GET or POST parameter
    label: str
    id: str
    chosen: list[Choice] = field(default_factory=list)  # the programs chosen, the unknown ones with no label
    choices: list[Choice] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # a sentence for each value that names no program
    help: str = "Type a PrgID or part of a program's name, then pick it from the list."

    @property
    def summary(self) -> str:
        n = len(self.chosen)
        return "Every program" if not n else f"{n} program{'s' if n != 1 else ''} chosen"


def program_picker(values, *, name: str = "program", label: str = "Program", id: str = "program-pick", pa=None) -> ProgramPicker:
    """The multi-program picker for a filter's values (`core/_program_picker.html`)."""
    choices = program_choices(pa=pa)
    by_value = {c.value.casefold(): c for c in choices}
    chosen, problems = [], []
    for v in resolve_programs(values, choices):
        c = by_value.get(v.casefold())
        if c is None:
            from cedars.services.registry.models import Program

            # a portfolio-cost ID is in the registry and in the records, but not a program to pick
            if not Program.objects.filter(prg_id__iexact=v).exists():
                problems.append(not_found(v, choices, "program"))
            c = Choice(v)
        chosen.append(c)
    return ProgramPicker(name, label, id, chosen, choices, problems)


# --- one choice, in a Django form


class PickerInput(forms.TextInput):
    """A text box with the choices as a native <datalist> (`core/widgets/picker.html`). `choices` is a list of Choice or a callable
    returning one, read when the form is drawn."""

    template_name = "core/widgets/picker.html"

    def __init__(self, choices=(), attrs=None):
        super().__init__({"autocomplete": "off", "spellcheck": "false", "class": "picker-input", **(attrs or {})})
        self.choices = choices

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        choices = self.choices() if callable(self.choices) else self.choices
        list_id = f"{context['widget']['attrs'].get('id') or name}-list"
        context["widget"]["attrs"]["list"] = list_id
        context["widget"] |= {"list_id": list_id, "choices": choices}
        return context


class PickerField(forms.CharField):
    """One choice from a picker. Cleans to the choice's value in its own case (`find`), or fails naming the near matches."""

    noun = "choice"

    def __init__(self, *, choices=None, **kwargs):
        self._choices = choices  # None: default_choices()
        kwargs.setdefault("help_text", self.default_help)
        super().__init__(widget=PickerInput(self.choice_list), **kwargs)

    default_help = "Type to narrow the list, then pick one."

    def default_choices(self) -> list[Choice]:
        return []

    def choice_list(self) -> list[Choice]:
        if not hasattr(self, "_cache"):
            source = self.default_choices if self._choices is None else self._choices
            self._cache = source() if callable(source) else list(source)
        return self._cache

    def set_choices(self, choices) -> None:
        """Change the choices after the form is made (a form that knows its PA or organization only in __init__)."""
        self._choices = choices
        self.__dict__.pop("_cache", None)

    def __deepcopy__(self, memo):
        result = super().__deepcopy__(memo)
        result.__dict__.pop("_cache", None)
        result.widget.choices = result.choice_list
        return result

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return value
        found = find(value, self.choice_list()) or self.lookup(value)
        if found is None:
            raise forms.ValidationError(not_found(value, self.choice_list(), self.noun), code="not_found")
        return found.value

    def lookup(self, value) -> Choice | None:
        """A choice the text names another way (a field's other names); none by default."""
        return None


class ProgramField(PickerField):
    """A program from the registry, by PrgID (or its name); with `pa`, that PA's programs only."""

    noun = "program"
    default_help = "Type a PrgID or part of a program's name, then pick it from the list."

    def __init__(self, *, pa=None, **kwargs):
        self.pa = pa
        super().__init__(**kwargs)

    def default_choices(self):
        return program_choices(pa=self.pa)


class FieldField(PickerField):
    """A field of the current data dictionary, by its name; `kinds` limits it to input, output or record fields."""

    noun = "field"
    default_help = "Type part of the field's name, then pick it from the list."

    def __init__(self, *, kinds=None, **kwargs):
        self.kinds = kinds
        super().__init__(**kwargs)

    def default_choices(self):
        return field_choices(kinds=self.kinds)

    def lookup(self, value):
        """A field by another of its names (a CET or E3 name), as uploads read it."""
        from cedars.services.dictionary import service as dictionary

        f = dictionary.resolve(value)
        return next((c for c in self.choice_list() if f is not None and c.value == f.name), None)


class MemberField(PickerField):
    """A current member of an organization, by email address (or name). Give the organization with `set_organisation` (or
    `organisation=`) before the form is drawn."""

    noun = "member"
    default_help = "Type part of the person's name or email address, then pick them from the list."

    def __init__(self, *, organisation=None, **kwargs):
        self.organisation = organisation
        super().__init__(**kwargs)

    def default_choices(self):
        return member_choices(self.organisation) if self.organisation is not None else []

    def set_organisation(self, organisation) -> None:
        self.organisation = organisation
        self.set_choices(None)
