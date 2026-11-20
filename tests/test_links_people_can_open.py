"""A link is shown only to people the page it leads to lets in (the 1920 px review): a value list's draft, the publication scan
settings, a load shape's upload, the page a tester's feedback came from."""

from cedars.services.feedback.views import _page_opens


def test_a_testers_own_run_page_is_named_without_a_link():
    assert not _page_opens("/testing/runs/1/PAF-002/")
    assert not _page_opens("/testing/runs/1/")
    assert _page_opens("/programs/?year=2028")
    assert not _page_opens("/no/such/page/")


def test_a_value_list_draft_is_linked_only_for_its_editors(client, signed_in, cpuc_admin, pge_admin, monkeypatch):
    from cedars.services.dictionary import value_lists

    monkeypatch.setattr(value_lists, "can_edit", lambda user: user == cpuc_admin)
    signed_in(pge_admin)
    assert client.get("/dictionary/lists/").context["can_edit"] is False
    signed_in(cpuc_admin)
    assert client.get("/dictionary/lists/").context["can_edit"] is True
