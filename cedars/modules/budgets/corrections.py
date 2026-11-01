"""The Budgets module's record editor (BR-02.4), for the submissions service's corrections: a version's records are its job's two
tables, Measure and ProgramCost, whole (a budget filing has no earlier records to carry in: each version is the whole filing), with the
file's own columns. A correction is kept as the version's new job and checked again (service.correct_records): the module's checks on
every record, the budget filing rules on the records it changed."""

from django.urls import reverse

from cedars.core import storage
from cedars.services.submissions import corrections

from . import jobfile, service
from .models import PROGRAM_COST_FIELDS


class BudgetsEditor(corrections.FileTables):
    module = service.MODULE
    label = "Budgets"
    table_names = jobfile.TABLES
    keys = service.KEYS
    required = jobfile.REQUIRED
    numbers = {
        "Measure": ["NumUnits", *service.INCENTIVE_FIELDS],
        "ProgramCost": [f for f, _ in PROGRAM_COST_FIELDS] + ["OnBillFinancing"],
    }
    descriptions = {"ProgramCost": dict(PROGRAM_COST_FIELDS)}

    def files(self, v):
        f = service.job_file(v)
        if f is None:
            return "", {}
        tables, _ = jobfile.read_upload(storage.store("uploads").get_bytes(f["key"]), "job.zip")
        return f["sha256"], tables

    def parse(self, content):
        return jobfile.rows(content)

    def read_tables(self, data, filename):
        return jobfile.read_tables(data, filename)

    def table_of(self, name):
        return jobfile.table_of(name)

    def prepare(self, v):
        service.ensure_checked(v)

    def refusal(self, v):
        d = v.submission.deadline
        if d.module != service.MODULE or not hasattr(d, "budget_setup"):
            return "That is not a budget filing."
        return ""

    def store(self, actor, v, tables, scope):
        return service.correct_records(actor, v, tables, scope)

    def findings(self, v):
        return service.findings(v)

    def version_page(self, v):
        return reverse("budgets:filing", args=[v.submission.deadline_id]) + f"?org={v.submission.organisation.code}"
