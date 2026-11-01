from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "cedars.services.accounts"
    label = "accounts"
    verbose_name = "Identity and access"

    def ready(self):
        from cedars.core import queue as my_queue

        from . import queue, signals  # noqa: F401  (signals: the second-factor set-up flow's receivers)

        # each user's queue (BR-09.5): access requests and data stewards' decisions
        my_queue.register("steward", "Data steward decisions", queue.steward_decisions, action="Decide", order=30)
        my_queue.register("access", "Access requests to decide", queue.access_requests, action="Decide", order=40)
