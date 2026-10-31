from django.apps import AppConfig


class CalculationsConfig(AppConfig):
    """The Calculations module: cost-effectiveness runs on the ReCET engine (BR-06). The platform keeps the run records, the inputs
    and outputs in object storage and the queue; a ReCET worker (`recet worker run`, the engine's job contract) prices each job, locally
    or on AWS Batch, and writes its outputs and a receipt. The engine is never imported here."""

    name = "cedars.modules.calculations"
    label = "calculations"
    verbose_name = "Calculations"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import drafts, service

        def expired_scratch(cutoff):  # one purger per record class: scratch runs, then idle Edit inputs drafts
            yield from service.expired_runs(cutoff)
            yield from drafts.expired_drafts(cutoff)

        register_purger("scratch_run", expired_scratch)

        from cedars.services.submissions.service import register_pricer

        register_pricer(service.price_submission)  # filing modules price their versions through the submissions service

        from . import health

        health.register()  # stalled and failed runs on the administrators' health page (BR-12.2)
