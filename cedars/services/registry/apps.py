from django.apps import AppConfig


class RegistryConfig(AppConfig):
    name = "cedars.services.registry"
    label = "registry"
    verbose_name = "Program registry"

    def ready(self):
        from cedars.core import queue as my_queue

        from . import queue

        my_queue.register("program_change", "Program changes to decide", queue.change_requests, action="Decide", order=20)

        from cedars.services.dictionary import value_lists

        from . import field_guide

        for field, list_name in field_guide.VALUE_LISTS.items():  # kept as versions on the dictionary's Value lists pages
            value_lists.register_list(
                list_name,
                label=field_guide.entry(field).dictionary[0],
                origin="the values in the program fields guide",
                rows=lambda field=field: field_guide.first_rows(field),
            )
