from django.apps import AppConfig


class RulesConfig(AppConfig):
    name = "cedars.services.rules"
    label = "rules"
    verbose_name = "Validation rule library"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import jobs

        register_purger("check_job", jobs.expired)  # check jobs' copies of the tables and their findings
