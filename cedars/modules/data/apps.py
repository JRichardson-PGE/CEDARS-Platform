from django.apps import AppConfig


class DataConfig(AppConfig):
    """The Data, dashboards and downloads module (phase 6, BR-07, decision H): queries over the publication snapshots' releases
    (`query.py`, DuckDB over Parquet, decision I), the metric catalog every page and download uses (`metrics.py`), and stamped
    downloads (`downloads.py`). The dashboard pages build on these."""

    name = "cedars.modules.data"
    label = "data"
    verbose_name = "Data, dashboards and downloads"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import jobs

        register_purger("data_download", jobs.expired)
