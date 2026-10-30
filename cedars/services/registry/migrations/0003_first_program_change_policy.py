"""The first program change policy (BR-13.7): the attributes that decide which filings, tests and allocations a program falls into are
locked; the rest stay with the program's administrators. It is data: a CPUC system administrator sets a newer policy on the locked
attributes page, and this one is kept as history."""

import datetime

from django.db import migrations

FIRST = datetime.date(2000, 1, 1)
LOCKED = ["segment", "statewide_program", "parent_program", "exclude_from_budget", "exclude_from_ce", "exclude_from_statewide"]


def add(apps, schema_editor):
    apps.get_model("registry", "ProgramChangePolicy").objects.get_or_create(
        effective_from=FIRST, defaults={"locked_fields": LOCKED, "reason": "First policy, shipped with the program change requests"}
    )


def remove(apps, schema_editor):
    apps.get_model("registry", "ProgramChangePolicy").objects.filter(effective_from=FIRST).delete()


class Migration(migrations.Migration):
    dependencies = [("registry", "0002_program_change_requests")]
    operations = [migrations.RunPython(add, remove)]
