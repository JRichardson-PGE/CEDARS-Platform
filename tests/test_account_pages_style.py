"""The sign-in library's own pages look like the rest of the site: sentence-case headings, labels and buttons, forms in the narrow
form column, and a second-factor page whose parts are cards with buttons in a row rather than bullet points."""

from cedars.core.templatetags.ui import sentence_case
from tests.conftest import sign_in


def test_sentence_case_keeps_the_first_capital_and_words_in_capitals():
    assert sentence_case("Change Password") == "Change password"
    assert sentence_case("\n   New Password (again)\n ") == "New password (again)"
    assert sentence_case("Your CEDARS PII Settings") == "Your CEDARS PII settings"
    assert sentence_case("Two-factor authentication") == "Two-factor authentication"


def test_change_password_is_a_narrow_form_in_sentence_case(client, make_user):
    sign_in(client, make_user("someone@example.org", "Some One"))
    html = client.get("/accounts/password/change/").content.decode()
    assert "<title>Change password" in html
    assert 'class="stack form-narrow"' in html
    assert ">Change password</h1>" in html
    assert "Change Password" not in html
    assert "New password (again)" in html


def test_the_second_factor_page_shows_cards_with_buttons_not_a_list(client, make_user):
    sign_in(client, make_user("someone@example.org", "Some One"))
    html = client.get("/accounts/2fa/").content.decode()
    assert html.count('class="card account-panel"') == 3
    assert "<li><a class=\"button" not in html
