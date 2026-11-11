from django.apps import AppConfig


class DataConfig(AppConfig):
    """The Data, dashboards and downloads module (phase 6, decision H): queries over the publication snapshots' releases
    (`query.py`, DuckDB over Parquet, decision I), the metric catalog every page and download uses (`metrics.py`), and stamped
    downloads (`downloads.py`). The dashboard pages build on these."""

    name = "cedars.modules.data"
    label = "data"
    verbose_name = "Data, dashboards and downloads"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import jobs

        register_purger("data_download", jobs.expired)

        from cedars.core import yourwork as your_work

        from . import yourwork

        # Your work on the Published data page: the person's saved views and record downloads
        your_work.register("saved_views", yourwork.saved_views, order=60, pages=("published",))
        your_work.register("downloads", yourwork.downloads, order=65, pages=("published",))
