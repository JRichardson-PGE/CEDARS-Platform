from django.apps import AppConfig


class AdministrationConfig(AppConfig):
    """The Admin and configuration module's access pages: a user's own account, PA admins' members and grants, recertification,
    data stewards' approvals and the CPUC's organizations, settings and audit log. Every change goes through accounts.service."""

    name = "cedars.modules.administration"
    label = "administration"
    verbose_name = "Administration"
