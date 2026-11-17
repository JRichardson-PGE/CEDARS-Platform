"""The platform's wording on django-allauth's forms (UAT F12); their checks and flows are allauth's (ACCOUNT_FORMS)."""

from allauth.account import forms as allauth_forms


class LoginForm(allauth_forms.LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["login"].label = "Email address"
        if "remember" in self.fields:
            self.fields["remember"].label = "Keep me signed in on this device"


class SignupForm(allauth_forms.SignupForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].label = "Email address"
        if "password2" in self.fields:
            self.fields["password2"].label = "Password, again"


class ResetPasswordForm(allauth_forms.ResetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].label = "Email address"
