from django.apps import AppConfig


class SpecificationsConfig(AppConfig):
    name = "cedars.services.specifications"
    label = "specifications"
    verbose_name = "Filing specifications"

    def ready(self):
        from cedars.services.rules import service as rules

        from . import service

        rules.register_layout_provider(service.rule_set_layout)
