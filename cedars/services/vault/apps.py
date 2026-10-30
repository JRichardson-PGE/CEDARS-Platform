from django.apps import AppConfig


class VaultConfig(AppConfig):
    """The PII vault (decision G, BR-10.1): PII_Contact and PII_Site only, in their own database (the `vault` alias, its own instance
    and KMS key in AWS), linked to claims by ContactID and SiteID. Reached only through this service's functions."""

    name = "cedars.services.vault"
    label = "vault"
    verbose_name = "PII vault"
