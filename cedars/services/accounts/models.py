from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower


class PlatformUserManager(UserManager):
    """Users sign in with their email address; the username field is kept equal to it so Django's tools still work."""

    def _create_user(self, username, email, password, **extra_fields):
        email = self.normalize_email(email or username)
        return super()._create_user(email, email, password, **extra_fields)

    def create_user(self, email=None, password=None, **extra_fields):
        return super().create_user(email, email, password, **extra_fields)

    def create_superuser(self, email=None, password=None, **extra_fields):
        return super().create_superuser(email, email, password, **extra_fields)


class User(AbstractUser):
    """One identity per person, whatever organisations they belong to (BR-01.1). Accounts are deactivated, never deleted, so their audit
    history keeps its actor. An account with no membership is a community account (public read and its own runs)."""

    email = models.EmailField("email address", unique=True)
    name = models.CharField("full name", max_length=200, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = PlatformUserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique")]

    def save(self, *args, **kwargs):
        self.email = self.email.strip()
        self.username = self.email
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.email


class Organisation(models.Model):
    """A program administrator (IOU, REN or CCA), the CPUC, or another organisation (an evaluator, an implementer)."""

    class Kind(models.TextChoices):
        PA = "pa", "Program administrator"
        CPUC = "cpuc", "CPUC"
        OTHER = "other", "Other"

    class PAType(models.TextChoices):
        IOU = "IOU", "Investor-owned utility"
        REN = "REN", "Regional energy network"
        CCA = "CCA", "Community choice aggregator"

    code = models.CharField(max_length=20, unique=True, help_text="Short code used in filings, e.g. PGE, BAYREN")
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    pa_type = models.CharField("PA type", max_length=3, choices=PAType.choices, blank=True)
    federated = models.BooleanField(default=False, help_text="Its members sign in through the organisation's own directory (decision F)")
    federation_provider = models.CharField(max_length=100, blank=True, help_text="The sign-in provider id its federated members use")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["kind"], condition=models.Q(kind="cpuc"), name="accounts_one_cpuc_organisation")]

    def __str__(self):
        return f"{self.code} ({self.name})"


class Membership(models.Model):
    """A user's membership of an organisation. Granting one promotes a community account; ending it demotes the account again, which
    keeps its own history. Runs and uploads made under a membership belong to the organisation."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="memberships")
    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="memberships")
    started_at = models.DateTimeField(auto_now_add=True)
    started_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    end_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["organisation__code", "user__email"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organisation"], condition=models.Q(ended_at__isnull=True), name="accounts_one_open_membership"
            )
        ]

    def __str__(self):
        return f"{self.user.email} in {self.organisation.code}"

    @property
    def active(self) -> bool:
        return self.ended_at is None


class Grant(models.Model):
    """One permission held by one member: who granted it and when, an optional expiry, and who revoked it, when and why (BR-01.2 to 01.4).
    A grant is never deleted; its status moves on and every move is an audit event."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a data steward"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        REVOKED = "revoked", "Revoked"
        DECLINED = "declined", "Declined"

    OPEN = (Status.PENDING, Status.ACTIVE, Status.SUSPENDED)

    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="grants")
    permission = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    granted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)
    grant_reason = models.TextField(blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="The data steward")
    approved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    certified_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    certified_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_reason = models.TextField(blank=True)
    revoked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["membership__organisation__code", "membership__user__email", "permission"]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "permission"],
                condition=models.Q(status__in=["pending", "active", "suspended"]),
                name="accounts_one_open_grant_per_permission",
            )
        ]

    def __str__(self):
        return f"{self.permission} for {self.membership}"

    @property
    def user(self):
        return self.membership.user

    @property
    def organisation(self):
        return self.membership.organisation


class AccessRequest(models.Model):
    """A user asking an organisation's admins for membership and permissions (D6: they are the only approval step)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        WITHDRAWN = "withdrawn", "Withdrawn"

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="access_requests")
    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="access_requests")
    permissions = models.JSONField(default=list, help_text="Permission codes asked for")
    message = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} asks {self.organisation.code}"


class RecertificationCampaign(models.Model):
    """An organisation's periodic access review: its admins confirm or remove each open grant by the due date; what is not certified by
    then is suspended when AccessPolicy.recertification_auto_suspend is on."""

    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="recertifications")
    opened_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    opened_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(fields=["organisation"], condition=models.Q(closed_at__isnull=True), name="accounts_one_open_campaign")
        ]

    def __str__(self):
        return f"{self.organisation.code} recertification due {self.due_at:%Y-%m-%d}"


class RecertificationItem(models.Model):
    class Decision(models.TextChoices):
        UNDECIDED = "", "Not yet reviewed"
        CERTIFIED = "certified", "Certified"
        REMOVED = "removed", "Removed"

    campaign = models.ForeignKey(RecertificationCampaign, on_delete=models.PROTECT, related_name="items")
    grant = models.ForeignKey(Grant, on_delete=models.PROTECT, related_name="recertification_items")
    decision = models.CharField(max_length=10, choices=Decision.choices, blank=True, default="")
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["grant__membership__user__email", "grant__permission"]
        constraints = [models.UniqueConstraint(fields=["campaign", "grant"], name="accounts_one_item_per_grant")]

    def __str__(self):
        return f"{self.grant} in {self.campaign}"


class AccessPolicy(models.Model):
    """The access settings the CPUC can change without a release (one row). Settings that change the rules for everyone's PII take one
    data steward to propose and a second to confirm (PolicyChange, decision G); the others a system administrator changes, audited."""

    STEWARD_SETTINGS = frozenset({"pii_requires_federated"})

    recertification_auto_suspend = models.BooleanField(default=True, help_text="Suspend grants not certified by the campaign's due date")
    recertification_interval_days = models.PositiveIntegerField(default=365)
    pii_requires_federated = models.BooleanField(
        default=False, help_text="PII grants only for federated organisations, usable only in a federated session (decision F)"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "access policy"

    def __str__(self):
        return "Access policy"

    @classmethod
    def current(cls) -> "AccessPolicy":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PolicyChange(models.Model):
    """A data steward's proposed change to a steward-controlled setting, taking effect when a second steward confirms it."""

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        CONFIRMED = "confirmed", "Confirmed"
        WITHDRAWN = "withdrawn", "Withdrawn"

    setting = models.CharField(max_length=60)
    value = models.JSONField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROPOSED)
    proposed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    proposed_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-proposed_at"]

    def __str__(self):
        return f"{self.setting} = {self.value!r} ({self.status})"


class ApiToken(models.Model):
    """A key a member's own systems use to call the API for one organisation (decision J; BR-01). Only the SHA-256 of the key is kept:
    the key is shown once, when it is made. A token never holds more than its user does: its permissions are the user's grants in its
    organisation, limited to its scopes (modules), and it stops working when it expires, is revoked, or the membership ends."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="api_tokens")
    organisation = models.ForeignKey(
        Organisation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="api_tokens",
        help_text="Blank: a community account's own",
    )
    name = models.CharField(max_length=100, help_text="What it is for, e.g. the system that uses it")
    prefix = models.CharField(max_length=12, unique=True, help_text="The key's first part, shown so a key can be recognised")
    key_sha256 = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list, help_text="The modules it may reach (permissions.MODULES keys)")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    revoke_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @property
    def status(self) -> str:
        from django.utils import timezone

        if self.revoked_at:
            return "revoked"
        return "expired" if self.expires_at <= timezone.now() else "active"
