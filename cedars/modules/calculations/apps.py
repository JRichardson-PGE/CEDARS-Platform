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

        from . import service

        register_purger("scratch_run", service.expired_runs)
