from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    """The Documents & EM&V module (BR-15): one library for PA documents, CPUC staff guidance, DEER resources, program files and EM&V
    deliverables, replacing CEDARS' Reports & Documents and DEER Resources pages and the Energy Division's Public Document Area (PDA).
    Every document has a permanent link and numbered versions; published ones are public; every download is an audit event.
    The study register (`studies.py`) replaces the Energy Division's Project Status Report (PSR)."""

    name = "cedars.modules.documents"
    label = "documents"
    verbose_name = "Documents"

    def ready(self):
        from cedars.services.registry.service import register_evaluations

        from . import studies

        register_evaluations(studies.evaluations)  # each program's page lists the studies that evaluate it (BR-15.7)

        from cedars.services.notifications.service import register_concerns

        from . import subscriptions

        register_concerns("documents.documentversion", subscriptions.notice)  # the PAs' and programs' followers (BR-09.2)

        from cedars.services.registry.service import register_program_records

        from . import program_page

        register_program_records("documents", program_page.documents)  # and the documents that name it (BR-07.6)

        from . import health

        health.register()

        from cedars.core import queue as my_queue

        from . import queue

        # each user's queue (BR-09.5): documents to publish, responses owed, questions to answer
        my_queue.register("document_draft", "Documents to publish", queue.drafts, action="Review and publish", order=80)
        my_queue.register("document_response", "Responses to recommendations", queue.responses, action="Respond", order=85)
        my_queue.register("document_question", "Questions to answer", queue.questions, action="Answer", order=90)
