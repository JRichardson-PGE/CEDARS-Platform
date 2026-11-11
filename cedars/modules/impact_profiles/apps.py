from django.apps import AppConfig


class ImpactProfilesConfig(AppConfig):
    """The Impact profiles module: deemed load shapes synced from eTRM and processed into impact profiles, custom (PA-owned) impact
    profiles, and the profile library the engine reads (architecture, "The Impact profiles module, step by step")."""

    name = "cedars.modules.impact_profiles"
    label = "impact_profiles"
    verbose_name = "Impact profiles"

    def ready(self):
        from . import health

        health.register()  # stalled and failed jobs and the eTRM sync on the administrators' health page

        from cedars.core import page_access

        from . import access

        # who the Help guides offer a link to these pages (UI review 3, U4): the pages' own checks
        page_access.register(
            "impact_profiles:custom_new",
            lambda r: access.can_submit(r.user, getattr(r, "organisation", None)),
            "people who submit impact profiles for the organization they act for",
        )
        page_access.register("impact_profiles:settings", lambda r: access.is_admin(r.user), "the CPUC's impact profile administrators")
