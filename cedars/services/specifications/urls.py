from django.urls import path

from . import editor_views, views

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
    # the editor (CPUC system administrators): a draft copied from a published version, a page per part of it
    path("<slug:code>/<slug:slug>/new-draft", editor_views.start, name="start_draft"),
    path("<slug:code>/<slug:slug>/edit/", editor_views.edit, name="edit"),
    path("<slug:code>/<slug:slug>/edit/settings/", editor_views.edit_settings, name="edit_settings"),
    path("<slug:code>/<slug:slug>/edit/rules/", editor_views.edit_rules, name="edit_rules"),
    path("<slug:code>/<slug:slug>/edit/checks/", editor_views.edit_checks, name="edit_checks"),
    path("<slug:code>/<slug:slug>/edit/changes/", editor_views.edit_changes, name="edit_changes"),
    path("<slug:code>/<slug:slug>/edit/publish/", editor_views.edit_publish, name="edit_publish"),
    path("<slug:code>/<slug:slug>/edit/discard", editor_views.discard, name="discard"),
    path("<slug:code>/<slug:slug>/edit/tables/<str:table>/", editor_views.edit_table, name="edit_table"),
    path("<slug:code>/<slug:slug>/edit/tables/<str:table>/add/", editor_views.add_field, name="add_field"),
    path("<slug:code>/<slug:slug>/edit/tables/<str:table>/fields/<str:field>/", editor_views.edit_field, name="edit_field"),
    path("<slug:code>/<slug:slug>/edit/tables/<str:table>/fields/<str:field>/remove", editor_views.remove_field, name="remove_field"),
    path("<slug:code>/<slug:slug>/edit/tables/<str:table>/fields/<str:field>/rename", editor_views.rename_field, name="rename_field"),
]
