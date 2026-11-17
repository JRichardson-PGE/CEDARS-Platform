"""Documents' part of site search (cedars.core.search): the document library's search (service.search), so published documents are
found by everyone and unpublished ones only by those who may read them; a document titled exactly as the query is the best match."""

from urllib.parse import urlencode

from django.urls import reverse

from cedars.core import search

from . import service


def documents(user, q: str):
    qs = service.search(user, service.Search(q=q))
    total = qs.count()
    hits = [
        search.Hit(
            d.title,
            reverse("documents:document", args=[d.pk]),
            search.first_sentence(d.summary),
            d.get_collection_display(),
            search.same(q, d.title),
        )
        for d in qs[: search.PER_PART]
    ]
    more = f"{reverse('documents:library')}?{urlencode({'q': q})}" if total > search.PER_PART else ""
    return search.Part("documents", "Documents", hits, total, more, "See every match in the document library", "Document")
