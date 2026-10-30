from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower


class Collection(models.TextChoices):
    """The library's five collections (BR-15.1): one record type, so search, versions, permanent links and downloads work alike."""

    PA = "pa", "PA documents"
    GUIDANCE = "guidance", "CPUC staff guidance"
    DEER = "deer", "DEER resources"
    PROGRAM = "program", "Program files"
    EMV = "emv", "EM&V documents"


class AttributeList(models.Model):
    """A controlled list of document attributes kept by CPUC ED (BR-15.3): the PDA's eight categories (sector, stage of work, study type,
    technology type, portfolio cycle, deliverable type, study lead, evaluation firm) and each collection's document types. Data with an
    admin page, not code; options are retired, never deleted, so documents filed with them keep them."""

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    multiple = models.BooleanField(default=True, help_text="A document can carry several of its options")
    collections = ArrayField(
        models.CharField(max_length=10, choices=Collection.choices), default=list, blank=True, help_text="The collections that use it"
    )
    type_of = models.CharField(
        max_length=10, choices=Collection.choices, blank=True, help_text="The collection whose document types it lists, if it is that"
    )
    position = models.PositiveSmallIntegerField(default=0)
    pda_id = models.PositiveIntegerField("PDA category id", null=True, blank=True, help_text="The PDA's catID, for the migration")

    class Meta:
        ordering = ["position", "name"]
        constraints = [models.UniqueConstraint(fields=["type_of"], condition=~Q(type_of=""), name="documents_one_type_list_per_collection")]

    def __str__(self):
        return self.name


class AttributeOption(models.Model):
    """One value of a list. A retired option stays on the documents that carry it and in search, but is no longer offered."""

    attribute_list = models.ForeignKey(AttributeList, on_delete=models.PROTECT, related_name="options")
    label = models.CharField(max_length=200)
    position = models.PositiveSmallIntegerField(default=0)
    retired_on = models.DateField(null=True, blank=True, help_text="Not offered for documents from this date")
    pda_id = models.PositiveIntegerField("PDA option id", null=True, blank=True, unique=True, help_text="The PDA's catOptID")

    class Meta:
        ordering = ["attribute_list__position", "position", "label"]
        constraints = [models.UniqueConstraint(F("attribute_list"), Lower("label"), name="documents_option_label_unique")]

    def __str__(self):
        return self.label

    def offered_on(self, day) -> bool:
        return self.retired_on is None or self.retired_on > day


class Document(models.Model):
    """One document of any collection, at its permanent link /documents/<id>/ (BR-15.1, 15.2). Its files live in numbered versions; the
    details here (title, summary, PAs, programs, years, attributes) are changed through the service with an audit event. Visible to the
    public once any version is published; until then only to its owner, the PAs named on it and CPUC staff (BR-15.9)."""

    collection = models.CharField(max_length=10, choices=Collection.choices)
    document_type = models.ForeignKey(AttributeOption, on_delete=models.PROTECT, related_name="typed_documents")
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, max_length=3500)
    owner = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="owned_documents", help_text="The organisation that filed it"
    )
    pas = models.ManyToManyField(
        "accounts.Organisation", blank=True, related_name="documents", help_text="The PAs it applies to: several for a joint document"
    )
    programs = models.ManyToManyField("registry.Program", blank=True, related_name="documents")
    first_year = models.PositiveSmallIntegerField(null=True, blank=True, help_text="The program year or reporting period it covers")
    last_year = models.PositiveSmallIntegerField(null=True, blank=True)
    attributes = models.ManyToManyField(AttributeOption, blank=True, related_name="documents")
    published_at = models.DateTimeField(null=True, blank=True, help_text="When its first version was published")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [F("published_at").desc(nulls_first=True), "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(first_year__isnull=True) | Q(last_year__isnull=True) | Q(first_year__lte=F("last_year")),
                name="documents_years_in_order",
            )
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return f"/documents/{self.pk}/"

    @property
    def years(self) -> str:
        if self.first_year and self.last_year and self.first_year != self.last_year:
            return f"{self.first_year} to {self.last_year}"
        return str(self.first_year or self.last_year or "")


class DocumentVersion(models.Model):
    """A numbered set of files for a document. Replacing a file adds a version; earlier ones stay listed and downloadable (BR-15.1,
    BR-09.6). A version is published once and never changes after (the database refuses: migration 0003)."""

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveSmallIntegerField()
    reason = models.TextField(blank=True, help_text="Why it replaces the previous version (required from version 2)")
    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="+", help_text="The author's organisation"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["document", "-number"]
        constraints = [models.UniqueConstraint(fields=["document", "number"], name="documents_version_number_unique")]

    def __str__(self):
        return f"{self.document} v{self.number}"

    @property
    def published(self) -> bool:
        return self.published_at is not None


class DocumentFile(models.Model):
    """A file of a version, stored once in the uploads area under its version's prefix. Position 0 is the main file; the rest are
    attachments."""

    version = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, related_name="files")
    position = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=255)
    key = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["version", "position"]
        constraints = [models.UniqueConstraint(fields=["version", "position"], name="documents_file_position_unique")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return f"/documents/files/{self.pk}/{self.name}"

    @property
    def main(self) -> bool:
        return self.position == 0
