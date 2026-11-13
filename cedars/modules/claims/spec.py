"""The claims tables as a filing's specification version lays them out: each table's fields and their requirements and types, the names
an upload may give a table or a column, each table's key, the tables whose rows for a claim are one set, the columns CEDARS fills in, and
the PII tables, which are the version's vault tables and never travel with the claim records.

A filing reads its own version, on any date (`for_filing`: specifications.layout_for_filing); the module's code holds no layout of its
own. The Quarterly claims specification's starting version is CEDARS' 2026 Claims specification (cedars.cpuc.ca.gov/claims/specification/,
six tables) with the fuel-substitution infrastructure cost, claim site and withdrawal tables, each field under its canonical name
(ReCET's renamed dictionary) with CEDARS' name accepted on upload."""

from __future__ import annotations

from cedars.services.specifications import service as specifications
from cedars.services.specifications.service import FieldLayout, VersionLayout

MODULE = "claims"


class Layout:
    """The claim tables of a specification version (its tables that are not vault tables), with what the module asks of them."""

    def __init__(self, version: VersionLayout):
        self.version_layout = version
        self.version = version.version
        claim = [t for t in version.tables if not t.vault]
        self._tables = {t.name: t for t in claim}
        self.vault = tuple(t for t in version.tables if t.vault)
        self.TABLES = tuple(t.name for t in claim)  # in upload order
        self.KEYS = {t.name: t.keys for t in claim}
        self.BY_CLAIM = tuple(t.name for t in claim if t.replace == "claim")  # a later upload's rows for a claim replace all earlier ones
        self.SYSTEM_FIELDS = {t.name: tuple(t.system_fields()) for t in claim if t.system_fields()}  # an uploaded value is replaced

    def table(self, name: str):
        return self._tables[name]

    def fields(self, table: str) -> tuple[FieldLayout, ...]:
        return self._tables[table].fields

    def columns(self, table: str) -> list[str]:
        return self._tables[table].columns()

    def upload_columns(self, table: str) -> list[str]:
        """The columns a PA fills in: the template's, less the ones CEDARS fills in."""
        return self._tables[table].upload_columns()

    def required(self, table: str) -> list[str]:
        return self._tables[table].required_fields()

    def field(self, table: str, name: str) -> FieldLayout | None:
        return self._tables[table].field(name)

    def canonical(self, table: str, header: str) -> str | None:
        """The canonical name of an uploaded column, or None when the table has no such field (ignored, not rejected)."""
        return self._tables[table].canonical(header)

    def table_name(self, name: str) -> str | None:
        return self.version_layout.table_name(name, vault=False)

    def is_pii_table(self, name: str) -> bool:
        """A file or sheet of one of the vault's tables (PII_Contact, PII_Site): never part of a claims upload."""
        return self.version_layout.table_name(name, vault=True) is not None


def _layout(version: VersionLayout) -> Layout:
    made = version.__dict__.get("_claims_layout")
    if made is None:
        made = Layout(version)
        object.__setattr__(version, "_claims_layout", made)  # a frozen version's layout is kept, and this with it
    return made


def for_filing(deadline) -> Layout:
    """The claim tables a claims filing follows: its specification version's, on any date."""
    return _layout(specifications.layout_for_filing(deadline, MODULE))


def current() -> Layout:
    """The claim tables of the current Quarterly claims version (a new filing's, and the pre-upload checker's)."""
    return _layout(specifications.layout_for_module(MODULE))


def of_version(version) -> Layout:
    """The claim tables of a given specification version."""
    return _layout(specifications.layout(version))
