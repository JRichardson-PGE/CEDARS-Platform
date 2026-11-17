from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    """The Documents & EM&V module: one library for PA documents, CPUC staff guidance, DEER resources, program files and EM&V
    deliverables, replacing CEDARS' Reports & Documents and DEER Resources pages and the Energy Division's Public Document Area (PDA).
    Every document has a permanent link and numbered versions; published ones are public; every download is an audit event.
    The study register (`studies.py`) replaces the Energy Division's Project Status Report (PSR)."""

    name = "cedars.modules.documents"
    label = "documents"
    verbose_name = "Documents"

    def ready(self):
        from cedars.services.registry.service import register_evaluations

        from . import studies

        register_evaluations(studies.evaluations)  # each program's page lists the studies that evaluate it

        from cedars.services.notifications.service import register_concerns

        from . import subscriptions

        register_concerns("documents.documentversion", subscriptions.notice)  # the PAs' and programs' followers

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("documents", program_page.documents)  # and the documents that name it

        from cedars.services.registry.service import register_document_filing

        register_document_filing(program_page.add_document)  # its "Add a document" button

        from . import health

        health.register()

        from cedars.core import queue as my_queue

        from . import queue

        # each user's queue: documents to publish, responses owed, questions to answer
        my_queue.register("document_draft", "Documents to publish", queue.drafts, action="Review and publish", order=80)
        my_queue.register("document_response", "Responses to recommendations", queue.responses, action="Respond", order=85)
        my_queue.register("document_question", "Questions to answer", queue.questions, action="Answer", order=90)

        from cedars.core import page_access

        from . import views, views_studies

        # who the Help guides offer a link to these pages (UI review 3, U4): the pages' own checks
        page_access.register("documents:new", views.can_file, "people who file documents for the organization they act for")
        page_access.register("documents:aging", lambda r: views_studies._sees_aging(r.user), "the CPUC's documents and studies staff")

        from cedars.core import search as site_search

        from . import search

        site_search.register("documents", "Documents", search.documents, order=80)  # the library's search, under its rules
