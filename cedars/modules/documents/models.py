from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone


class Collection(models.TextChoices):
    """The library's five collections (BR-15.1): one record type, so search, versions, permanent links and downloads work alike."""

    PA = "pa", "PA documents"
    GUIDANCE = "guidance", "CPUC staff guidance"
    DEER = "deer", "DEER resources"
    PROGRAM = "program", "Program files"
    EMV = "emv", "EM&V documents"


class Stage(models.TextChoices):
    """The stages of an EM&V deliverable's revision workflow (BR-15.11), in order. The comment stages ("workplan comments", "study-result
    comments") are the comment windows of the draft versions, not versions of their own."""

    DRAFT_WORKPLAN = "draft_workplan", "Draft workplan"
    FINAL_WORKPLAN = "final_workplan", "Final workplan"
    DRAFT_RESULTS = "draft_results", "Draft study results"
    FINAL_RESULTS = "final_results", "Final study results"
    PA_RESPONSE = "pa_response", "PA response to the recommendations"
    ED_REPLY = "ed_reply", "CPUC ED reply"


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
        "accounts.Organisation", on_delete=models.PROTECT, related_name="owned_documents", help_text="The organization that filed it"
    )
    pas = models.ManyToManyField(
        "accounts.Organisation", blank=True, related_name="documents", help_text="The PAs it applies to: several for a joint document"
    )
    programs = models.ManyToManyField("registry.Program", blank=True, related_name="documents")
    first_year = models.PositiveSmallIntegerField(null=True, blank=True, help_text="The program year or reporting period it covers")
    last_year = models.PositiveSmallIntegerField(null=True, blank=True)
    attributes = models.ManyToManyField(AttributeOption, blank=True, related_name="documents")
    study = models.ForeignKey(
        "Study", null=True, blank=True, on_delete=models.PROTECT, related_name="documents", help_text="The evaluation study it belongs to"
    )
    work_order = models.ForeignKey(
        "WorkOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text="The contract it was delivered under",
    )
    task = models.ForeignKey("Task", null=True, blank=True, on_delete=models.PROTECT, related_name="documents")
    deliverable = models.BooleanField(
        default=False, help_text="An EM&V deliverable: its versions go through the revision stages (BR-15.11) on this one record"
    )
    published_at = models.DateTimeField(null=True, blank=True, help_text="When its first version was published")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, help_text="When it was filed (a migrated document: its original date)")
    updated_at = models.DateTimeField(auto_now=True)
    pda_id = models.PositiveIntegerField(
        "PDA document id", null=True, blank=True, unique=True, help_text="The PDA's deliverableID, which its old links carry (BR-15.10)"
    )
    calmac_id = models.CharField("CALMAC ID", max_length=50, blank=True, help_text="The report's id on calmac.org")
    cedars_key = models.CharField(
        "CEDARS record",
        max_length=120,
        null=True,
        blank=True,
        unique=True,
        help_text="<collection>:<key> of the CEDARS document (or the 2006-2013 EM&V site's file, emv:edw/...) it was migrated from",
    )

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
    def migrated_from(self) -> str:
        """Where a document from cedars_import came from: CEDARS, or the 2006-2013 EM&V site (keys emv:edw/...)."""
        if not self.cedars_key:
            return ""
        return "The 2006-2013 EM&V site" if self.cedars_key.startswith("emv:edw/") else "CEDARS"

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
        "accounts.Organisation", on_delete=models.PROTECT, related_name="+", help_text="The author's organization"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    stage = models.CharField(max_length=20, choices=Stage.choices, blank=True, help_text="A deliverable's revision stage (BR-15.11)")
    stage_number = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Draft 1, 2 ... of a draft stage; revision 1, 2 ... of a final; a PA's response 1, 2 ..."
    )
    responding_pa = models.ForeignKey(
        "accounts.Organisation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The PA whose response to the recommendations this is",
    )

    class Meta:
        ordering = ["document", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "number"], name="documents_version_number_unique"),
            models.CheckConstraint(condition=Q(stage="") | Q(stage_number__isnull=False), name="documents_version_stage_numbered"),
            models.CheckConstraint(
                condition=Q(responding_pa__isnull=True) | Q(stage="pa_response"), name="documents_version_response_has_pa_stage"
            ),
        ]

    def __str__(self):
        return f"{self.document} v{self.number}"

    @property
    def published(self) -> bool:
        return self.published_at is not None

    @property
    def stage_label(self) -> str:
        """e.g. "Draft workplan 2", "Final study results, revision 1", "PA response to the recommendations (PGE)"."""
        if not self.stage:
            return ""
        label = Stage(self.stage).label
        if self.stage == Stage.PA_RESPONSE:
            pa = self.responding_pa.code if self.responding_pa_id else ""
            return f"{label} ({pa})" + (f", revision {self.stage_number - 1}" if self.stage_number and self.stage_number > 1 else "")
        if self.stage in (Stage.DRAFT_WORKPLAN, Stage.DRAFT_RESULTS):
            return f"{label} {self.stage_number}"
        if self.stage_number and self.stage_number > 1:
            return f"{label}, revision {self.stage_number - 1}"
        return label


class CommentPeriod(models.Model):
    """The window in which registered users comment on a published version (BR-15.4; the PDA's Comment Start and Comment End). Set by
    whoever posts the version and changed only with a reason (an extension is common), so it lives beside the version, which never
    changes once published."""

    version = models.OneToOneField(DocumentVersion, on_delete=models.CASCADE, related_name="comment_period")
    opens = models.DateField()
    closes = models.DateField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(opens__lte=F("closes")), name="documents_comment_period_in_order")]

    def __str__(self):
        return f"{self.opens} to {self.closes}"

    def open_on(self, day) -> bool:
        return self.opens <= day <= self.closes


class ResponseRequest(models.Model):
    """CPUC ED's request that a PA respond to a deliverable's recommendations by a due date (BR-15.11); the PA's response is a version of
    the deliverable at the stage PA response."""

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="response_requests")
    pa = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="response_requests")
    due = models.DateField()
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document", "pa__code"]
        constraints = [models.UniqueConstraint(fields=["document", "pa"], name="documents_one_request_per_pa")]

    def __str__(self):
        return f"{self.document}: {self.pa.code} by {self.due}"


class Comment(models.Model):
    """A comment or question on a published version, posted in its comment period by a registered user (BR-15.4). Public, dated and
    attributed to its author and organization. Never edited; the author may withdraw it until it is answered (it then stops being shown
    but is kept, with its audit event)."""

    class Kind(models.TextChoices):
        COMMENT = "comment", "Comment"
        QUESTION = "question", "Question"

    version = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, related_name="comments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    subject = models.CharField(max_length=100)
    section = models.CharField(max_length=100, blank=True, help_text="The section or page it is about")
    text = models.TextField(max_length=2000)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="None: migrated from the PDA"
    )
    author_name = models.CharField(max_length=200, help_text="The author's name as shown, kept as it was when posted")
    organisation = models.ForeignKey(
        "accounts.Organisation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The organization it was posted for",
    )
    file_name = models.CharField(max_length=255, blank=True)
    file_key = models.CharField(max_length=500, blank=True)
    file_sha256 = models.CharField(max_length=64, blank=True)
    file_size = models.PositiveBigIntegerField(null=True, blank=True)
    file_type = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    pda_id = models.PositiveIntegerField("PDA comment id", null=True, blank=True, unique=True)

    class Meta:
        ordering = ["version", "created_at", "id"]

    def __str__(self):
        return self.subject

    def get_file_url(self):
        return f"/documents/comments/{self.pk}/{self.file_name}" if self.file_name else ""


class CommentResponse(models.Model):
    """A response to a comment by CPUC ED, a PA on the document or the study's evaluator (BR-15.4). Public and never edited."""

    comment = models.ForeignKey(Comment, on_delete=models.PROTECT, related_name="responses")
    text = models.TextField(max_length=2000)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="None: migrated from the PDA"
    )
    author_name = models.CharField(max_length=200)
    organisation = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["comment", "created_at", "id"]

    def __str__(self):
        return f"Response to {self.comment}"


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
    legacy_name = models.CharField(
        max_length=300, blank=True, help_text="Its name in the PDA's view and download links, when it differs or was wrapped in a zip"
    )

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


class WorkOrder(models.Model):
    """An Energy Division contract work order: the PDA's second classification of EM&V documents beside the attributes (121 of them,
    with tasks), and the PSR's work order number. Kept by CPUC ED."""

    number = models.CharField("work order number", max_length=50, unique=True, help_text="e.g. 23NC0465")
    name = models.CharField(max_length=255, blank=True)
    firm = models.ForeignKey(
        "accounts.Organisation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="work_orders",
        help_text="The contractor, when it has an organization here",
    )
    firm_name = models.CharField(max_length=200, blank=True, help_text="The contractor as the contract names it")
    cpuc_contract_id = models.CharField("CPUC contract id", max_length=50, blank=True)
    cancelled = models.BooleanField(default=False)
    pda_id = models.PositiveIntegerField("PDA contract id", null=True, blank=True, unique=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"{self.number} {self.name}".strip()

    def get_absolute_url(self):
        return f"/documents/work-orders/{self.pk}/"


class LegacyLink(models.Model):
    """An address of the old CEDARS site (its Reports & Documents, DEER Resources and program document links) or of the 2006-2013 EM&V
    site (kept under /edw) and the migrated document or file it now leads to (BR-15.10). The path is kept decoded, as the old sites'
    links resolve, with no host."""

    path = models.CharField(max_length=500, unique=True)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="legacy_links")
    file = models.ForeignKey("DocumentFile", null=True, blank=True, on_delete=models.PROTECT, related_name="legacy_links")

    class Meta:
        ordering = ["path"]

    def __str__(self):
        return self.path


class Task(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="tasks")
    name = models.CharField(max_length=200)
    pda_id = models.PositiveIntegerField("PDA task id", null=True, blank=True, unique=True)

    class Meta:
        ordering = ["work_order", "name"]
        constraints = [models.UniqueConstraint(fields=["work_order", "name"], name="documents_task_name_unique")]

    def __str__(self):
        return f"{self.work_order.number}: {self.name}"


class Study(models.Model):
    """One EM&V study, as the Project Status Report keeps it (BR-15.6): its study ID (year, sector, number) never changes; status,
    type, group, lead and sectors come from value lists CPUC ED keeps, replacing the PSR's free text. Changes go through the service
    with an audit event, which is its status and budget history. Public except the contacts. The study manager's organization and
    CPUC ED update it (BR-15.9)."""

    number = models.PositiveIntegerField(unique=True, help_text="The PSR project number, continued for new studies")
    study_id = models.CharField("study ID", max_length=80, unique=True, help_text="<year>_<sector>_<number>, as the PSR gives it")
    title = models.CharField(max_length=255)
    work_order = models.ForeignKey(WorkOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="studies")
    description = models.TextField(blank=True, max_length=1000)
    objective = models.TextField(blank=True, max_length=500)
    research_questions = models.TextField(blank=True, max_length=1500)
    methods = models.TextField("potential EM&V methods", blank=True, max_length=250)
    study_type = models.ForeignKey(AttributeOption, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    sectors = models.ManyToManyField(AttributeOption, blank=True, related_name="+")
    group = models.ForeignKey(
        AttributeOption, null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="The PSR's group or sub-sector"
    )
    lead = models.ForeignKey(AttributeOption, on_delete=models.PROTECT, related_name="+", help_text="CPUC ED, IOU or REN")
    manager = models.ForeignKey(
        "accounts.Organisation",
        on_delete=models.PROTECT,
        related_name="managed_studies",
        help_text="The study manager (an evaluation contractor or a PA); its members with Documents: submit update it",
    )
    funding_cycles = models.ManyToManyField(AttributeOption, blank=True, related_name="+")
    budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.ForeignKey(AttributeOption, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    next_activity = models.CharField(max_length=255, blank=True)
    next_activity_due = models.DateField(null=True, blank=True)
    completion_date = models.DateField(null=True, blank=True)
    calmac_id = models.CharField("CALMAC ID", max_length=40, blank=True)
    notes = models.TextField(blank=True, max_length=500)
    # contacts: not public (BR-15.6)
    ed_program_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="ED's program manager"
    )
    contacts = models.TextField(
        blank=True, help_text="ED consultants, PA contacts, the PCG and its coordinators; CPUC staff and the study manager see them"
    )
    psr = models.JSONField(
        "kept from the PSR",
        default=dict,
        blank=True,
        help_text="A migrated study's PSR values that the register's lists did not take as they were; seen with the contacts",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-number"]
        verbose_name_plural = "studies"

    def __str__(self):
        return f"{self.study_id} {self.title}"

    def get_absolute_url(self):
        return f"/documents/studies/{self.number}/"


class StudyProgram(models.Model):
    """A program a study evaluates, in whole or in part, and the program years it covers (BR-15.7), so the program's page lists the
    study."""

    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="coverage")
    program = models.ForeignKey("registry.Program", on_delete=models.PROTECT, related_name="evaluations")
    first_year = models.PositiveSmallIntegerField()
    last_year = models.PositiveSmallIntegerField()
    whole = models.BooleanField(default=True, help_text="The whole program; otherwise a part of it, named in the note")
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["program__prg_id"]
        constraints = [
            models.UniqueConstraint(fields=["study", "program"], name="documents_study_program_unique"),
            models.CheckConstraint(condition=Q(first_year__lte=F("last_year")), name="documents_coverage_years_in_order"),
        ]

    def __str__(self):
        return f"{self.study.study_id}: {self.program.prg_id} {self.first_year}-{self.last_year}"

    @property
    def years(self) -> str:
        return str(self.first_year) if self.first_year == self.last_year else f"{self.first_year} to {self.last_year}"


class FileText(models.Model):
    """The words of a document file, read when it is stored, for searching by text (BR-15.2). Kept beside the file, which never changes;
    `vector` is PostgreSQL's full-text index of it."""

    file = models.OneToOneField(DocumentFile, on_delete=models.CASCADE, related_name="text")
    text = models.TextField(blank=True)
    vector = SearchVectorField(null=True)
    error = models.CharField(max_length=300, blank=True, help_text="Why no text could be read (a scanned PDF has none)")
    extracted_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [GinIndex(fields=["vector"], name="documents_filetext_vector")]

    def __str__(self):
        return f"Text of {self.file}"


class DocumentSubscription(models.Model):
    """A user's wish to hear when a matching document is published, a comment period opens or a study's status changes (BR-15.5):
    for one collection, one attribute value, one work order or one study. Delivered through the notification service."""

    class Channel(models.TextChoices):
        IN_APP = "in_app", "On the notifications page"
        EMAIL = "email", "On the notifications page and by email"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="document_subscriptions")
    collection = models.CharField(max_length=10, choices=Collection.choices, blank=True)
    option = models.ForeignKey(AttributeOption, null=True, blank=True, on_delete=models.CASCADE, related_name="subscriptions")
    work_order = models.ForeignKey("WorkOrder", null=True, blank=True, on_delete=models.CASCADE, related_name="subscriptions")
    study = models.ForeignKey("Study", null=True, blank=True, on_delete=models.CASCADE, related_name="subscriptions")
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.EMAIL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user", "collection", "option", "work_order", "study"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (~Q(collection="") & Q(option__isnull=True) & Q(work_order__isnull=True) & Q(study__isnull=True))
                    | (Q(collection="") & Q(option__isnull=False) & Q(work_order__isnull=True) & Q(study__isnull=True))
                    | (Q(collection="") & Q(option__isnull=True) & Q(work_order__isnull=False) & Q(study__isnull=True))
                    | (Q(collection="") & Q(option__isnull=True) & Q(work_order__isnull=True) & Q(study__isnull=False))
                ),
                name="documents_subscription_one_target",
            ),
            models.UniqueConstraint(
                fields=["user", "collection", "option", "work_order", "study"], name="documents_one_subscription", nulls_distinct=False
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.target_label}"

    @property
    def target_label(self) -> str:
        if self.collection:
            return f"Collection: {Collection(self.collection).label}"
        if self.option_id:
            return f"{self.option.attribute_list.name}: {self.option.label}"
        if self.work_order_id:
            return f"Work order {self.work_order.number}"
        return f"Study {self.study.study_id}"


class KeyDate(models.Model):
    """A key date or event CPUC ED posts on the library's home page (BR-15.3): a workshop, a comment deadline, a report release."""

    on = models.DateField("date")
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True, max_length=1000)
    link = models.URLField(blank=True, max_length=400)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    posted_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True, help_text="Taken off the home page (kept, with its audit event)")

    class Meta:
        ordering = ["on", "id"]

    def __str__(self):
        return f"{self.on}: {self.title}"
