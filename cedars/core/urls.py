from django.urls import path
from django.views.generic import RedirectView

from cedars.modules.data import views as data_views

from . import views

app_name = "core"
urlpatterns = [
    # The front page: what CEDARS is, the program cycle at a glance and Help (Jake, 2026-10-31; it opened on the dashboard before).
    path("", data_views.front_page, name="home"),
    path("about/", views.about, name="about"),
    path("queue/", views.my_queue, name="queue"),
    path("search/", views.search, name="search"),
    # Each header menu's own page, its heading's link (Jake, 2026-11-06). Calculations' runs and Reference data's packs moved under them.
    path("filings/", views.section, {"page": "filings"}, name="filings"),
    path("calculations/", views.section, {"page": "calculations"}, name="calculations"),
    path("published/", views.section, {"page": "published"}, name="published"),
    path("reference-data/", views.section, {"page": "reference"}, name="reference"),
    path("admin/", views.section, {"page": "administration"}, name="administration"),
    path("help/", views.help_index, name="help"),
    path("help/contact/", views.help_contact, name="help_contact"),
    path("help/tour/", views.help_tour, name="help_tour"),
    path("help/faq/", views.help_faq, name="help_faq"),
    path("help/pages/", views.help_pages, name="help_pages"),
    path("help/pages/<path:key>/", views.help_page, name="help_page"),
    # The engine's documents are Calculations' (navigation review, D1); their first address, under Help, redirects.
    path("calculations/engine-docs/", views.recet_docs, name="recet_docs"),
    path("calculations/engine-docs/<slug:slug>/", views.recet_doc, name="recet_doc"),
    path("help/recet/", RedirectView.as_view(pattern_name="core:recet_docs", permanent=True, query_string=True)),
    path("help/recet/<slug:slug>/", RedirectView.as_view(pattern_name="core:recet_doc", permanent=True, query_string=True)),
    path("help/<slug:slug>/", views.help_guide, name="help_guide"),
    path("healthz", views.healthz, name="healthz"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
]
