"""The fields the PII vault's code reads, which a draft of the Quarterly claims specification cannot remove, rename or retype, nor make
optional where they are required (specifications.editor.register_locked_fields, under the claims module's specification)."""

KEY = "The PII vault links a person's or a site's record to the claim records by it."
ACCOUNT = "The PII vault reads it to check a service account number."
EMAIL = "The PII vault checks it is an email address."

LOCKED = {
    "Claim": {"SiteID": KEY},
    "Site": {"SiteID": KEY},
    "ContactClaim": {"ContactID": KEY},
    "Participant": {"SiteID": KEY},
    "InfrastructureCost": {"SiteID": KEY},
    "ClaimSite": {"SiteID": KEY},
    "PII_Contact": {"ContactID": KEY, "ContactEmail": EMAIL},
    "PII_Site": {"SiteID": KEY, "ElectricServiceAccountID": ACCOUNT, "GasServiceAccountID": ACCOUNT},
}
