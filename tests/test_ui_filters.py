"""The shared component filters (cedars.core.templatetags.ui): status tones and due-date words."""

from datetime import UTC, datetime, timedelta

from cedars.core.templatetags.ui import due_words, status_tone

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def test_status_tone_maps_submission_statuses():
    assert status_tone("published") == "ok"
    assert status_tone("confirmed") == "ok"
    assert status_tone("validated") == "info"
    assert status_tone("draft") == "warn"
    assert status_tone("rejected") == "bad"
    assert status_tone("Not started") == ""
    assert status_tone(None) == ""


def test_due_words():
    assert due_words(NOW + timedelta(days=12), NOW) == "Due in 1\xa0week"
    assert due_words(NOW + timedelta(hours=3), NOW) == "Due today"
    assert due_words(NOW - timedelta(days=220), NOW) == "Passed 7\xa0months ago"
    assert due_words(None, NOW) == ""


def test_date_words_and_datetime_words():
    from zoneinfo import ZoneInfo

    from cedars.core.templatetags.ui import date_words, datetime_words

    when = datetime(2026, 10, 31, 14, 56, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert date_words(when) == "2026-10-31"
    assert datetime_words(when) == "2026-10-31 14:56 PDT"
    assert date_words(when.date()) == "2026-10-31"
    assert date_words(None) == "" and datetime_words("") == ""


def test_cycle_label():
    from cedars.core.templatetags.ui import cycle_label

    class Cycle:
        def __init__(self, a, b):
            self.first_year, self.last_year = a, b

    assert cycle_label(Cycle(2024, 2027)) == "2024-2027"
    assert cycle_label(Cycle(2021, 2021)) == "2021"
    assert cycle_label("2028-2031") == "2028-2031" and cycle_label("2021-2021") == "2021"
    assert cycle_label(None) == ""


def test_person_and_person_name():
    from cedars.core.templatetags.ui import person, person_name

    class User:
        def __init__(self, name, email):
            self.name, self.email = name, email

    assert person_name(User("Dana Lee", "dana@example.org")) == "Dana Lee"
    assert person_name(User("", "dana@example.org")) == "dana@example.org"
    assert person_name(None) == ""
    assert person(User("Dana <Lee>", "dana@example.org")) == 'Dana &lt;Lee&gt; <span class="small muted">dana@example.org</span>'
    assert person(User("", "dana@example.org")) == "dana@example.org"
    assert person(None) == ""


def test_templates_use_the_shared_filters():
    """No template writes a date, a person or a cycle its own way (DESIGN.md): dates go through date_words / datetime_words, people
    through person / person_name, so the whole site changes in one place."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "cedars"
    own_dates, emails = [], []
    for path in root.rglob("templates/**/*.html"):
        for line in path.read_text().splitlines():
            if re.search(r'\|date:"[^"]*[YmdHi][^"]*"', line):  # a month name (date:"F") is fine; datetime= attributes use 'c'
                own_dates.append(f"{path.relative_to(root)}: {line.strip()[:80]}")
            allowed = any(
                x in line for x in ("email_address.email", "user.email }}</dd>", "support.email")
            )  # not a person: a confirmation, the account's own address, the support mailbox
            if re.search(r"\{\{ [\w.]+\.email(\|[^}]*)? \}\}", line) and not allowed:
                emails.append(f"{path.relative_to(root)}: {line.strip()[:80]}")
    assert not own_dates, own_dates
    assert not emails, emails
