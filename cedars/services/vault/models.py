from django.db import models

# Everything here lives in the vault database (router.VaultRouter). It holds no foreign keys into the core database: organization,
# submission version and user are stored by id, and the claims service resolves them.


class PIIUpload(models.Model):
    """One PII file uploaded with a claims submission version. Accepted: its records are the version's PII (replacing an earlier
    accepted upload of the same version while it is open); rejected: no records are kept, only what failed (keys and fields, never
    values)."""

    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    organisation_id = models.BigIntegerField()
    version_id = models.BigIntegerField(help_text="The claims submission version it was uploaded with")
    uploaded_by_id = models.BigIntegerField(null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Status.choices)
    counts = models.JSONField(default=dict, help_text="{table: records}")
    findings = models.JSONField(default=list, help_text="[{table, line, key, field, rule, message}]: no values")

    class Meta:
        ordering = ["-uploaded_at", "-pk"]
        indexes = [models.Index(fields=["version_id", "status"]), models.Index(fields=["organisation_id", "status"])]

    def __str__(self):
        return f"PII upload {self.pk} ({self.status})"


class PIIContact(models.Model):
    """PII_Contact: one contact, keyed by ContactID (the ContactClaim table links it to claims)."""

    upload = models.ForeignKey(PIIUpload, on_delete=models.CASCADE, related_name="contacts")
    contact_id = models.CharField("ContactID", max_length=255)
    values = models.JSONField(help_text="{field: value} for the table's other fields")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["upload", "contact_id"], name="vault_one_contact")]

    def __str__(self):
        return self.contact_id


class PIISite(models.Model):
    """PII_Site: one site's address, service accounts and names, keyed by SiteID (the Site and Claim tables link it to claims)."""

    upload = models.ForeignKey(PIIUpload, on_delete=models.CASCADE, related_name="sites")
    site_id = models.CharField("SiteID", max_length=255)
    values = models.JSONField(help_text="{field: value} for the table's other fields")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["upload", "site_id"], name="vault_one_site")]

    def __str__(self):
        return self.site_id
