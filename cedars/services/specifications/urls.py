from django.urls import path

from . import views

app_name = "specifications"
urlpatterns = [
    path("", views.index, name="index"),
    path("compare/", views.compare, name="compare"),
    path("<slug:code>/current/", views.current, name="current"),
    path("<slug:code>/<slug:slug>/", views.version, name="version"),
    path("<slug:code>/<slug:slug>/template.<str:kind>", views.template, name="template"),
    path("<slug:code>/<slug:slug>/fields-and-rules.xlsx", views.fields, name="fields"),
    path("<slug:code>/<slug:slug>/specification.json", views.document, name="document"),
    path("<slug:code>/<slug:slug>/schema/<str:table>.json", views.schema, name="schema"),
    path("<slug:code>/<slug:slug>/examples/<slug:example>.<str:kind>", views.example, name="example"),
]
