"""The starting version of each filing specification, generated from what the platform checks today (starting/<code>.json, made by
`manage.py specification_starting --write` from the filing modules' layouts) and published, with the rule set in force today for its
rules. A specification whose current version already says the same (the same fingerprint) is left as it is."""

import json
from pathlib import Path

from django.db import migrations
from django.utils import timezone

STARTING = Path(__file__).resolve().parent.parent / "starting"


def load_starting(apps, schema_editor):
    from cedars.services.specifications import service

    Version = apps.get_model("specifications", "SpecificationVersion")
    RuleSet = apps.get_model("rules", "RuleSet")
    today = timezone.localdate()
    for path in sorted(STARTING.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        current = (
            Version.objects.filter(specification__code=document["specification"]["code"], status="published")
            .order_by("-published_at", "-pk")
            .first()
        )
        if current is not None and current.fingerprint == service.fingerprint(document):
            continue
        code = document["version"].get("rule_set_code", "")
        rule_set = (
            RuleSet.objects.filter(code=code, status="published", effective_from__lte=today).order_by("-effective_from").first()
            if code
            else None
        )
        service.load(document, models=apps, rule_set=rule_set)


class Migration(migrations.Migration):
    dependencies = [
        ("specifications", "0001_initial"),
        ("rules", "0002_rule_versions"),
    ]

    operations = [migrations.RunPython(load_starting, migrations.RunPython.noop)]
