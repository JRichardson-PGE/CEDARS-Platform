"""The Monthly reports module's record editor (BR-02.4), for the submissions service's corrections: a version's records are its
report's three tables, Commitments and the year-to-date BCA inputs Measure and ProgramCost, whole (each month's report is the whole year
to date), with the file's own columns. A correction is kept as the report's new files and checked again (service.correct_records)."""

from django.urls import reverse

from cedars.core import storage
from cedars.services.submissions import corrections

from . import service
from . import upload as up
from .models import COMMITMENT_FIELDS, INCENTIVE_FIELDS, PROGRAM_COST_FIELDS


class MonthlyEditor(corrections.FileTables):
    module = service.MODULE
    label = "Monthly reports"
    table_names = up.TABLES
    keys = service.KEYS
    required = up.REQUIRED
    numbers = {
        "Commitments": [f for f, _ in COMMITMENT_FIELDS],
        "Measure": ["NumUnits", *INCENTIVE_FIELDS],
        "ProgramCost": [f for f, _ in PROGRAM_COST_FIELDS] + ["OnBillFinancing"],
    }
    descriptions = {"Commitments": dict(COMMITMENT_FIELDS), "ProgramCost": dict(PROGRAM_COST_FIELDS)}

    def files(self, v):
        f = service.report_file(v)
        if f is None:
            return "", {}
        return f["sha256"], up.read_upload(storage.store("uploads").get_bytes(f["key"]), "report.zip")

    def parse(self, content):
        return up.rows(content)

    def read_tables(self, data, filename):
        return up.read_tables(data, filename)

    def table_of(self, name):
        return up.table_of(name)

    def prepare(self, v):
        service.ensure_checked(v)

    def refusal(self, v):
        d = v.submission.deadline
        if d.module != service.MODULE or service.setup_for(d) is None:
            return "That is not a monthly report."
        return ""

    def store(self, actor, v, tables, scope):
        return service.correct_records(actor, v, tables, scope)

    def findings(self, v):
        return service.findings(v)

    def version_page(self, v):
        return reverse("monthly:report", args=[v.submission.deadline_id]) + f"?org={v.submission.organisation.code}"
