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

        from cedars.core import yourwork as your_work

        from . import yourwork

        # Your work on the Reference data page: the person's cost-test sets being built and draft packs they registered
        your_work.register("cost_test_builds", yourwork.cost_test_sets, order=50, pages=("reference",))
        your_work.register("draft_packs", yourwork.draft_packs, order=55, pages=("reference",))
