from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    """The Documents & EM&V module (BR-15): one library for PA documents, CPUC staff guidance, DEER resources, program files and EM&V
    deliverables, replacing CEDARS' Reports & Documents and DEER Resources pages and the Energy Division's Public Document Area (PDA).
    Every document has a permanent link and numbered versions; published ones are public; every download is an audit event."""

    name = "cedars.modules.documents"
    label = "documents"
    verbose_name = "Documents"
