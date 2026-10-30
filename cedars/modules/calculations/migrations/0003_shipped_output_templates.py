"""The two output templates ReCET ships with (recet/output_templates.py BUILTIN, 0.1.79), offered to everyone. They are data: a CPUC
system administrator may change or delete them, and add others, on the output templates page."""

from django.db import migrations

SHIPPED = {
    "Key results": [
        "CEInputID", "PrgID", "MeasDescription", "ClaimYearQuarter", "IOU_AC_Territory", "Used_ACCVersion", "ElecPricingStatus",
        "GasPricingStatus", "TotalSystemBenefit", "ElecBen", "GasBen", "OtherBen", "TRCCost", "PACCost", "TRCRatio", "PACRatio", "RIMRatio",
        "SCBRatio", "SCHRatio", "FirstYearNetKWh", "FirstYearNetKW", "FirstYearNetThm", "LifecycleNetElecCO2", "LifecycleNetGasCO2",
        "CreditedPA",
    ],
    "Participant costs": [
        "CEInputID", "PrgID", "MeasDescription", "ClaimYearQuarter", "GrossMeasureCost", "GrossMeasCostPV", "ExcessIncentives",
        "ExcessIncentivesPV", "GrossParticipantCost", "GrossParticipantCostAdjustedPV", "NetParticipantCost", "NetParticipantCostAdjusted",
        "NetParticipantCostPV", "RebatesandIncents", "RebatesandIncentsPV", "TRCCost", "TRCRatio",
    ],
}  # fmt: skip


def add(apps, schema_editor):
    OutputTemplate = apps.get_model("calculations", "OutputTemplate")
    for name, columns in SHIPPED.items():
        OutputTemplate.objects.get_or_create(owner=None, name=name, defaults={"columns": columns})


def remove(apps, schema_editor):
    apps.get_model("calculations", "OutputTemplate").objects.filter(owner=None, name__in=list(SHIPPED)).delete()


class Migration(migrations.Migration):
    dependencies = [("calculations", "0002_output_templates")]
    operations = [migrations.RunPython(add, remove)]
