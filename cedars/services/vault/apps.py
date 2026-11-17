from django.apps import AppConfig


class VaultConfig(AppConfig):
    """The PII vault: PII_Contact and PII_Site only, in their own database (the `vault` alias, its own instance
    and KMS key in AWS), linked to claims by ContactID and SiteID. Reached only through this service's functions."""

    name = "cedars.services.vault"
    label = "vault"
    verbose_name = "PII vault"

    def ready(self):
        from cedars.services.rules.coded import register_coded_checks

        from . import coded_checks

        register_coded_checks("vault", "Claims PII files", coded_checks.ABOUT, coded_checks.CHECKS, order=4)
        from cedars.services.specifications.editor import register_locked_fields

        from . import locked_fields

        register_locked_fields("claims", locked_fields.LOCKED)  # a claims specification draft keeps the fields the vault reads
