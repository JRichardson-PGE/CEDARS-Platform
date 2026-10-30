"""The PII tables of CEDARS' 2026 Claims specification (its PII_Contact and PII_Site data rules, sent today over Kiteworks SFTP): each
field's name, type and length. Values are never written to logs, findings or the audit trail; findings name the key and field only."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # text, phone (digits only), email
    length: int = 255


TABLES = {
    "PII_Contact": (
        Field("ContactID", "text"),
        Field("ContactType", "text"),
        Field("ContactName", "text"),
        Field("ContactPhoneNumber", "phone"),
        Field("ContactPhoneExtension", "phone"),
        Field("ContactPhoneType", "text"),
        Field("ContactPhoneNumberAlt", "phone"),
        Field("ContactPhoneExtensionAlt", "phone"),
        Field("ContactPhoneTypeAlt", "text"),
        Field("ContactEmail", "email"),
    ),
    "PII_Site": (
        Field("SiteID", "text"),
        Field("SiteAddress", "text"),
        Field("SiteUnitNumber", "text"),
        Field("ElectricServiceAccountID", "text"),
        Field("GasServiceAccountID", "text"),
        Field("CensusTract", "text", 11),
        Field("ServiceAccountName", "text"),
        Field("BusinessName", "text"),
        Field("SchoolIdentifier", "text"),
    ),
}
KEYS = {"PII_Contact": "ContactID", "PII_Site": "SiteID"}
NAMES = {"piicontact": "PII_Contact", "piisite": "PII_Site"}


def columns(table: str) -> list[str]:
    return [f.name for f in TABLES[table]]


def table_name(name: str) -> str | None:
    return NAMES.get(name.strip().lower().replace("_", "").replace(" ", "").replace("-", ""))
