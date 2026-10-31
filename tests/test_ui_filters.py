"""The shared component filters (cedars.core.templatetags.ui): status tones and due-date words."""

from datetime import datetime, timedelta, timezone

from cedars.core.templatetags.ui import due_words, status_tone

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


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
