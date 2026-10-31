from django.apps import AppConfig


class ClaimsConfig(AppConfig):
    """The Claims module (phase 4, BR-05): quarterly and annual claims on CEDARS' claims template with the fuel-substitution
    infrastructure cost table, each confirmation a submission with versions on the submissions service, compiled over the year to date,
    priced through it before confirmation and published on confirm."""

    name = "cedars.modules.claims"
    label = "claims"
    verbose_name = "Claims"

    def ready(self):
        from cedars.services.submissions.service import register_claims_totals, register_publisher

        from . import service

        register_publisher("claims", service.on_publish, service.on_unpublish)
        register_claims_totals(service.year_totals)

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("claims", program_page.confirmations)  # each program's page lists its claims (BR-07.6)
