"""What the program page shows from the document library (BR-07.6), through registry.register_program_records: every document that
names the program (implementation plans and other program files, PA documents, EM&V deliverables). A published document is public; one
with nothing published yet is shown only to its owner, the PAs named on it and CPUC staff, as on its own page (BR-15.9)."""

from . import service
from .models import Collection, Document


def _covers(doc: Document, year: int) -> bool:
    first, last = doc.first_year or doc.last_year, doc.last_year or doc.first_year
    return first is None or first <= year <= last


def _placeholder(user, doc: Document) -> bool:
    """Whether the version the reader is served is a placeholder plan (BR-13.6), from the prefetched versions."""
    versions = sorted(doc.versions.all(), key=lambda v: v.number, reverse=True)
    published = [v for v in versions if v.published]
    if published:
        return published[0].placeholder
    return bool(versions) and versions[0].placeholder and service.sees_unpublished(user, doc)


def documents(program, user, year: int | None = None) -> list[dict]:
    """One row per document naming the program that the user may read; year keeps those covering it (or giving no years). Newest
    first."""
    qs = Document.objects.filter(programs=program).select_related("owner", "document_type").prefetch_related("versions")
    if user is None or not user.is_authenticated:
        qs = qs.filter(published_at__isnull=False)
    labels = dict(Collection.choices)
    out = []
    for doc in qs:
        if doc.published_at is None and not service.sees_unpublished(user, doc):
            continue
        if year and not _covers(doc, year):
            continue
        out.append(
            {
                "title": doc.title,
                "url": doc.get_absolute_url(),
                "organisation": doc.owner.code,
                "status": "Published" if doc.published_at else "Not published yet",
                "published": doc.published_at is not None,
                "when": doc.published_at or doc.created_at,
                "collection": labels.get(doc.collection, doc.collection),
                "type": doc.document_type.label,
                "years": doc.years,
                "placeholder": _placeholder(user, doc),
            }
        )
    out.sort(key=lambda r: r["when"], reverse=True)
    return out
