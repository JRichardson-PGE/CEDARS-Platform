"""The form field template (cedars/core/templates/django/forms/field.html; UI review 2, 2026-10-31, batch B): every field rendered with
as_field_group is one .field block in the order label, control, errors, hint; the label and legend carry no colon; a checkbox is one line
with its box inside the label; the control's aria-describedby names the hint."""

import re

from django import forms


class Sample(forms.Form):
    name = forms.CharField(label="Name", help_text="As filed")
    shared = forms.BooleanField(label="Offer it to everyone", required=False, help_text="Everyone can pick it")
    modules = forms.MultipleChoiceField(
        label="Modules", choices=[("a", "A"), ("b", "B")], widget=forms.CheckboxSelectMultiple, help_text="Tick any"
    )


def order(html, *parts):
    found = [html.find(p) for p in parts]
    assert all(i >= 0 for i in found), (parts, html)
    return found == sorted(found)


def test_text_field_is_label_control_hint_without_a_colon():
    html = Sample(auto_id="t_%s")["name"].as_field_group()
    assert '<label for="t_name">Name</label>' in html
    assert "Name:" not in html
    assert order(html, "<label", "<input", 'class="helptext"')
    assert 'aria-describedby="t_name_helptext"' in html and 'id="t_name_helptext"' in html


def test_errors_sit_between_the_control_and_the_hint():
    html = Sample(data={}, auto_id="t_%s")["name"].as_field_group()
    assert "has-errors" in html
    assert order(html, "<input", "errorlist", 'class="helptext"')


def test_checkbox_is_one_line_inside_its_label():
    html = Sample(auto_id="t_%s")["shared"].as_field_group()
    assert re.search(r'<label class="check" for="t_shared">\s*<input[^>]*type="checkbox"[^>]*>\s*Offer it to everyone\s*</label>', html), (
        html
    )
    assert order(html, "</label>", 'class="helptext"')


def test_fieldset_legend_without_a_colon_and_its_hint_last():
    html = Sample(auto_id="t_%s")["modules"].as_field_group()
    assert "<legend>Modules</legend>" in html
    assert "Modules:" not in html
    assert order(html, "<legend>", 'type="checkbox"', 'class="helptext"')
    assert 'aria-describedby="t_modules_helptext"' in html
