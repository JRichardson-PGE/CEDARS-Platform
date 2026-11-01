"""Add a measure from eTRM (BR-06.22): the pack's offering keys and packages, the narrowing lookup, the row it fills, and the page."""

import html
import importlib
import io

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.urls import reverse

from cedars.modules.calculations import drafts
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.refdata import etrm
from cedars.services.refdata import service as refdata
from cedars.services.refdata.models import EtrmPackage, EtrmPermutation

from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures
from .test_claims_deemed import ANY, DEEMED, DI, UPSTREAM, download
from .test_edit_inputs import edit_zip, web  # noqa: F401 - fixtures

ed = pytest.importorskip("recet.editor", reason="Edit inputs uses the ReCET engine's editor code")
pytestmark = pytest.mark.django_db

CZ02 = DEEMED.replace("-CZ01-", "-CZ02-")
OLD = "SWOLD001-01-A-NR-DnDeemed-Res-Any-Ex-Any-CZ03-Res-Default-Def-GSIA-Any-Any"


def extra_rows():
    return [
        {"Measure Detail ID": ANY, "Delivery Type": "DnDeemed", "Building Type": "Any", "Building HVAC": "Any"},
        {"Measure Detail ID": CZ02, "Delivery Type": "DnDeemed", "Building Location": "CZ02", "First Baseline - Electric Savings": "70.1"},
        {
            "Measure Detail ID": OLD,
            "Measure Version ID": "SWOLD001-01",
            "Statewide Measure ID": "SWOLD001",
            "Measure Name": "Retired furnace",
            "Offering Description": "Old furnace",
            "Sector": "Res",
            "Building Location": "CZ03",
            "Effective Start Date": "2019-01-01",
            "End Date": "2021-12-31",
        },
    ]


@pytest.fixture
def pack(cpuc_admin, tmp_path):
    p = download(tmp_path, extra=extra_rows())
    return refdata.publish_pack(cpuc_admin, etrm.load(cpuc_admin, [p], label="eTRM published permutations", version="1"))


def test_loading_keeps_each_permutations_offering_keys_and_each_package(pack):
    p = EtrmPermutation.objects.get(pack=pack, key=CZ02.lower())
    assert (p.offering, p.meas_app_type, p.delivery_type, p.sector, p.bldg_type, p.bldg_vint, p.bldg_hvac, p.bldg_loc) == (
        "A",
        "NR",
        "DnDeemed",
        "Com",
        "Com",
        "Ex",
        "cWtd",
        "CZ02",
    )
    assert (p.ntg_id, p.gsia_id, p.pa_type, p.pa) == ("Com-Default>2yrs", "Def-GSIA", "Any", "Any")
    pkgs = {k.label: k for k in EtrmPackage.objects.filter(pack=pack)}
    assert set(pkgs) == {"SWHC052-02", "SWOLD001-01"}
    k = pkgs["SWHC052-02"]
    assert k.name == "Air-Cooled Chiller, Path B" and k.use_category == "HVAC" and k.permutations == 5
    assert k.offerings == {"A": "Air-cooled screw chiller, path B < 150 tons, tier 1 (10.4 EER, 17.7 IPLV)"}
    assert str(k.start_date) == "2023-01-01" and k.end_date is None  # one permutation ended in 2024; the rest are open
    old = pkgs["SWOLD001-01"]
    assert old.permutations == 1 and str(old.end_date) == "2021-12-31" and old.offerings == {"A": "Old furnace"}


def test_keys_missing_from_a_download_come_from_the_measure_detail_id():
    keys = etrm.offering_keys(
        "SWHC052-02-B-NR-DnDeemed-Com-Com-Ex-cWtd-CZ01-Com-Default>2yrs-Def-GSIA-IOU-PGE", "swhc052-02", {"Sector": "Com"}
    )
    assert (keys["offering"], keys["pa_type"], keys["pa"], keys["sector"], keys["ntg_id"]) == ("B", "IOU", "PGE", "Com", "")


def test_packages_are_found_by_id_or_name_and_by_the_year_in_effect(pack):
    assert [k.label for k in etrm.packages(pack, "", 2028)] == ["SWHC052-02"]
    assert [k.label for k in etrm.packages(pack, "", None)] == ["SWHC052-02", "SWOLD001-01"]
    assert [k.label for k in etrm.packages(pack, "furnace", 2020)] == ["SWOLD001-01"]
    assert [k.label for k in etrm.packages(pack, "swhc", None)] == ["SWHC052-02"]


def test_each_choice_lists_only_the_values_left_and_one_value_is_chosen_for_you(pack):
    n = etrm.narrow(pack, "SWHC052-02", {})
    assert [(s.field, s.value, s.only) for s in n.steps] == [
        ("offering", "A", True),
        ("meas_app_type", "NR", True),
        ("delivery_type", "", False),
    ]
    assert n.open_step.options == ["DnDeemDI", "DnDeemed", "UpDeemed"] and n.permutation is None

    n = etrm.narrow(pack, "SWHC052-02", {"delivery_type": "DnDeemed"})
    assert n.open_step.field == "bldg_type" and n.open_step.options == ["Any", "Com"]  # the Any permutation and the rest

    n = etrm.narrow(pack, "SWHC052-02", {"delivery_type": "DnDeemed", "bldg_type": "Com"})
    assert n.open_step.field == "bldg_loc" and n.open_step.options == ["CZ01", "CZ02"]
    assert {s.field: s.value for s in n.steps[:-1]}["bldg_hvac"] == "cWtd"

    n = etrm.narrow(pack, "SWHC052-02", {"delivery_type": "DnDeemed", "bldg_type": "Com", "bldg_loc": "CZ02"})
    assert n.open_step is None and n.permutation.measure_detail_id == CZ02 and n.matches == 1

    # change an earlier choice: the later ones are kept where they still apply
    n = etrm.narrow(pack, "SWHC052-02", {"delivery_type": "DnDeemDI", "bldg_type": "Com", "bldg_loc": "CZ02"})
    assert n.permutation.measure_detail_id == DI
    # a value that is not among the choices leaves that step open
    n = etrm.narrow(pack, "SWHC052-02", {"delivery_type": "Nope"})
    assert n.open_step.field == "delivery_type"
    with pytest.raises(ValueError):
        etrm.choices(pack, "SWHC052-02", "sector", {})


def test_a_permutation_fills_the_cet_measure_fields_and_leaves_any_for_the_person(pack):
    up = etrm.lookup(pack, [UPSTREAM])[UPSTREAM.lower()]
    fill, left = etrm.measure_row(up, ed.MEASURE_COLUMNS)
    assert fill["MeasureID"] == UPSTREAM and fill["AC_Climate_Zone"] == "1" and fill["Upstream_Flag"] == "1"
    assert fill["UnitkWh1stBaseline"] == "63.3" and fill["EUL_Yrs"] == "20.00" and fill["InstallationRatekWh"] == "1.0000"
    assert fill["DeliveryType"] == "UpDeemed" and fill["AC_Elec_Impact_Profile"] == "DEER:HVAC_Chillers" and fill["NTGRkWh"] == "0.6000"
    assert fill["MeasDescription"].startswith("Air-cooled screw chiller") and fill["SourceDesc"] == "SWHC052-02"
    assert "NumUnits" not in fill and "UnitEndUserRebate" not in fill
    assert ("RUL_ID", "Remaining Useful Life ID", "blank in eTRM") in left

    any_ = etrm.lookup(pack, [ANY])[ANY.lower()]
    fill, left = etrm.measure_row(any_, ed.MEASURE_COLUMNS)
    assert "BldgType" not in fill and ("BldgType", "Building Type", "eTRM allows any value: enter the actual one") in left
    assert etrm.climate_zone("CZ16") == "16" and etrm.climate_zone("IOU") == ""


def test_adding_a_measure_from_etrm(analyst, pge, pack, tmp_path):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Chillers", edit_zip(tmp_path))
    perm = etrm.lookup(pack, [CZ02])[CZ02.lower()]
    with pytest.raises(AccessDenied, match="number of units"):
        drafts.add_from_etrm(analyst, d, perm, "0")
    done = drafts.add_from_etrm(analyst, d, perm, "12.5")
    m = drafts.frame(d, "Measure")
    assert done.index == 3 and len(m) == 4
    r = m.iloc[3]
    assert r["NumUnits"] == "12.5" and r["MeasureID"] == CZ02 and r["AC_Climate_Zone"] == "2" and r["UnitkWh1stBaseline"] == "70.1"
    assert r["PrgID"] == m.at[0, "PrgID"] and r["ClaimYearQuarter"] == m.at[0, "ClaimYearQuarter"]  # from row 1, as Add a measure
    assert r["CEInputID"] not in set(m["CEInputID"][:3]) and r["RealizationRatekWh"] == "1"
    e = AuditEvent.objects.filter(action="calculation_draft.row_added").latest("pk")
    assert e.after["etrm"] == {"pack": pack.pk, "measure_detail_id": CZ02, "units": "12.5"} and "UnitkWh1stBaseline" in e.after["filled"]
    drafts.issues(d)  # the engine's cell checks run on the filled row
    drafts.set_row(analyst, d, "Measure", 3, {"UnitkWh1stBaseline": "65"})  # and every field stays editable
    assert drafts.frame(d, "Measure").at[3, "UnitkWh1stBaseline"] == "65"


def test_the_page_walks_package_offering_and_units(web, analyst, pge, pack, tmp_path):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Chillers", edit_zip(tmp_path))
    url = reverse("calculations:draft_etrm", args=[d.pk])
    assert url in web.get(reverse("calculations:draft", args=[d.pk])).content.decode()

    page = web.get(url).content.decode()
    assert 'value="swhc052-02"' in page and "SWOLD001" not in page  # in effect in row 1's claim year (2028) only
    assert "SWOLD001" in web.get(url, {"year": "all"}).content.decode()

    page = web.get(url, {"package": "swhc052-02"}).content.decode()
    assert "Show the next choice" in page and '<option value="DnDeemDI"' in page and "(the only one)" in page

    keys = {"package": "swhc052-02", "delivery_type": "DnDeemed", "bldg_type": "Com", "bldg_loc": "CZ01"}
    page = web.get(url, keys).content.decode()
    assert html.escape(DEEMED) in page and "Add the measure" in page and "Cap-Tons" in page and "UnitkWh1stBaseline" in page

    r = web.post(url + "?package=swhc052-02", {"measure_detail_id": DEEMED, "units": "3"})
    assert r.status_code == 302 and r["Location"] == reverse("calculations:draft_row", args=[d.pk, "Measure", 4])
    d.refresh_from_db()
    assert drafts.frame(d, "Measure").at[3, "MeasureID"] == DEEMED

    r = web.post(url, {"measure_detail_id": "nope", "units": "3"}, follow=True)
    d.refresh_from_db()
    assert "not in the eTRM pack" in r.content.decode() and len(drafts.frame(d, "Measure")) == 4


def test_the_page_says_when_no_pack_is_loaded(web, analyst, pge, tmp_path):  # noqa: F811
    d = drafts.create_from_zip(analyst, pge, "Chillers", edit_zip(tmp_path))
    page = web.get(reverse("calculations:draft_etrm", args=[d.pk])).content.decode()
    assert "No eTRM permutations pack is loaded yet" in page


def test_a_draft_pack_is_used_until_one_is_published(cpuc_admin, tmp_path):
    draft = etrm.load(cpuc_admin, [download(tmp_path)], label="eTRM published permutations", version="1")
    assert etrm.editor_pack() == draft
    published = refdata.publish_pack(
        cpuc_admin, etrm.load(cpuc_admin, [download(tmp_path)], label="eTRM published permutations", version="2")
    )
    assert etrm.editor_pack() == published


def test_the_migration_fills_packs_loaded_before_it(pack):
    EtrmPermutation.objects.filter(pack=pack).update(**{f: "" for f in etrm.KEY_FIELDS})
    EtrmPackage.objects.filter(pack=pack).delete()
    importlib.import_module("cedars.services.refdata.migrations.0005_etrm_offering_keys").backfill(django_apps, None)
    assert EtrmPermutation.objects.get(pack=pack, key=CZ02.lower()).bldg_loc == "CZ02"
    k = EtrmPackage.objects.get(pack=pack, measure_version_id="swhc052-02")
    assert k.label == "SWHC052-02" and k.permutations == 5 and k.offerings["A"].startswith("Air-cooled")


def test_the_timing_command_loads_a_synthetic_pack_times_it_and_discards_it():
    out = io.StringIO()
    call_command("etrm_lookup_timings", synthetic=3000, packages=12, walks=5, stdout=out)
    text = out.getvalue()
    assert "3,000 permutations in 12 packages" in text and "5 walks over packages" in text and "discarded the synthetic pack" in text
    assert not EtrmPermutation.objects.exists() and not EtrmPackage.objects.exists()
