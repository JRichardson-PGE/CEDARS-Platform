"""The PII vault's checks (rule IDs PII-*) on a claim's PII_Contact and PII_Site file, listed on the Rules pages (rules.coded). Keep
this list in step with service.py: tests/test_coded_checks.py compares them. Any finding rejects the file."""

from cedars.services.rules.coded import CodedCheck as C

ABOUT = (
    "Run on a claim's PII file (PII_Contact and PII_Site) when it is uploaded to the vault. A finding names the record's key and the "
    "field, never the value, and any finding rejects the file."
)
CHECKS = [
    C("PII-REQ", "reject", "PII_Contact, PII_Site", "ContactID, SiteID", "The record's key is blank.", "Fill in the ContactID or SiteID."),
    C("PII-ID", "reject", "PII_Contact, PII_Site", "ContactID, SiteID", "The key does not start with the PA's code.",
      "Start each ContactID and SiteID with your PA code."),
    C("PII-KEY", "reject", "PII_Contact, PII_Site", "ContactID, SiteID", "The same key is on two records.", "Keep one record per key."),
    C("PII-REF", "reject", "PII_Contact, PII_Site", "ContactID, SiteID",
      "No ContactClaim or Site record of the claim names this key.",
      "Use the ContactIDs and SiteIDs the claim's ContactClaim and Site records name."),
    C("PII-LEN", "reject", "PII_Contact, PII_Site", "Any text field", "A value is longer than the field allows.",
      "Shorten the value to the field's length in the template."),
    C("PII-NUM", "reject", "PII_Contact", "The phone number and extension fields", "A phone number has characters other than digits.",
      "Write phone numbers as digits only."),
    C("PII-EMAIL", "reject", "PII_Contact", "ContactEmail", "An email address has no @.", "Correct the email address."),
    C("PII-ACCT", "reject", "PII_Site", "ElectricServiceAccountID, GasServiceAccountID",
      "Neither an electric nor a gas service account is filled in for the site.", "Fill in at least one of the two."),
]  # fmt: skip
