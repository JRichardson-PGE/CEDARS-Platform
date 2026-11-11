from django.apps import AppConfig


class CalculationsConfig(AppConfig):
    """The Calculations module: cost-effectiveness runs on the ReCET engine. The platform keeps the run records, the inputs
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

        from cedars.services.submissions.service import register_pricer, register_restater, register_run_state

        register_pricer(service.price_submission)  # filing modules price their versions through the submissions service
        register_run_state(service.run_state)  # and show whether that run still runs, or ended without pricing them
        from cedars.services.submissions.service import register_run_walkthrough

        register_run_walkthrough(service.walkthrough_url)  # a priced version's page links its run's measure walkthrough
        # and re-price a published filing as an analysis run that never touches it (restated results)
        register_restater(service.restate_version, service.restate_options, service.describe_run)

        from . import health

        health.register()  # stalled and failed runs on the administrators' health page

        from cedars.core import search as site_search

        from . import search

        site_search.register("runs", "Your runs", search.runs, order=30)  # site search finds the runs a person sees, by name

        from cedars.core import yourwork as your_work

        from . import yourwork

        # Your work: the person's last runs (front page and Calculations) and Edit inputs drafts (Calculations)
        your_work.register("runs", yourwork.runs, order=30, pages=("front", "calculations"))
        your_work.register("drafts", yourwork.edit_inputs, order=35, pages=("calculations",))
