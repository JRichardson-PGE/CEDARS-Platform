"""The value lists the study register adds (BR-15.6: status, type, group and lead as controlled lists, replacing the PSR's free text).
Status: the PSR's eleven project statuses. Group: its sub-sectors. Study lead gains REN, which the PSR has and the PDA does not (27 of
its 740 studies). Sector gains the PSR's Public and Non-Resource; portfolio cycle its funding cycles the PDA lacks (it stopped at 2021).
Study type is the PDA's list, which holds the four types the PSR's form offers. Lists with no collection are the register's alone."""

from django.db import migrations
from django.db.models import Max

# (code, name, multiple, [labels]) for the register's own lists
STUDY_LISTS = [
    (
        "study_status",
        "Study status",
        False,
        [
            "Initiation",
            "Scoping",
            "RFP",
            "Design",
            "Data/Analysis",
            "Draft Report",
            "Completed",
            "On-hold",
            "Cancelled",
            "Combined",
            "Other",
        ],
    ),
    (
        "study_group",
        "Study group",
        False,
        [
            "Group B",
            "Codes and Standards",
            "ME&O",
            "ETP",
            "WE&T",
            "Finance",
            "Equity",
            "Residential",
            "New Construction",
            "Cross-Cutting",
        ],
    ),
]
ADDITIONS = {
    "study_lead": ["REN"],
    "sector": ["Public", "Non-Resource"],
    "portfolio_cycle": ["2013-2015", "2017-2018"] + [str(y) for y in range(2022, 2041)],
}


def seed(apps, schema_editor):
    AttributeList = apps.get_model("documents", "AttributeList")
    AttributeOption = apps.get_model("documents", "AttributeOption")
    start = (AttributeList.objects.aggregate(m=Max("position"))["m"] or 0) + 1
    for i, (code, name, multiple, labels) in enumerate(STUDY_LISTS):
        al, _ = AttributeList.objects.get_or_create(
            code=code, defaults={"name": name, "multiple": multiple, "collections": [], "position": start + i}
        )
        for j, label in enumerate(labels):
            AttributeOption.objects.get_or_create(attribute_list=al, label=label, defaults={"position": j})
    for code, labels in ADDITIONS.items():
        al = AttributeList.objects.get(code=code)
        top = al.options.aggregate(m=Max("position"))["m"] or 0
        for j, label in enumerate(labels, start=1):
            if not al.options.filter(label__iexact=label).exists():
                AttributeOption.objects.create(attribute_list=al, label=label, position=top + j)


class Migration(migrations.Migration):
    dependencies = [("documents", "0004_study_register")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
