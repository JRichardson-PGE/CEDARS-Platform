from django.apps import AppConfig


class RefdataConfig(AppConfig):
    name = "cedars.services.refdata"
    label = "refdata"
    verbose_name = "Reference data"

    def ready(self):
        from cedars.core import page_access

        from . import etrm, service

        # who the Help guides offer a link to Upload eTRM permutations (UI review 3, U4): the page's own check
        page_access.register("refdata:etrm_upload", lambda r: service.can_manage(r.user, etrm.KIND), "the CPUC's eTRM data administrators")
