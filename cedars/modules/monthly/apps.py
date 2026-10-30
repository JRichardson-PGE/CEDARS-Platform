from django.apps import AppConfig


class MonthlyConfig(AppConfig):
    """The Monthly reports module (phase 5, BR-14): each PA's report for each month of a program year, a submission with versions on the
    submissions service. A report holds the month's expenditures, its commitments (a separate file, D2) and the year-to-date BCA inputs,
    which are priced through the submissions service for estimated, unvalidated savings and TSB (BR-14.5); each program is compared with
    its operating budget from Budgets (BR-04.7). Fund shifts are made in Budgets, not here (BR-14.4)."""

    name = "cedars.modules.monthly"
    label = "monthly"
    verbose_name = "Monthly reports"

    def ready(self):
        from cedars.services.submissions.service import register_publisher

        from . import service

        register_publisher("monthly", service.on_publish, service.on_unpublish)
