from django.urls import path

from cedars.modules.data import views as data_views

from . import views

app_name = "core"
urlpatterns = [
    # The front page: what CEDARS is, the program cycle at a glance and Help (Jake, 2026-10-31; it opened on the dashboard before).
    path("", data_views.front_page, name="home"),
    path("about/", views.about, name="about"),
    path("queue/", views.my_queue, name="queue"),
    path("help/", views.help_index, name="help"),
    path("help/contact/", views.help_contact, name="help_contact"),
    path("help/tour/", views.help_tour, name="help_tour"),
    path("help/faq/", views.help_faq, name="help_faq"),
    path("help/pages/", views.help_pages, name="help_pages"),
    path("help/pages/<path:key>/", views.help_page, name="help_page"),
    path("help/recet/", views.recet_docs, name="recet_docs"),
    path("help/recet/<slug:slug>/", views.recet_doc, name="recet_doc"),
    path("help/<slug:slug>/", views.help_guide, name="help_guide"),
    path("healthz", views.healthz, name="healthz"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
]
