from django.apps import AppConfig


class ClaimsConfig(AppConfig):
    """The Claims module (phase 4): quarterly and annual claims on CEDARS' claims template with the fuel-substitution
    infrastructure cost table, each confirmation a submission with versions on the submissions service, compiled over the year to date,
    priced through it before confirmation and published on confirm."""

    name = "cedars.modules.claims"
    label = "claims"
    verbose_name = "Claims"

    def ready(self):
        from cedars.services.rules.precheck import register_checker

        from . import service as checks

        register_checker(  # the pre-upload checker runs the rule-set step of a claims upload's validation
            "claims", checks.RULE_SET, "Quarterly claims", checks.precheck,
            year_help="The claim year; left blank, the one year the records are for.",
        )  # fmt: skip
        from cedars.services.rules.coded import register_coded_checks

        from . import coded_checks

        register_coded_checks("claims", "Claims", coded_checks.ABOUT, coded_checks.CHECKS, order=2)
        from cedars.services.specifications.editor import register_locked_fields

        from . import locked_fields

        register_locked_fields("claims", locked_fields.LOCKED)  # a draft keeps the fields this module reads
        from cedars.services.submissions.service import (
            register_claims_totals,
            register_deadline_page,
            register_filers,
            register_publisher,
            register_reader,
            register_setup_groups,
            register_setup_link,
            register_setup_page,
            register_stream,
        )

        from . import service

        register_publisher("claims", service.on_publish, service.on_unpublish)
        from cedars.core import search as site_search

        from . import search

        site_search.register("claims", "Confirmed claims", search.claims, order=65)  # site search finds a confirmed claim by ClaimID
        register_claims_totals(service.year_totals)
        from cedars.services.submissions.service import register_acc_needs

        register_acc_needs("claims", service.acc_needs)  # Engine releases warns when the default release lacks one
        register_deadline_page("claims", service.deadline_page)  # the filing calendar links each claim period
        register_setup_page(  # the filing calendar's Add a filing page leads here
            "claims",
            label="A claim year",
            url_name="claims:year_new",
            about="A program year's claims: its four quarterly claims and the annual claim, each with its own deadline, and the "
            "statewide claims filed before each.",
            order=2,
        )
        register_setup_groups("claims", service.setup_groups)  # what clear_filing_calendar deletes or retires together
        register_setup_link("claims", service.setup_link)  # the calendar and Filing sets link to the set a filing belongs to
        register_reader("claims", service.can_read_submission)  # the Filings page shows where its filings stand to their readers
        from django.urls import reverse

        from cedars.services.submissions.service import register_version_page

        register_version_page("claims", lambda v: reverse("claims:version", args=[v.pk]))  # a pricing run's walkthrough links back
        register_filers("claims", service.filers)  # statewide claims are filed by the statewide programs' lead PAs only
        register_stream("claims", service.stream)  # a lead PA's statewide claims stand beside its own (snapshots)

        from cedars.services.notifications.service import register_filing

        register_filing("claims", service.notice)  # what subscribers and the programs' followers are told

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("claims", program_page.confirmations)  # each program's page lists its claims

        from cedars.services.submissions import corrections as record_corrections

        from .corrections import ClaimsEditor

        record_corrections.register(ClaimsEditor())  # records corrected in place on an open draft
