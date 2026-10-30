from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class AuditEvent(models.Model):
    """One change: who (and under which organisation and sign-in assurance), when, what, why, before and after. Never updated or
    deleted; kept permanently (decision M)."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    actor_label = models.CharField(max_length=254, blank=True, help_text="The actor's email at the time, or 'system'")
    organisation = models.ForeignKey(
        "accounts.Organisation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The actor's active organisation",
    )
    assurance = models.CharField(max_length=20, blank=True, help_text="How the actor's session was established: password, mfa or federated")
    action = models.CharField(max_length=80, db_index=True, help_text="dotted name, e.g. grant.revoked")
    object_type = models.CharField(max_length=80, blank=True, db_index=True, help_text="app_label.model of the changed record")
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    object_repr = models.CharField(max_length=300, blank=True)
    reason = models.TextField(blank=True)
    before = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id", "at"])]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor_label or 'system'} {self.action} {self.object_repr}"
