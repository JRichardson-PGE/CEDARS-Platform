from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "cedars.services.accounts"
    label = "accounts"
    verbose_name = "Identity and access"

    def ready(self):
        from . import signals  # noqa: F401  (the second-factor set-up flow's receivers)
