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
from .test_calculations import analyst  # noqa: F401 - fixture
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
    problems, release = builder.check(d)
    assert release == engine_release()
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
