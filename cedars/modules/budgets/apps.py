from django.apps import AppConfig


class BudgetsConfig(AppConfig):
    """The Budgets module (phase 3, BR-04): budget filings for a four-year filing cycle (business plan, TUAL, MCAL), each a submission
    with versions on the submissions service, priced through it before confirmation; fund shifts follow. Every filed version is kept
    and comparable, excluded programs are filtered and never zeroed, and the confirmed inputs and run settings are published."""

    name = "cedars.modules.budgets"
    label = "budgets"
    verbose_name = "Budgets"

    def ready(self):
        from cedars.services.registry.service import register_operating_budget
        from cedars.services.submissions.service import register_publisher

        from . import service, shifts

        register_publisher("budgets", service.on_publish, service.on_unpublish)
        register_operating_budget(shifts.operating_rows)  # what monthly reports, claims and dashboards compare against (BR-04.7)
