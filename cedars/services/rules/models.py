from django.conf import settings
from django.db import models


class RuleSet(models.Model):
    """One version of one module's rules (e.g. the claims rules effective 2026-03-12). A draft can be edited (rules added, removed or
    their tests changed, messages, fixes, module notes); a new version starts as a copy of a published one. A published version never
    changes (database triggers, migration 0002), and the version in force on a date is the newest published one effective by then
    (BR-03.3). A submission and a run record the version they were checked against."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    code = models.CharField(max_length=40, help_text="Which rules: claims, budget_filing, ...")
    label = models.CharField(max_length=200)
    effective_from = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(max_length=300, blank=True, help_text="Where the rules came from, e.g. CEDARS' rule files of 2026-03-12")
    based_on = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="The version this one was copied from"
    )
    reason = models.TextField(blank=True, help_text="Why this version was made")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["code", "-effective_from", "-pk"]
        constraints = [models.UniqueConstraint(fields=["code", "effective_from"], name="rules_one_version_per_date")]

    def __str__(self):
        return f"{self.label} ({self.effective_from:%Y-%m-%d})"


class Rule(models.Model):
    """One rule: its stable ID (content-hash, the same in every set and version that has it), severity, the table and field it tests,
    the plain-language message and suggested fix, the rule itself as data, and which modules apply it. A deliberate difference between
    modules is recorded in `difference` and listed on the rule library's pages."""

    class Severity(models.TextChoices):
        REJECT = "reject", "Rejects the record"
        WARNING = "warning", "Warning"

    rule_set = models.ForeignKey(RuleSet, on_delete=models.CASCADE, related_name="rules")
    rule_id = models.CharField(max_length=20)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    table = models.CharField(max_length=60)
    field = models.CharField(max_length=200)
    message = models.TextField()
    fix = models.TextField(blank=True)
    kind = models.CharField(max_length=10, help_text="rule (a validation or warning rule) or spec (a field constraint)")
    definition = models.JSONField(default=dict, help_text="The rule as the engine reads it")
    source = models.CharField(max_length=200, blank=True)
    modules = models.JSONField(default=list, help_text="Modules that apply it")
    difference = models.TextField(blank=True, help_text="Why it differs between modules, when it deliberately does")
    replaces = models.CharField(
        max_length=20, blank=True, help_text="The ID of the rule this one replaces in the version it was copied from (its test changed)"
    )

    class Meta:
        ordering = ["table", "severity", "field", "rule_id"]
        constraints = [models.UniqueConstraint(fields=["rule_set", "rule_id"], name="rules_one_id_per_set")]

    def __str__(self):
        return f"{self.rule_id} {self.message[:60]}"
