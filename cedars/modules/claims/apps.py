from django.apps import AppConfig


class ClaimsConfig(AppConfig):
    """The Claims module (phase 4, BR-05): quarterly and annual claims on CEDARS' claims template with the fuel-substitution
    infrastructure cost table, each confirmation a submission with versions on the submissions service, compiled over the year to date,
    priced through it before confirmation and published on confirm."""

    name = "cedars.modules.claims"
    label = "claims"
    verbose_name = "Claims"

    def ready(self):
        from cedars.services.rules.precheck import register_checker

        from . import service as checks

        register_checker(  # the pre-upload checker runs the rule-set step of a claims upload's validation (BR-03.4)
            "claims", checks.RULE_SET, "Quarterly claims", checks.precheck,
            year_help="The claim year; left blank, the one year the records are for.",
        )  # fmt: skip
        from cedars.services.submissions.service import register_claims_totals, register_deadline_page, register_publisher

        from . import service

        register_publisher("claims", service.on_publish, service.on_unpublish)
        register_claims_totals(service.year_totals)
        register_deadline_page("claims", service.deadline_page)  # the filing calendar links each claim period

        from cedars.services.notifications.service import register_filing

        register_filing("claims", service.notice)  # what subscribers and the programs' followers are told (BR-09.2, 09.3)

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("claims", program_page.confirmations)  # each program's page lists its claims (BR-07.6)

        from cedars.services.submissions import corrections as record_corrections

        from .corrections import ClaimsEditor

        record_corrections.register(ClaimsEditor())  # records corrected in place on an open draft (BR-02.4)
