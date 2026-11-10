from django.apps import AppConfig


class MonthlyConfig(AppConfig):
    """The Monthly reports module (phase 5): each PA's report for each month of a program year, a submission with versions on the
    submissions service. A report holds the month's expenditures, its commitments (a separate file) and the year-to-date BCA inputs,
    which are priced through the submissions service for estimated, unvalidated savings and TSB; each program is compared with
    its operating budget from Budgets. Fund shifts are made in Budgets, not here."""

    name = "cedars.modules.monthly"
    label = "monthly"
    verbose_name = "Monthly reports"

    def ready(self):
        from cedars.services.submissions.service import (
            register_acc_needs,
            register_deadline_page,
            register_nothing_to_file,
            register_publisher,
            register_reader,
            register_setup_groups,
            register_setup_link,
            register_setup_page,
        )

        from . import service

        register_publisher("monthly", service.on_publish, service.on_unpublish)
        register_acc_needs("monthly", service.acc_needs)  # Engine releases warns when the default release lacks one
        register_deadline_page("monthly", service.deadline_page)  # the filing calendar links each of its deadlines
        register_setup_page(  # the filing calendar's Add a filing page leads here
            "monthly",
            label="A year of monthly reports",
            url_name="monthly:year_new",
            about="A program year's twelve monthly reports and its 13th-month report, due with the annual claim: thirteen deadlines.",
            order=3,
        )
        register_setup_groups("monthly", service.setup_groups)  # what clear_filing_calendar deletes or retires together
        register_setup_link("monthly", service.setup_link)  # the calendar and Filing sets link to the set a filing belongs to
        register_nothing_to_file("monthly", service.nothing_to_file)  # the 13th-month report is generated from claims
        register_reader("monthly", service.can_read_submission)  # the Filings page shows where its filings stand to their readers
        from django.urls import reverse

        from cedars.services.submissions.service import register_version_page

        register_version_page("monthly", lambda v: reverse("monthly:version", args=[v.pk]))  # a pricing run's walkthrough links back

        from cedars.services.registry.service import register_reported_spend

        register_reported_spend(service.reported_spend)  # what the budget-and-spending dashboard shows

        from cedars.services.notifications.service import register_filing

        register_filing("monthly", service.notice)  # what subscribers and the programs' followers are told

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("monthly", program_page.reports)  # each program's page lists its monthly reports

        from cedars.services.submissions import corrections as record_corrections

        from .corrections import MonthlyEditor

        record_corrections.register(MonthlyEditor())  # records corrected in place on an open draft
