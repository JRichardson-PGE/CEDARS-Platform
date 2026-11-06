"""The cost-test set builder (refdata/builder.py; design "Cost-test set builder: scope and design", Jake 2026-11-03): every signed-in user
builds sets for their own analysis runs, the CPUC registers them for everyone. The set is kept as its YAML mapping, every save is checked
by the engine's validator and its messages filed by step, and a set saved page by page unchanged writes a file the engine reads as the
same set."""

import copy

import pytest
import yaml
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from cedars.services import audit
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import builder, cost_tests
from cedars.services.refdata import builder_forms as bf
from cedars.services.refdata.models import CostTestBuild, PackVersion

from .conftest import page_only, sign_in
from .test_calculations import analyst, release  # noqa: F401 - fixtures
from .test_cost_test_sets import registered, set_file

pytestmark = pytest.mark.django_db
engine = pytest.importorskip("recet.cost_test_sets")
needs_catalog = pytest.mark.skipif(not hasattr(engine, "engine_streams"), reason="the engine here is older than ReCET 0.1.92")
EE = engine.SETS_DIR / "EE-2026" / engine.DEFINITION


def same_set(a, b):
    """Two CostTestSets are the same set: every part equal (not the file's hash, where it came from, or its version)."""
    for f in ("id", "workstream", "label", "description", "draft", "years_from", "years_to", "pricings", "streams", "aggregates", "tests"):
        assert getattr(a, f) == getattr(b, f), f


def loaded(build):
    data = builder.to_yaml(build.data)
    return engine.parse(yaml.safe_load(data), "x", "builder")


# --- the file


def test_the_file_round_trips_and_reads_like_reccets_own():
    data, aside = builder.read_file(EE.read_bytes())
    assert aside == {} and data == yaml.safe_load(EE.read_text(encoding="utf-8"))
    out = builder.to_yaml(data)
    assert yaml.safe_load(out) == data
    text = out.decode()
    assert text.startswith("# EE-2026: EE cost tests 2026\n# Written by the CEDARS cost-test set builder")
    assert "program_years: {from: null, to: null}" in text and "pricings: [primary, sct_base, sct_high]" in text
    assert "description: >-\n" in text and "version: '2026-10-31'" in text
    same_set(loaded(type("B", (), {"data": data})), engine.builtin()["EE-2026"])


def test_an_uploaded_file_keeps_what_it_cannot_use_aside():
    d = yaml.safe_load(EE.read_text(encoding="utf-8"))
    d["colour"] = "blue"
    d["streams"]["ElecBen"]["weight"] = 2
    raw = yaml.safe_dump(d, sort_keys=False).replace("version: '2026-10-31'", "version: 2026-10-31").encode()
    data, aside = builder.read_file(raw)
    assert aside == {"colour": "blue", "streams.ElecBen.weight": 2}
    assert data["version"] == "2026-10-31" and "weight" not in data["streams"]["ElecBen"]
    with pytest.raises(builder.BuildError, match="mapping"):
        builder.read_file(b"- a\n- list\n")
    with pytest.raises(builder.BuildError, match="readable"):
        builder.read_file(b"id: [unclosed\n")


@needs_catalog
def test_every_validator_message_lands_on_a_step():
    d = yaml.safe_load(EE.read_text(encoding="utf-8"))
    d["id"] = "ee"
    d["pricings"]["primary"]["acc_rows"] = "nope"
    d["streams"]["ElecBen"]["kind"] = "cost"
    d["streams"]["OtherBen"]["basis"] = "monthly"
    d["aggregates"]["TotalSystemBenefit"]["plus"].append("Nope")
    d["tests"]["PAC"]["program_costs"] = {"share": "RIM"}
    problems, checked_by = builder.check(d)
    assert checked_by == engine_release()
    assert list(problems) == ["set", "pricings", "engine", "inputs", "aggregates", "tests"]
    assert sum(len(v) for v in problems.values()) == len(engine.validate(d)) and all(problems.values())
    assert any("OtherBen.basis" in m for m in problems["inputs"]) and any("ElecBen.kind" in m for m in problems["engine"])


def engine_release():
    import recet

    return recet.__version__


# --- starting


def test_starting_from_a_pack_a_build_the_engine_a_file_or_nothing(cpuc_admin, analyst, pge):  # noqa: F811
    pack = registered(cpuc_admin, publish=False)
    b = builder.start_from_pack(analyst, pack)
    assert b.parent == pack and b.data["version"] != pack.version and b.source == "a copy of EE-2026 2026-09-15"
    assert b.problems  # the test file is a header and tests only: the engine says what it lacks
    e = builder.start_from_engine(analyst, "EE-2026", organisation=pge)
    assert e.problems == {} and e.organisation == pge and e.checked_by == engine_release()
    c = builder.start_from_build(analyst, e)
    assert c.parent_build == e and c.data["streams"] == e.data["streams"]
    f = builder.start_from_file(analyst, EE.read_bytes(), "mine.yaml")
    assert f.data["version"] == "2026-10-31" and f.source == "the file mine.yaml"
    blank = builder.start_blank(analyst)
    assert blank.data["pricings"]["primary"]["acc_rows"] == "trc" and "tests" in blank.problems
    events = audit.models.AuditEvent.objects.filter(action="cost_test_build.started")
    assert events.count() == 5


def test_who_sees_and_changes_a_build(cpuc_admin, analyst, pge_admin, pge, make_user):  # noqa: F811
    own = builder.start_from_engine(analyst, "EE-2026")
    shared = builder.start_from_engine(analyst, "EE-2026", organisation=pge)
    other = make_user("someone@example.org")
    assert set(builder.visible(analyst)) == {own, shared}
    assert set(builder.visible(other)) == set() and not builder.can_change(other, own)
    with pytest.raises(AccessDenied):
        builder.set_engine_streams(other, own, {})
    from cedars.services.accounts import service as access

    access.grant(pge_admin, other, pge, "calculations.submit")
    assert set(builder.visible(other)) == {shared} and builder.can_change(other, shared)
    with pytest.raises(AccessDenied):
        builder.discard(other, shared)  # only its starter (or the organization's administrator) discards it
    builder.discard(analyst, own)
    assert own not in builder.visible(analyst)
    with pytest.raises(AccessDenied):
        builder._start(other, {}, source="x", organisation=make_org(cpuc_admin))


def make_org(cpuc_admin):
    from .conftest import create_pge

    return type(create_pge(cpuc_admin))(code="ZZZ", name="Elsewhere", kind="pa")


# --- the steps


def post_all(c, build):
    """Save every page of the build with what it shows (each form's initial values), as someone clicking Save on each would."""
    build.refresh_from_db()
    h = bf.HeaderForm.initial_for(build)
    data = {k: ("" if v is None else v) for k, v in h.items() if k not in ("draft", "share")}
    if h["draft"]:
        data["draft"] = "on"
    assert c.post(f"/reference-data/cost-tests/build/{build.pk}/set/", data).status_code == 302
    pricings = build.data["pricings"]
    data = {"p-TOTAL_FORMS": len(pricings), "p-INITIAL_FORMS": len(pricings), "new-acc_rows": "trc"}
    data.update({"new-discount_rate_quarterly": "DiscountRateQtr", "new-discount_rate_annual": "DiscountRateAnnual"})
    for i, (pid, p) in enumerate(pricings.items()):
        data[f"p-{i}-id"] = pid
        data.update({f"p-{i}-{k}": p.get(k, "") for k in builder.PRICING_KEYS})
    assert c.post(f"/reference-data/cost-tests/build/{build.pk}/pricings/", data).status_code == 302
    build.refresh_from_db()
    rows = builder.engine_rows(build.data)
    data = {}
    for r in rows:
        n = r["name"]
        if r["included"]:
            data[f"include_{n}"] = "on"
        data[f"pricings_{n}"] = [p for p in r["pricings"] if p in r["can"]]
        data[f"unit_{n}"], data[f"definition_{n}"] = r["unit"], r["definition"]
    assert c.post(f"/reference-data/cost-tests/build/{build.pk}/engine/", data).status_code == 302
    build.refresh_from_db()
    for name, s in builder.input_streams(build.data):
        i = bf.InputStreamForm.initial_for(name, s, [])
        data = {k: v for k, v in i.items() if v not in (None, [], "")}
        assert c.post(f"/reference-data/cost-tests/build/{build.pk}/inputs/{name}/", data).status_code == 302, name
    build.refresh_from_db()
    for name, a in list((build.data.get("aggregates") or {}).items()):
        data = {k: v for k, v in bf.AggregateForm.initial_for(name, a).items() if v not in (None, [], "", False)}
        assert c.post(f"/reference-data/cost-tests/build/{build.pk}/aggregates/{name}/", data).status_code == 302, name
    build.refresh_from_db()
    for tid, t in list((build.data.get("tests") or {}).items()):
        data = {k: v for k, v in bf.TestForm.initial_for(tid, t).items() if v not in (None, [], False)}
        data["levelized"] = list(t.get("levelized") or [])  # "" (the test's cost) is a value, not a blank
        assert c.post(f"/reference-data/cost-tests/build/{build.pk}/tests/{tid}/", data).status_code == 302, tid
    build.refresh_from_db()
    return build


@needs_catalog
def test_saving_every_page_unchanged_gives_back_the_same_set(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    c = Client()
    sign_in(c, analyst)
    b = post_all(c, b)
    assert b.problems == {}
    same_set(loaded(b), engine.builtin()["EE-2026"])
    assert engine.describe(loaded(b)).split("\n")[2:] == engine.describe(engine.builtin()["EE-2026"]).split("\n")[2:]
    assert not audit.models.AuditEvent.objects.filter(action="cost_test_build.changed").exists()  # nothing changed, nothing recorded


@needs_catalog
def test_a_neb_built_through_the_forms(analyst):  # noqa: F811
    """ReCET's synthetic EE-2026-neb, as far as steps 1 to 4 go: the id, and an annual health NEB from a new column in the primary and SCT
    Base pricings."""
    b = builder.start_from_engine(analyst, "EE-2026")
    c = Client()
    sign_in(c, analyst)
    h = {k: ("" if v is None else v) for k, v in bf.HeaderForm.initial_for(b).items() if k not in ("draft", "share")}
    h.update(tail="neb", label="With a health NEB")
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/set/", {**h, "then": "next"})["Location"].endswith("/pricings/")
    neb = {"name": "HealthNEB", "kind": "benefit", "new_columns": "UnitNEBHealth", "basis": "annual_over_life", "ntg": "kwh",
           "market_effects": "benefits", "pricings": ["primary", "sct_base"], "naming": "suffix", "gross": "divide_ntg",
           "unit": "$ (PV)", "definition": "Health benefits."}  # fmt: skip
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/inputs/new/", neb).status_code == 302
    b.refresh_from_db()
    assert b.data["id"] == "EE-2026-neb" and b.problems == {}
    assert list(b.data["streams"]).index("HealthNEB") == list(b.data["streams"]).index("TaxCredits") + 1
    s = loaded(b)
    assert s.column("HealthNEB", "sct_base") == "HealthNEB_SB" and s.input_columns()[-1] == "UnitNEBHealth"
    ev = audit.models.AuditEvent.objects.filter(action="cost_test_build.changed").order_by("pk")
    assert [list(e.after) for e in ev] == [["set"], ["inputs"]]
    # renamed: the stream keeps its place and its new name is used wherever the set names it
    b.data["tests"]["TRC"]["benefits"].append("HealthNEB")
    b.save()
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/inputs/HealthNEB/", {**neb, "name": "NEBHealth"}).status_code == 302
    b.refresh_from_db()
    assert "NEBHealth" in b.data["tests"]["TRC"]["benefits"] and "HealthNEB" not in b.data["streams"]
    # taken out, the test that reads it shows the problem on its step
    c.post(f"/reference-data/cost-tests/build/{b.pk}/inputs/", {"remove": "NEBHealth"})
    b.refresh_from_db()
    assert "NEBHealth" not in b.data["streams"] and any("NEBHealth" in m for m in b.problems["tests"])


@needs_catalog
def test_engine_streams_take_the_engines_facts_and_only_its_pricings(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    rows = {r["name"]: r for r in builder.engine_rows(b.data)}
    assert rows["LifecycleBillReducElec"]["can"] == ["primary"] and "sct_base" in rows["LifecycleBillReducElec"]["cannot"]
    assert not rows["NetParticipantCostPV"]["included"]  # in the engine's catalog, left out of EE-2026
    choices = {n: {"include": r["included"], "pricings": r["pricings"], "unit": r["unit"], "definition": r["definition"]}
               for n, r in rows.items()}  # fmt: skip
    choices["NetParticipantCostPV"] = {"include": True, "pricings": ["primary", "sct_high"], "unit": "$", "definition": "Net."}
    choices["WaterEnergyCost"]["include"] = False
    b.data["streams"]["ElecBen"]["kind"] = "cost"  # a hand-edited file: the engine's facts come back on save
    builder.set_engine_streams(analyst, b, choices)
    names = list(b.data["streams"])
    assert "WaterEnergyCost" not in names and names.index("NetParticipantCostPV") == names.index("TaxCreditsGrossPV") + 1
    assert b.data["streams"]["NetParticipantCostPV"]["naming"] == "participant_suffix" and b.data["streams"]["ElecBen"]["kind"] == "benefit"
    assert any("WaterEnergyCost" in m for m in b.problems["aggregates"]) and any("WaterEnergyCost" in m for m in b.problems["tests"])


def test_a_pricing_in_use_stays(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    rows = [{"id": p, **v} for p, v in b.data["pricings"].items()]
    rows[1]["remove"] = True
    with pytest.raises(builder.BuildError, match="sct_base is still used by stream ElecBen"):
        builder.set_pricings(analyst, b, rows)
    new = {"id": "own", "label": "Mine", "acc_rows": "trc", "discount_rate_quarterly": "DiscountRateQtr_SCT",
           "discount_rate_annual": "DiscountRateAnnual_SCT", "suffix": "_OWN", "participant_suffix": ""}  # fmt: skip
    rows[1]["remove"] = False
    builder.set_pricings(analyst, b, rows, new)
    assert list(b.data["pricings"]) == ["primary", "sct_base", "sct_high", "own"] and b.problems == {}
    with pytest.raises(builder.BuildError, match="already has a pricing own"):
        builder.set_pricings(analyst, b, [{"id": p, **v} for p, v in b.data["pricings"].items()], new)


def test_the_header_builds_the_id(analyst, pge):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    builder.set_header(analyst, b, workstream="iqp", first_year=2028, tail="", label="IQP tests", description="", version="v1",
                       draft=True, years_from=2028, years_to=None, share=True, organisation=pge)  # fmt: skip
    assert b.data["id"] == "IQP-2028" and b.data["workstream"] == "IQP" and b.organisation == pge
    assert list(b.data)[:7] == ["schema_version", "id", "workstream", "version", "label", "description", "draft"]
    with pytest.raises(builder.BuildError, match="workstream"):
        builder.set_header(analyst, b, workstream="2X", first_year=2028, tail="", label="x", description="", version="v1",
                           draft=False, years_from=None, years_to=None, share=False)  # fmt: skip


# --- without the engine


def test_without_the_engine_only_the_header_is_checked(analyst, monkeypatch):  # noqa: F811
    monkeypatch.setattr(builder, "engine", lambda: None)
    b = builder.start_from_file(analyst, EE.read_bytes(), "ee.yaml")
    assert b.problems == {} and b.checked_by == ""
    builder.set_header(analyst, b, workstream="EE", first_year=2028, tail="", label="", description="", version="v2",
                       draft=False, years_from=None, years_to=None, share=False)  # fmt: skip
    assert b.problems == {"set": ["label: text is required"]}
    c = Client()
    sign_in(c, analyst)
    page = c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode()
    assert "engine is not installed on this server" in page and 'id="words-h"' not in page


# --- registering


def test_only_the_cpuc_registers_and_a_registered_set_stays(cpuc_admin, analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    builder.set_header(analyst, b, workstream="EE", first_year=2028, tail="", label="EE cost tests 2028", description="",
                       version="2026-11-03", draft=False, years_from=2028, years_to=None, share=False)  # fmt: skip
    with pytest.raises(AccessDenied, match="CPUC"):
        builder.register(analyst, b)
    pack = builder.register(cpuc_admin, b, notes="from the builder")
    b.refresh_from_db()
    assert b.status == "registered" and b.pack == pack and pack.status == "draft" and pack.label == "EE-2028"
    assert cost_tests.file_bytes(pack) == builder.to_yaml(b.data) and pack.notes == "from the builder"
    with pytest.raises(AccessDenied, match="Copy it"):
        builder.set_header(analyst, b, workstream="EE", first_year=2028, tail="", label="x", description="", version="v",
                           draft=False, years_from=None, years_to=None, share=False)  # fmt: skip
    again = builder.start_from_build(analyst, b)
    again.data["version"] = "2026-11-03"
    again.problems, _ = builder.check(again.data)
    assert any("already registered" in m for m in again.problems["set"])
    again.save()
    with pytest.raises(builder.BuildError, match="problems"):
        builder.register(cpuc_admin, again)
    assert audit.models.AuditEvent.objects.filter(action="cost_test_build.registered").count() == 1


def test_the_engines_own_set_is_registered_only_as_its_own_file(cpuc_admin):
    b = builder.start_from_engine(cpuc_admin, "EE-2026")
    b.data["version"] = "2026-10-31"  # the engine ships EE-2026 2026-10-31; the builder's file is not byte for byte the engine's
    b.problems = {}
    b.save()
    with pytest.raises(AccessDenied, match="engine ships"):
        builder.register(cpuc_admin, b)
    assert b.status == "building" and not PackVersion.objects.filter(label="EE-2026").exists()


# --- the pages


def test_the_pages(cpuc_admin, analyst):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    page = c.get("/reference-data/cost-tests/").content.decode()
    assert "Build a set" in page and "My sets" in page
    r = c.post("/reference-data/cost-tests/build/", {"start": "file", "file": SimpleUploadedFile("mine.yaml", EE.read_bytes())})
    b = CostTestBuild.objects.get()
    assert r["Location"] == f"/reference-data/cost-tests/build/{b.pk}/set/"
    for step in builder.PAGES:
        assert c.get(f"/reference-data/cost-tests/build/{b.pk}/{step}/").status_code == 200, step
    assert c.get(f"/reference-data/cost-tests/build/{b.pk}/inputs/OtherBen/").status_code == 200
    assert c.get(f"/reference-data/cost-tests/build/{b.pk}/inputs/ElecBen/").status_code == 404
    review = c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode()
    assert "Register as a draft pack" not in page_only(review) and "TRCBen  =" in review
    f = c.get(f"/reference-data/cost-tests/build/{b.pk}/definition.yaml")
    assert f.content == builder.to_yaml(b.data) and 'filename="EE-2026_2026-10-31.yaml"' in f["Content-Disposition"]
    r = c.post("/reference-data/cost-tests/build/", {"start": "pack"})
    assert "Choose one to start from" in r.content.decode()
    other = Client()
    sign_in(other, cpuc_admin)
    assert other.get(f"/reference-data/cost-tests/build/{b.pk}/set/").status_code == 404  # not shared: the CPUC does not see it either
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/action/", {"action": "copy"}).status_code == 302
    assert CostTestBuild.objects.count() == 2
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/action/", {"action": "discard"})["Location"] == "/reference-data/cost-tests/"
    assert CostTestBuild.objects.get(pk=b.pk).status == "discarded"


def test_a_manager_registers_from_review(cpuc_admin):
    b = builder.start_from_engine(cpuc_admin, "EE-2026")
    b.data = copy.deepcopy(b.data) | {"id": "EE-2028", "program_years": {"from": 2028, "to": None}}
    b.problems, _ = builder.check(b.data)
    b.save()
    c = Client()
    sign_in(c, cpuc_admin)
    assert "Register as a draft pack" in c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode()
    r = c.post(f"/reference-data/cost-tests/build/{b.pk}/action/", {"action": "register", "notes": ""})
    pack = PackVersion.objects.get(kind="cost_tests", label="EE-2028")
    assert r["Location"] == f"/reference-data/{pack.pk}/"
    assert set_file  # the cost-test set fixtures stay importable here


# --- aggregates and tests (steps 5 and 6), what changed, the engine check


def jst(**kw):
    d = {"id": "JST", "label": "Just transition", "pricing": "primary", "benefits": ["ElecBen"], "numerator_adders": [], "costs":
         ["RebatesandIncentsPV"], "share": "TRC", "variants": ["NoAdmin"], "lifecycle_net_benefit": False, "levelized": [""],
         "headline": False, "filing": False, "definition": "A test of our own."}  # fmt: skip
    return {**d, **kw}


@needs_catalog
def test_tests_are_added_renamed_moved_and_removed(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    builder.put_test(analyst, b, None, jst())
    assert list(b.data["tests"])[-1] == "JST" and b.problems == {}
    t = b.data["tests"]["JST"]
    assert t["program_costs"] == {"share": "TRC"} and t["filing"] is False and t["levelized"] == [""] and "headline" not in t
    builder.move_test(analyst, b, "JST", -1)
    assert list(b.data["tests"])[-2:] == ["JST", "PCT"]
    builder.put_test(analyst, b, "TRC", {**bf.TestForm.initial_for("TRC", b.data["tests"]["TRC"]), "id": "TRCX"})
    assert list(b.data["tests"])[0] == "TRCX" and b.data["tests"]["PAC"]["program_costs"] == {"share": "TRCX"}
    assert b.data["tests"]["JST"]["program_costs"] == {"share": "TRCX"} and b.problems == {}
    builder.remove_test(analyst, b, "TRCX")
    assert sum("program_costs.share" in m for m in b.problems["tests"]) == 3  # PAC, RIM and JST shared its program costs
    with pytest.raises(builder.BuildError, match="capital"):
        builder.put_test(analyst, b, None, jst(id="jst"))
    with pytest.raises(builder.BuildError, match="already"):
        builder.put_test(analyst, b, None, jst(id="PAC"))
    assert [c for c, _ in builder.share_choices(b.data, "JST")] == ["", "positive_benefits", "SCB", "SCH"]


@needs_catalog
def test_an_aggregate_through_the_forms(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    c = Client()
    sign_in(c, analyst)
    url = f"/reference-data/cost-tests/build/{b.pk}/aggregates/new/"
    data = {
        "name": "NetElec",
        "pricing": "primary",
        "plus": ["ElecBen"],
        "minus": ["ElecSupplyCost"],
        "unit": "$ (PV)",
        "definition": "Net.",
    }
    assert c.post(url, data).status_code == 302
    b.refresh_from_db()
    assert list(b.data["aggregates"]) == ["TotalSystemBenefit", "NetElec"] and b.problems == {}
    assert b.data["aggregates"]["NetElec"] == {"pricing": "primary", "plus": ["ElecBen"], "minus": ["ElecSupplyCost"], "unit": "$ (PV)",
                                               "definition": "Net."}  # fmt: skip
    assert (
        c.post(url, {**data, "name": "SocialElec", "pricing": "sct_base", "plus": ["RebatesandIncentsPV"], "minus": []}).status_code == 302
    )
    b.refresh_from_db()
    assert b.problems == {}  # RebatesandIncentsPV is read in sct_base
    c.post(f"/reference-data/cost-tests/build/{b.pk}/aggregates/", {"remove": "SocialElec"})
    b.refresh_from_db()
    assert list(b.data["aggregates"]) == ["TotalSystemBenefit", "NetElec"]
    page = page_only(c.get(f"/reference-data/cost-tests/build/{b.pk}/aggregates/NetElec/").content.decode())
    assert "ElecBen (primary, sct_base, sct_high)" in page


@needs_catalog
def test_the_tests_page_orders_and_changes_tests(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    c = Client()
    sign_in(c, analyst)
    url = f"/reference-data/cost-tests/build/{b.pk}/tests/"
    assert c.post(url, {"down": "TRC"}).status_code == 302
    b.refresh_from_db()
    assert list(b.data["tests"])[:2] == ["PAC", "TRC"]
    page = page_only(c.get(url).content.decode())
    assert 'name="up" value="PAC"' not in page and 'name="down" value="PCT"' not in page
    r = c.post(f"{url}new/", {**{k: v for k, v in jst().items() if v not in ([], False)}, "share": "positive_benefits"})
    assert r.status_code == 302
    b.refresh_from_db()
    assert b.data["tests"]["JST"]["program_costs"] == {"share": "positive_benefits"} and b.problems == {}


@needs_catalog
def test_review_lists_what_changed_since_the_copy(cpuc_admin, analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    assert builder.changes(b) == []
    builder.put_test(analyst, b, None, jst())
    builder.move_test(analyst, b, "PCT", -1)
    builder.remove_aggregate(analyst, b, "TotalSystemBenefit")
    choices = {r["name"]: {"include": r["included"], "pricings": r["pricings"], "unit": r["unit"], "definition": r["definition"]}
               for r in builder.engine_rows(b.data)}  # fmt: skip
    choices["GasBen"]["definition"] = "Changed."
    builder.set_engine_streams(analyst, b, choices)
    got = {(x["part"], x["name"], x["what"], tuple(x["keys"])) for x in builder.changes(b)}
    assert got == {("Test", "JST", "added", ()), ("Test", "order", "changed", ()), ("Aggregate", "TotalSystemBenefit", "removed", ()),
                   ("Stream", "GasBen", "changed", ("definition",))}  # fmt: skip
    c = Client()
    sign_in(c, analyst)
    page = page_only(c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode())
    assert "What changed" in page and "TotalSystemBenefit" in page and "removed" in page
    assert builder.changes(builder.start_blank(analyst)) == []


@needs_catalog
def test_registering_records_the_engine_check_at_once(cpuc_admin):
    b = builder.start_from_engine(cpuc_admin, "EE-2026")
    builder.put_test(cpuc_admin, b, None, jst())
    pack = builder.register(cpuc_admin, b)
    check = pack.tie_outs.get(name="engine_check")
    assert check.passed and check.engine_release == engine_release() and "7 tests" in check.summary
    from cedars.services.refdata import service as refdata

    assert refdata.outstanding(pack) == [] and refdata.publish_pack(cpuc_admin, pack).status == "published"


def test_a_registered_file_the_engine_refuses_fails_its_check_and_can_be_checked_again(cpuc_admin):
    pack = registered(cpuc_admin, publish=False)  # a header-and-tests file: the engine lacks its pricings and streams
    if not cost_tests.engine_installed():
        assert not pack.tie_outs.exists()
        return
    check = pack.tie_outs.get(name="engine_check")
    assert not check.passed and "refuses" in check.summary and check.report["errors"]
    c = Client()
    sign_in(c, cpuc_admin)
    assert "Check it with the engine again" in page_only(c.get(f"/reference-data/{pack.pk}/").content.decode())
    assert c.post(f"/reference-data/{pack.pk}/", {"action": "engine_check"}).status_code == 302
    assert pack.tie_outs.filter(name="engine_check").count() == 2


def test_the_ee_set_from_the_engine_passes_its_check_as_the_engines_own_file(cpuc_admin):
    data = cost_tests.engine_copy("EE-2026", "2026-10-31")
    if data is None:
        pytest.skip("the engine is not installed here")
    pack = cost_tests.register_set(cpuc_admin, data)
    check = pack.tie_outs.get(name="engine_check")
    assert check.passed and "byte for byte" in check.summary


# --- offering a set to the CPUC, and New run's "My cost-test sets"


def test_a_set_is_offered_declined_with_a_reason_and_registered(cpuc_admin, analyst):  # noqa: F811
    from cedars.services.notifications.models import Notification

    b = builder.start_from_engine(analyst, "EE-2026")
    builder.put_test(analyst, b, None, jst())
    assert b not in builder.visible(cpuc_admin)  # private until it is offered
    with pytest.raises(AccessDenied):
        builder.decline(analyst, b, reason="x")
    with django_capture_on_commit():
        b = builder.offer(analyst, b, note="A JST test for everyone")
    assert b.status == "offered" and b in builder.visible(cpuc_admin) and list(builder.offered()) == [b]
    assert b not in builder.mine(cpuc_admin)
    with pytest.raises(builder.BuildError, match="reason"):
        builder.decline(cpuc_admin, b, reason=" ")
    with django_capture_on_commit():
        builder.decline(cpuc_admin, b, reason="JST needs the working group's sign-off first.")
    b.refresh_from_db()
    assert b.status == "building" and b.declined_reason.startswith("JST") and b not in builder.visible(cpuc_admin)
    n = Notification.objects.get(user=analyst)
    assert "declined" in n.title and n.body.startswith("JST") and n.link.endswith(f"/build/{b.pk}/review/")
    with django_capture_on_commit():
        builder.offer(analyst, b)
        builder.withdraw_offer(analyst, b)
        builder.offer(analyst, b)
        pack = builder.register(cpuc_admin, b)
    assert Notification.objects.filter(user=analyst, title__contains="registered").count() == 1
    assert pack.label == "EE-2026" and set(
        audit.models.AuditEvent.objects.filter(action__startswith="cost_test_build.").values_list("action", flat=True)
    ) >= {"cost_test_build.offered", "cost_test_build.declined", "cost_test_build.offer_withdrawn", "cost_test_build.registered"}


def django_capture_on_commit():
    """Run on_commit callbacks at once (the tests run inside one transaction)."""
    from unittest import mock

    from django.db import transaction

    return mock.patch.object(transaction, "on_commit", lambda f, *a, **k: f())


def test_offering_and_declining_on_the_pages(cpuc_admin, analyst):  # noqa: F811
    b = builder.start_from_file(analyst, EE.read_bytes(), "ee.yaml")
    b.data["version"] = "v-offer"
    b.save()
    c, m = Client(), Client()
    sign_in(c, analyst)
    sign_in(m, cpuc_admin)
    page = page_only(c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode())
    assert "Offer it to the CPUC" in page
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/action/", {"action": "offer", "note": "Please"}).status_code == 302
    assert "Withdraw the offer" in page_only(c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode())
    sets = page_only(m.get("/reference-data/cost-tests/").content.decode())
    assert "Offered to the CPUC" in sets and "Please" in sets
    review = page_only(m.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode())
    assert "Decline it" in review and "Register as a draft pack" in review
    r = m.post(f"/reference-data/cost-tests/build/{b.pk}/action/", {"action": "decline", "reason": "Not now."})
    assert r.status_code == 302 and r.url == "/reference-data/cost-tests/"
    b.refresh_from_db()
    assert b.status == "building" and "declined it" in c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode()
    assert "Offered to the CPUC" not in page_only(c.get("/reference-data/cost-tests/").content.decode())  # managers only


@needs_catalog
def test_new_run_prices_one_of_my_sets_as_its_file(tmp_path, analyst, release):  # noqa: F811
    from cedars.modules.calculations.models import Run

    from .test_calculation_run_options import post_new

    b = builder.start_from_engine(analyst, "EE-2026")
    builder.set_header(analyst, b, workstream="EE", first_year=2026, tail="mine", label="Mine", description="", version="v1",
                       draft=False, years_from=None, years_to=None, share=False)  # fmt: skip
    builder.put_test(analyst, b, None, jst())
    c = Client()
    sign_in(c, analyst)
    page = c.get("/calculations/new/").content.decode()
    assert 'name="cost_test_build"' in page and "EE-2026-mine v1 (Mine)" in page
    r = post_new(c, tmp_path, cost_test_build=str(b.pk))
    assert r.status_code == 302, r.content.decode()[:2000]
    run = Run.objects.get()
    data = builder.to_yaml(b.data)
    assert run.cost_tests["status"] == "custom" and run.cost_tests["build"] == b.pk and run.cost_tests["file"] == builder.file_name(b)
    assert storage_bytes(run.inputs["cost_tests"]["key"]) == data
    assert "from the set builder" in c.get(f"/calculations/runs/{run.pk}/").content.decode()
    builder.put_test(analyst, b, None, jst(id="BAD", pricing="nope"))
    r = post_new(c, tmp_path, cost_test_build=str(b.pk))
    assert r.status_code == 200 and "fix them on its Review step" in r.content.decode()
    assert Run.objects.count() == 1


def storage_bytes(key):
    from cedars.core import storage

    return storage.store("uploads").get_bytes(key)


# ---------- The formula bar (Jake 2026-11-06: "I like the clickable prototype, lets make that change") ----------


@needs_catalog
def test_every_published_test_reads_back_from_its_formula():
    from cedars.services.refdata import formula

    data = yaml.safe_load(EE.read_text())
    streams = [n for n, s in builder.readable_streams(data)]
    shares = [v for v, _ in builder.share_choices(data, None) if v not in ("", "positive_benefits")]
    assert formula.render("PAC", data["tests"]["PAC"]).endswith("+ share(TRC) x ProgramCosts)")
    for tid, t in data["tests"].items():
        p = formula.parse(formula.render(tid, t), streams, shares)
        pc = t.get("program_costs")
        assert p.problems == [] and p.test == tid, tid
        assert (p.benefits, p.numerator_adders, p.costs) == (t["benefits"], t.get("numerator_adders") or [], t["costs"]), tid
        assert p.share == (pc.get("share") if isinstance(pc, dict) else ""), tid


def test_a_formula_says_what_is_wrong_with_it():
    from cedars.services.refdata.formula import parse

    names = ["ElecBen", "GasBen", "TaxCredits", "ElecSupplyCost", "OtherCost"]

    def problems(text, shares=("TRC",)):
        return " | ".join(parse(text, names, shares).problems)

    p = parse("(ElecBen + GasBen + adder(TaxCredits)) / (ElecSupplyCost + share × ProgramCosts)", names)
    assert (p.test, p.benefits, p.numerator_adders, p.costs, p.share) == ("", ["ElecBen", "GasBen"], ["TaxCredits"], ["ElecSupplyCost"],
                                                                         "positive_benefits")  # fmt: skip
    assert parse("X = (ElecBen) / (OtherCost + share(TRC) * ProgramCosts)", names, ["TRC"]).share == "TRC"
    assert "Did you mean ElecBen? Names are written with their capitals" in problems("(elecben) / (OtherCost)")
    assert "Did you mean ElecSupplyCost?" in problems("(ElecBen) / (ElecSuplyCost)")
    assert "engine change" in problems("(ElecBen + 0.5 x GasBen) / (OtherCost)")
    assert "engine change" in problems("(ElecBen - GasBen) / (OtherCost)")
    assert "below the line" in problems("(ElecBen + share x ProgramCosts) / (OtherCost)")
    assert "taken through a share" in problems("(ElecBen) / (ProgramCosts)")
    assert "PAC does not compute its own share" in problems("(ElecBen) / (OtherCost + share(PAC) x ProgramCosts)")
    assert "above the line" in problems("(ElecBen) / (OtherCost + adder(TaxCredits))")
    assert "named twice in the benefits" in problems("(ElecBen + ElecBen) / (OtherCost)")
    assert "brackets" in problems("(ElecBen / (OtherCost)")
    assert "one /" in problems("ElecBen + OtherCost")
    assert "at least one cost" in problems("(ElecBen) / ()")
    assert parse("(ElecBen) / (OtherCost + ElecBen)", names).costs == ["OtherCost", "ElecBen"]  # once on each side is fine


@needs_catalog
def test_a_test_typed_as_a_formula(analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    c = Client()
    sign_in(c, analyst)
    url = f"/reference-data/cost-tests/build/{b.pk}/tests/new/"
    base = {"id": "JST", "label": "Just transition", "pricing": "primary", "filing": "on"}
    # the formula alone, with no list ticked, makes the test
    r = c.post(url, {**base, "formula": "JST = (ElecBen + adder(TaxCredits)) / (RebatesandIncentsPV + share(TRC) x ProgramCosts)"})
    assert r.status_code == 302
    b.refresh_from_db()
    t = b.data["tests"]["JST"]
    assert (t["benefits"], t["numerator_adders"], t["costs"], t["program_costs"]) == (
        ["ElecBen"], ["TaxCredits"], ["RebatesandIncentsPV"], {"share": "TRC"})  # fmt: skip
    assert b.problems == {}
    # a formula with a problem is not saved, and says why
    page = page_only(c.post(url, {**base, "id": "JS2", "formula": "JS2 = (ElecBen x 2) / (RebatesandIncentsPV)"}).content.decode())
    assert "engine change" in page and "JS2" not in b.data["tests"]
    page = page_only(c.post(url, {**base, "id": "JS2", "formula": "JS3 = (ElecBen) / (RebatesandIncentsPV)"}).content.decode())
    assert "the test&#x27;s id is JS2" in page
    # neither a formula nor a list: both are asked for
    page = page_only(c.post(url, {**base, "id": "JS2"}).content.decode())
    assert "tick one, or write the formula" in page
    # the page shows the saved formula; left as it is, the ticked lists decide
    edit = f"/reference-data/cost-tests/build/{b.pk}/tests/JST/"
    page = page_only(c.get(edit).content.decode())
    shown = "JST = (ElecBen + adder(TaxCredits)) / (RebatesandIncentsPV + share(TRC) x ProgramCosts)"
    assert shown in page
    r = c.post(edit, {**base, "formula": shown, "benefits": ["ElecBen", "GasBen"], "numerator_adders": ["TaxCredits"],
                      "costs": ["RebatesandIncentsPV"], "share": "TRC"})  # fmt: skip
    assert r.status_code == 302
    b.refresh_from_db()
    assert b.data["tests"]["JST"]["benefits"] == ["ElecBen", "GasBen"]
    # the tests page lists each test as its formula
    page = page_only(c.get(f"/reference-data/cost-tests/build/{b.pk}/tests/").content.decode())
    assert "JST = (ElecBen + GasBen + adder(TaxCredits)) / (RebatesandIncentsPV + share(TRC) x ProgramCosts)" in page


def test_the_review_grid_puts_each_stream_in_each_test():
    from cedars.services.refdata.formula import grid

    data = {
        "streams": {
            "ElecBen": {"source": "engine", "kind": "benefit", "pricings": ["primary"]},
            "Rebates": {"source": "engine", "kind": "cost", "pricings": ["primary"]},
            "TaxCredits": {"source": "engine", "kind": "benefit", "pricings": ["primary"]},
            "Unused": {"source": "input", "kind": "benefit", "pricings": ["primary"]},
            "ProgramCosts": {"source": "program", "kind": "cost"},
        },
        "tests": {
            "TRC": {"label": "Total", "pricing": "primary", "benefits": ["ElecBen"], "numerator_adders": ["TaxCredits"],
                    "costs": ["Rebates"], "program_costs": {"share": "positive_benefits"}, "headline": True},
            "PCT": {"label": "Participant", "pricing": "primary", "benefits": ["Rebates"], "costs": ["Rebates"], "program_costs": "none"},
        },
    }  # fmt: skip
    g = grid(data)
    assert [c["id"] for c in g["tests"]] == ["TRC", "PCT"] and g["tests"][0]["headline"]
    assert [r["name"] for r in g["rows"]] == ["ElecBen", "TaxCredits", "Rebates"]  # benefits first, then costs
    roles = {r["name"]: [[x["key"] for x in c["roles"]] for c in r["cells"]] for r in g["rows"]}
    assert roles == {"ElecBen": [["benefit"], []], "TaxCredits": [["adder"], []], "Rebates": [["cost"], ["benefit", "cost"]]}
    assert [r["name"] for r in g["unused"]] == ["Unused"]
    assert [c["text"] for c in g["program"]["cells"]] == ["its own share", "none"]
    assert g["tests"][1]["formula"] == "PCT = (Rebates) / (Rebates)"


@needs_catalog
def test_review_and_the_published_set_show_how_the_tests_are_built(cpuc_admin, analyst):  # noqa: F811
    b = builder.start_from_engine(analyst, "EE-2026")
    c = Client()
    sign_in(c, analyst)
    neb = {"name": "HealthNEB", "kind": "benefit", "new_columns": "UnitNEBHealth", "basis": "one_time", "ntg": "kwh",
           "market_effects": "benefits", "pricings": ["primary"], "naming": "none", "gross": "divide_ntg", "unit": "$",
           "definition": "Health benefits."}  # fmt: skip
    assert c.post(f"/reference-data/cost-tests/build/{b.pk}/inputs/new/", neb).status_code == 302
    page = page_only(c.get(f"/reference-data/cost-tests/build/{b.pk}/review/").content.decode())
    assert "How the tests are built" in page and "Streams no test uses (1)" in page and "<code>HealthNEB</code>:" in page
    assert '<span class="role role-benefit">Benefit</span> and <span class="role role-cost">Cost</span>' in page  # PCT's rebates
    assert "PAC = (ElecBen + GasBen + WaterEnergyBen + OtherBen) / (" in page
    p = cost_tests.register_set(cpuc_admin, EE.read_bytes(), source="test")
    sign_in(c, cpuc_admin)
    page = page_only(c.get(f"/reference-data/{p.pk}/").content.decode())
    assert "How the tests are built" in page and "Streams no test uses" not in page
    assert "TRC = (ElecBen + GasBen + WaterEnergyBen + OtherBen + adder(TaxCredits)) / (" in page


def test_start_from_is_a_list_that_shows_only_the_next_field_it_needs(cpuc_admin, analyst):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    page = c.get("/reference-data/cost-tests/build/").content.decode()
    assert '<select name="start"' in page and '<option value="" selected>Choose what to start from</option>' in page
    assert 'type="radio"' not in page
    assert '<option value="build">' not in page  # no sets of their own yet: not offered
    # nothing chosen: every follow-on field and Start are hidden, Next is shown (without scripts it shows the chosen field)
    assert '<div data-start-for="pack" hidden>' in page and '<div data-start-for="file" hidden>' in page
    assert 'data-start-for="any" hidden>Start</button>' in page and 'name="next" value="1" data-start-next>' in page
    assert "refdata/builder_start.js" in page
    # Next with a file chosen: only the file field and Start show, nothing is checked yet
    page = c.post("/reference-data/cost-tests/build/", {"start": "file", "next": "1"}).content.decode()
    assert '<div data-start-for="file">' in page and '<div data-start-for="pack" hidden>' in page
    assert '<option value="file" selected>' in page and "Choose one to start from" not in page
    assert 'data-start-for="any">Start</button>' in page and "data-start-next hidden" in page
    assert not CostTestBuild.objects.exists()
    # a choice in the address works the same (a link straight to it)
    assert '<div data-start-for="pack">' in c.get("/reference-data/cost-tests/build/?start=pack").content.decode()


def test_each_step_goes_on_to_the_next_one(cpuc_admin, analyst):  # noqa: F811
    import re

    c = Client()
    sign_in(c, analyst)
    c.post("/reference-data/cost-tests/build/", {"start": "file", "file": SimpleUploadedFile("mine.yaml", EE.read_bytes())})
    b = CostTestBuild.objects.get()
    links = {}
    for step, after in zip(builder.PAGES, builder.PAGES[1:], strict=False):
        page = c.get(f"/reference-data/cost-tests/build/{b.pk}/{step}/").content.decode()
        for target, label in re.findall(r'<a href="/reference-data/cost-tests/build/\d+/(\w+)/">Go on to ([^<]+)</a>', page):
            assert (target, label) == (after, builder.STEP_LABELS[after]), step
            links[step] = target
    assert links["inputs"] == "aggregates" and links["aggregates"] == "tests" and links["tests"] == "review"
