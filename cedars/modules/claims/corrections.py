"""The Claims module's record editor (BR-02.4), for the submissions service's corrections: a version's own records are its upload's
(the claims new or changed since the last confirmed quarter); the records filed before it are the year so far, compiled over the earlier
confirmations, and correcting one adds the corrected record to this version, which then stands over it. A claim filed earlier is taken
out of the year by withdrawing it (the Withdrawal table), never by deleting its record."""

from functools import cache

from django.urls import reverse

from cedars.services.accounts.service import AccessDenied
from cedars.services.submissions import corrections
from cedars.services.submissions.corrections import FieldDef, Rows, TableDef

from . import service, spec, upload


@cache
def _tables() -> tuple[TableDef, ...]:
    out = []
    for name in spec.TABLES:
        system = spec.SYSTEM_FIELDS.get(name, ())
        fields = tuple(
            FieldDef(f.name, spec.TYPE_LABELS[f.type], f.required, f.description) for f in spec.fields()[name] if f.name not in system
        )
        group = ("ClaimID",) if name in spec.BY_CLAIM else ()
        note = (
            "A claim's records in this table are one set: this version's set for a claim replaces the earlier one whole."
            if name in spec.BY_CLAIM
            else ""
        )
        out.append(TableDef(name, spec.KEYS[name], fields, group, note))
    return tuple(out)


def _rows(tables: dict[str, upload.Table]) -> dict[str, Rows]:
    return {name: Rows(name, list(t.columns), [dict(r) for r in t.rows]) for name, t in tables.items()}


class ClaimsEditor(corrections.Editor):
    module = service.MODULE
    label = "Claims"
    earlier_delete = (
        "it was filed in an earlier confirmation, so it is not this upload's to delete. To take the claim out of the year, name it in the "
        "Withdrawal table with the reason."
    )

    def tables(self, v):
        return list(_tables())

    def rows(self, v):
        return _rows(service.own_tables(v))

    def earlier(self, v):
        base = service.base_for(v.submission.organisation, v.submission.deadline)
        if base is None:
            return "", {}
        tables = {n: t for n, t in service.compiled_tables(base).items() if n != "Withdrawal"}  # a claim is withdrawn once
        return f"the {service.label(base.submission.deadline)} (version {base.number})", _rows(tables)

    def prepare(self, v):
        service.ensure_checked(v)

    def refusal(self, v):
        d = v.submission.deadline
        if d.module != service.MODULE or d.kind not in service.KINDS:
            return "That is not a claims filing."
        try:
            service._require_in_order(v.submission.organisation, d)
        except AccessDenied as e:
            return str(e)
        if service.stale(v):
            return "An earlier confirmation has changed since this version was compiled on it. Check it again first, on the period's page."
        if not v.findings:
            return "Check the version first, on the period's page."
        return ""

    def table_of(self, name):
        return spec.table_name(name)

    def read_upload(self, data, filename):
        tables = {name: upload.without_system_fields(t) for name, t in upload.read(data, filename).items() if t.rows}
        return _rows(tables)

    def save(self, actor, v, tables, scope):
        return service.correct_records(actor, v, tables, scope)

    def findings(self, v):
        return service.findings(v)

    def version_page(self, v):
        return reverse("claims:period", args=[v.submission.deadline_id]) + f"?org={v.submission.organisation.code}"
