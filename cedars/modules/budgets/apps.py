from django.apps import AppConfig


class BudgetsConfig(AppConfig):
    """The Budgets module (phase 3): budget filings for a four-year filing cycle (business plan, TUAL, MCAL), each a submission
    with versions on the submissions service, priced through it before confirmation; fund shifts follow. Every filed version is kept
    and comparable, excluded programs are filtered and never zeroed, and the confirmed inputs and run settings are published."""

    name = "cedars.modules.budgets"
    label = "budgets"
    verbose_name = "Budgets"

    def ready(self):
        from cedars.services.rules.precheck import register_checker

        from . import service as checks

        register_checker(  # the pre-upload checker runs the rule-set step of a budget filing's validation
            "budgets", checks.RULE_SET, "Budget and application filings", checks.precheck,
            year_help="Each record is checked as a filing for its own year; give a year to check only that year's records.",
        )  # fmt: skip
        from cedars.services.notifications.service import register_concerns, register_filing
        from cedars.services.registry.service import register_operating_budget
        from cedars.services.submissions.service import (
            register_acc_needs,
            register_deadline_page,
            register_publisher,
            register_reader,
            register_setup_groups,
            register_setup_link,
            register_setup_page,
        )

        from . import service, shifts

        register_publisher("budgets", service.on_publish, service.on_unpublish)
        register_acc_needs("budgets", service.acc_needs)  # Engine releases warns when the default release lacks one
        register_deadline_page("budgets", service.deadline_page)  # the filing calendar links each of its deadlines
        register_setup_page(  # the filing calendar's Add a filing page leads here
            "budgets",
            label="A budget filing",
            url_name="budgets:filing_new",
            about="A business plan application, true-up or mid-cycle advice letter for any span of program years: one deadline.",
            order=1,
        )
        register_setup_groups("budgets", service.setup_groups)  # what clear_filing_calendar deletes or retires together
        register_setup_link("budgets", service.setup_link)  # the calendar and Filing sets link to the set a filing belongs to
        register_reader("budgets", service.can_read_submission)  # the Filings page shows where its filings stand to their readers
        from django.urls import reverse

        from cedars.services.submissions.service import register_version_page

        register_version_page("budgets", lambda v: reverse("budgets:version", args=[v.pk]))  # a pricing run's walkthrough links back
        register_operating_budget(shifts.operating_rows)  # what monthly reports, claims and dashboards compare against
        register_filing("budgets", service.notice)  # what subscribers and the programs' followers are told
        register_concerns("budgets.fundshift", shifts.notice)

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("budgets", program_page.filings)  # each program's page lists its filings and shifts
        register_program_records("fund_shifts", program_page.fund_shifts)

        from cedars.core import queue as my_queue

        from . import queue

        # each user's queue: fund shifts waiting for their PA's acknowledgment
        my_queue.register("fund_shift", "Fund shifts to acknowledge", queue.acknowledgements, action="Acknowledge", order=60)

        from cedars.services.submissions import corrections as record_corrections

        from .corrections import BudgetsEditor

        record_corrections.register(BudgetsEditor())  # records corrected in place on an open draft
