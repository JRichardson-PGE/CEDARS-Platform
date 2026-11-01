from django.apps import AppConfig


class BudgetsConfig(AppConfig):
    """The Budgets module (phase 3, BR-04): budget filings for a four-year filing cycle (business plan, TUAL, MCAL), each a submission
    with versions on the submissions service, priced through it before confirmation; fund shifts follow. Every filed version is kept
    and comparable, excluded programs are filtered and never zeroed, and the confirmed inputs and run settings are published."""

    name = "cedars.modules.budgets"
    label = "budgets"
    verbose_name = "Budgets"

    def ready(self):
        from cedars.services.notifications.service import register_concerns, register_filing
        from cedars.services.registry.service import register_operating_budget
        from cedars.services.submissions.service import register_deadline_page, register_publisher

        from . import service, shifts

        register_publisher("budgets", service.on_publish, service.on_unpublish)
        register_deadline_page("budgets", service.deadline_page)  # the filing calendar links each of its deadlines
        register_operating_budget(shifts.operating_rows)  # what monthly reports, claims and dashboards compare against (BR-04.7)
        register_filing("budgets", service.notice)  # what subscribers and the programs' followers are told (BR-09.2, 09.3)
        register_concerns("budgets.fundshift", shifts.notice)

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("budgets", program_page.filings)  # each program's page lists its filings and shifts (BR-07.6)
        register_program_records("fund_shifts", program_page.fund_shifts)
