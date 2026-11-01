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
        from cedars.services.submissions.service import register_deadline_page, register_publisher

        from . import service

        register_publisher("monthly", service.on_publish, service.on_unpublish)
        register_deadline_page("monthly", service.deadline_page)  # the filing calendar links each of its deadlines

        from cedars.services.registry.service import register_reported_spend

        register_reported_spend(service.reported_spend)  # what the budget-and-spending dashboard shows (BR-07.3)

        from cedars.services.notifications.service import register_filing

        register_filing("monthly", service.notice)  # what subscribers and the programs' followers are told (BR-09.2, 09.3)

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("monthly", program_page.reports)  # each program's page lists its monthly reports (BR-07.6)

        from cedars.services.submissions import corrections as record_corrections

        from .corrections import MonthlyEditor

        record_corrections.register(MonthlyEditor())  # records corrected in place on an open draft (BR-02.4)
