"""The value lists the library starts with. The PDA's eight categories with all 82 options as its public /api/categories gave them on
2026-10-30 (catID / catOptID kept for the migration of its documents), and the document types of the other collections: CEDARS' eight
Reports & Documents report types, the DEER Resources sections, and the program files BR-15.1 names. CPUC ED keeps them from here on the
value-list page; nothing here is fixed in code."""

from django.db import migrations

ALL = ["pa", "guidance", "deer", "program", "emv"]
EMV = ["emv"]

# (code, name, multiple, collections, type_of, pda catID, [(pda catOptID or None, label)])
LISTS = [
    (
        "pa_report_type",
        "Report type",
        False,
        [],
        "pa",
        None,
        [
            (None, "AB 841 School EE Reports"),
            (None, "Annual Report Narratives and Spreadsheets"),
            (None, "Cap and Target Report"),
            (None, "Goal Construct Survey"),
            (None, "Joint Cooperation Memo"),
            (None, "Monthly Expenditures Report"),
            (None, "On-Bill Finance Annual Report"),
            (None, "Program Segment Justification & Equity Market Support Indicators"),
        ],
    ),
    ("guidance_type", "Guidance type", False, [], "guidance", None, [(None, "Template"), (None, "Guidance document")]),
    (
        "deer_type",
        "DEER resource type",
        False,
        [],
        "deer",
        None,
        [
            (None, "Resolution"),
            (None, "Disposition"),
            (None, "Guidance"),
            (None, "Monthly summary"),
            (None, "Measure package"),
            (None, "DEER version"),
        ],
    ),
    ("program_file_type", "Program file type", False, [], "program", None, [(None, "Implementation plan"), (None, "Evaluation")]),
    (
        "deliverable_type",
        "Deliverable type",
        False,
        [],
        "emv",
        6,
        [
            (6, "Evaluation Plan"),
            (9, "Work Order"),
            (10, "Report"),
            (11, "Interim Work Product"),
            (12, "Final Evaluation Report"),
            (58, "Public Comments"),
            (70, "Draft Report"),
            (77, "Quarterly Meeting Materials"),
        ],
    ),
    (
        "sector",
        "Sector",
        True,
        ALL,
        "",
        1,
        [
            (1, "Agricultural"),
            (2, "Commercial"),
            (5, "Residential"),
            (15, "Industrial"),
            (16, "New Construction"),
            (17, "Codes & Standards"),
            (54, "Cross Cutting"),
            (71, "Low Income"),
            (79, "Water Energy Nexus"),
        ],
    ),
    (
        "stage_of_work",
        "Stage of work",
        False,
        EMV,
        "",
        2,
        [
            (3, "Signed Work Order"),
            (19, "Final Work Order"),
            (35, "IOU Projects Final for Download"),
            (63, "IOU SOW"),
            (64, "IOU Final Report"),
            (65, "Research Plan"),
            (66, "Preliminary Results"),
            (68, "Draft Report"),
            (69, "Final Report"),
        ],
    ),
    (
        "study_type",
        "Study type",
        True,
        EMV,
        "",
        3,
        [
            (22, "Evaluation Guidelines"),
            (23, "Impact Evaluation"),
            (24, "Market Effects"),
            (25, "Market Study"),
            (26, "Measure Retention"),
            (36, "Process Evaluation"),
            (37, "Program Design - Best Practices"),
            (38, "Program Design - Integration Assessment"),
            (39, "Program Design - Adoption Effectiveness"),
            (40, "Parameter Study - (NTG, EUL, IMC)"),
            (41, "Portfolio Analysis - Portfolio Impacts"),
            (42, "Portfolio Analysis - Portfolio Costs"),
            (56, "Potential & Goals Study"),
            (67, "Pilot Studies"),
            (76, "Master Joint Evaluation Plan"),
            (78, "Quarterly Meeting Materials"),
            (83, "Other"),
        ],
    ),
    (
        "technology_type",
        "Technology type",
        True,
        EMV,
        "",
        4,
        [
            (27, "Lighting"),
            (28, "HVAC"),
            (29, "Custom and Process"),
            (30, "Plug Loads"),
            (31, "Water Heating"),
            (43, "Building Envelope"),
            (44, "Insulation"),
            (45, "Emerging Technologies"),
            (46, "Codes & Standards"),
            (47, "Education & Training"),
            (48, "Marketing & Outreach"),
            (49, "Quality Installation"),
            (50, "Quality Maintenance"),
            (51, "Industrial Process"),
            (52, "Agricultural Process"),
            (53, "Other"),
            (57, "Multiple"),
            (80, "Water Energy Nexus"),
        ],
    ),
    (
        "portfolio_cycle",
        "Portfolio cycle",
        True,
        EMV,
        "",
        5,
        [
            (8, "2006-2008"),
            (7, "2009 (Bridge Year)"),
            (32, "2010-2012"),
            (72, "2013-2014"),
            (75, "2015-2016"),
            (81, "2016-2017"),
            (84, "2018"),
            (85, "2019"),
            (88, "2020"),
            (89, "2021"),
        ],
    ),
    ("study_lead", "Study lead", False, EMV, "", 7, [(20, "CPUC"), (21, "IOU")]),
    (
        "evaluation_firm",
        "Evaluation firm",
        False,
        EMV,
        "",
        8,
        [
            (13, "Kema"),
            (14, "Itron"),
            (59, "PGE"),
            (60, "SCE"),
            (61, "Sempra"),
            (73, "Opinion Dynamics"),
            (74, "CPUC-Energy Division"),
            (86, "DNV GL"),
            (87, "SBW"),
        ],
    ),
]


def seed(apps, schema_editor):
    AttributeList = apps.get_model("documents", "AttributeList")
    AttributeOption = apps.get_model("documents", "AttributeOption")
    for position, (code, name, multiple, collections, type_of, pda_id, options) in enumerate(LISTS):
        al, _ = AttributeList.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "multiple": multiple,
                "collections": collections,
                "type_of": type_of,
                "position": position,
                "pda_id": pda_id,
            },
        )
        for i, (opt_id, label) in enumerate(options):
            AttributeOption.objects.get_or_create(attribute_list=al, label=label, defaults={"position": i, "pda_id": opt_id})


class Migration(migrations.Migration):
    dependencies = [("documents", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
