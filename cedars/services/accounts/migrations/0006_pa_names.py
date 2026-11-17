"""PAs created from a filed code alone carry the code as their name ("SDGE (SDGE)" on every page, UAT F17). Name the ones CEDARS lists
(accounts.service.KNOWN_PA_NAMES, copied here so the migration does not change when the table does); a PA whose name was set by hand
is left alone, and so is a code the table does not have."""

from django.db import migrations

NAMES = {
    "PGE": "Pacific Gas and Electric Company",
    "SCE": "Southern California Edison",
    "SCG": "Southern California Gas Company",
    "SDGE": "San Diego Gas & Electric",
    "BAY": "Bay Area Regional Energy Network (BayREN)",
    "CCR": "Central California Rural Regional Energy Network",
    "IREN": "Inland Regional Energy Network",
    "RRN": "Rural Regional Energy Network",
    "SCR": "Southern California Regional Energy Network (SoCalREN)",
    "SDRN": "San Diego Regional Energy Network",
    "TCR": "Tri-County Regional Energy Network (3C-REN)",
    "AVA": "Ava Community Energy",
    "CPSF": "CleanPowerSF",
    "EBCE": "East Bay Community Energy",
    "LCE": "Lancaster Choice Energy",
    "MCE": "MCE",
    "PCE": "Peninsula Clean Energy",
    "RCEA": "Redwood Coast Energy Authority",
    "SCP": "Sonoma Clean Power",
    "SJCE": "San José Clean Energy",
}


def name_pas(apps, schema_editor):
    Organisation = apps.get_model("accounts", "Organisation")
    for org in Organisation.objects.filter(kind="pa"):
        if org.name == org.code and org.code in NAMES:
            org.name = NAMES[org.code]
            org.save(update_fields=["name"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_us_spelling")]
    operations = [migrations.RunPython(name_pas, migrations.RunPython.noop)]
