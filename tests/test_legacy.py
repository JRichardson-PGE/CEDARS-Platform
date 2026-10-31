import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from cedars.modules.claims import spec as claims_spec
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import legacy
from cedars.services.dictionary import service as dictionary
from cedars.services.legacy import service
from cedars.services.legacy.models import LegacyImport, LegacySource
from cedars.services.registry import service as registry
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .legacy_fixtures import claim_row, csv_text, manifest_row, manifest_text, write_zip

CATALOG = sorted((Path(__file__).parent.parent / "data" / "dictionary").glob("recet-*.json"))[-1]


# --- the crosswalk


def test_every_published_column_is_mapped_and_none_is_dropped():
    every = legacy.labels()
    for layout, header in legacy.layouts().items():
        assert len(header) == len(set(header)), layout
        cols = legacy.columns(layout)
        assert list(cols) == header
        names = [m.canonical for m in cols.values()]
        assert len(names) == len(set(names)), f"two {layout} labels load under one name"
        assert all(layout in m.layouts for m in cols.values())
    assert {lab for header in legacy.layouts().values() for lab in header} == set(every)
    assert {m.table for m in every.values()} <= set(legacy.TABLES)
    assert all(m.canonical.startswith("Legacy_") == (m.table == "Legacy") for m in every.values())
    assert [len(legacy.layouts()[name]) for name in legacy.LAYOUTS] == [175, 341, 10, 221, 167, 169]


def test_canonical_names_are_the_catalogs_and_the_claims_specs():
    catalog = {f["name"] for f in json.loads(CATALOG.read_text(encoding="utf-8"))["fields"]}
    spec = {(f.table, f.name) for fields in claims_spec.fields().values() for f in fields}
    for m in legacy.labels().values():
        if m.table in ("Measure", "Output", "Program"):
            assert m.canonical in catalog, m
        if m.table in ("Claim", "Participant"):
            assert (m.table, m.canonical) in spec, m
        if m.table == "Measure" and m.canonical not in ("ClaimYearQuarter", "PrgID"):
            assert ("Measure", m.canonical) in spec, m


def test_labels_renamed_between_layouts_load_under_one_name():
    a, b = legacy.rename("claims_a"), legacy.rename("claims_b")
    assert a["Weighted Direct Install Activity"] == b["Weighted Direct Implementation Activity"] == "WtdDIActivity"
    assert (
        a["Levelized Net Benefits TRC No Admin Electric"] == b["Levelized Net Benefits TRC Electric No Admin"] == "LevNetBenTRCElecNoAdmin"
    )
    assert legacy.rename("filings")["Electric Bill Reduction"] == b["Bill Reduction Electric"] == "LifecycleBillReducElec"
    # The program's attributes and its upstream flag are not the measure's
    assert b["Upstream Flag"] == "Prg_Upstream_Flag" and b["Measure Upstream Flag"] == "Upstream_Flag"
    assert legacy.rename("filings")["Filing ID"] == "CEInputID" and a["Claim ID"] == "ClaimID"
    assert "SiteID" in legacy.by_table("claims_b", "Claim") and "SiteID" not in legacy.by_table("claims_a", "Claim")
    assert legacy.by_table("claims_a", "Legacy") == [
        "Legacy_ProgramGroup",
        "Legacy_MeasureTable",
        "Legacy_MeasureQualifier",
        "Legacy_LocationCostAdjustment",
        "Legacy_LaborRate",
        "Legacy_GrossMeasureCostER",
        "Legacy_Budget",
        "Legacy_Benefits",
    ]


def test_layouts_are_recognised_and_a_changed_header_is_refused():
    for name, header in legacy.layouts().items():
        assert legacy.layout_of(list(reversed(header))) == name
    header = list(legacy.layouts()["claims_b"])
    header[0] = "﻿" + header[0]
    assert legacy.layout_of(header) == "claims_b"
    changed = [h for h in legacy.layouts()["claims_b"] if h != "Site ID"] + ["Site Number"]
    with pytest.raises(legacy.LayoutError, match=r"Nearest: claims_b; missing 1 \(Site ID\), added 1 \(Site Number\)"):
        legacy.layout_of(changed)


@pytest.mark.django_db
def test_the_crosswalk_download_lists_the_legacy_labels():
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    text = dictionary.crosswalk_csv()
    assert "E3ClimateZone,CET,AC_Climate_Zone" in text
    assert "Number of Units,legacy CEDARS downloads (claims_a claims_b filings filings_2024_2027 filings_2026_2027_tual),NumUnits" in text
    assert "Budget,legacy CEDARS downloads (claims_a filings filings_2024_2027 filings_2026_2027_tual),Legacy_Budget" in text


# --- imported versions


@pytest.fixture
def claims_deadline(cpuc_admin):
    period = [p for p in registry.ensure_periods(2023) if p.kind == "year"][0]
    return submissions.add_deadline(
        cpuc_admin, module="claims", kind="Annual (final) claim", period=period, due_at=timezone.now() - timedelta(days=300)
    )


@pytest.fixture
def analyst(make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


def _import(deadline, org, actor=None, reason="Imported from legacy CEDARS: https://cedars.example.org/x"):
    files = [{"name": "Measure.parquet", "key": "legacy/x/Measure.parquet", "sha256": "a" * 64, "bytes": 10}]
    return submissions.import_version(actor, deadline=deadline, organisation=org, files=files, reason=reason)


def test_an_imported_version_is_published_and_read_only(analyst, claims_deadline, pge, cpuc_admin):
    v = _import(claims_deadline, pge)
    assert (v.number, v.status, v.origin, v.imported) == (1, "published", "imported", True)
    assert submissions.current(v.submission) == v and v.published_at is not None
    event = AuditEvent.objects.get(action="submission.imported")
    assert event.after["origin"] == "imported" and event.reason.startswith("Imported from legacy CEDARS")
    # every path that would change, reprice, un-confirm, reopen or correct it refuses
    for call in (
        lambda: submissions.start(analyst, claims_deadline, pge),
        lambda: submissions.attach_files(analyst, v, []),
        lambda: submissions.record_validation(analyst, v, None, {"rejected": 0}),
        lambda: submissions.record_change(analyst, v, "a cell"),
        lambda: submissions.record_calculation(None, v, run="r", input_hash="h"),
        lambda: submissions.price(analyst, v, None, name="n", acc_version="2022"),
        lambda: submissions.confirm(analyst, v, "h"),
        lambda: submissions.unconfirm(analyst, v, "why"),
        lambda: submissions.request_reopen(analyst, v.submission, "a correction"),
    ):
        with pytest.raises(AccessDenied, match="imported from legacy CEDARS"):
            call()
    v.refresh_from_db()
    assert v.status == "published"
    # a later download that differs is the next imported version
    v2 = _import(claims_deadline, pge, actor=cpuc_admin, reason="Re-imported: the download changed")
    assert v2.number == 2 and v2.imported


def test_only_a_system_job_or_admin_imports_and_never_over_a_filing(analyst, claims_deadline, pge, pge_admin):
    with pytest.raises(AccessDenied, match="system administrator"):
        _import(claims_deadline, pge, actor=pge_admin)
    with pytest.raises(AccessDenied, match="reason"):
        _import(claims_deadline, pge, reason=" ")
    submissions.start(analyst, claims_deadline, pge)
    with pytest.raises(AccessDenied, match="filed .* on this platform"):
        _import(claims_deadline, pge)


def test_the_database_keeps_imported_versions_published(claims_deadline, pge):
    v = _import(claims_deadline, pge)
    with pytest.raises(IntegrityError), transaction.atomic():
        SubmissionVersion.objects.filter(pk=v.pk).update(status="draft")


# --- staging the downloads


@pytest.fixture
def downloads(tmp_path):
    """A download folder and its manifest: the 2023 all-PA and statewide claims, a 2026 per-PA zip with its participant CSV, and an
    archived filing with macOS junk entries."""
    folder = tmp_path / "downloads"
    rows = [
        claim_row("PGE-2023-0001", "PGE", "PGE_Res_001"),
        claim_row("SCE-2023-0001", "SCE", "SCE_Com_001"),
    ]
    files = [
        (
            "claims",
            "2023",
            "all",
            write_zip(folder / "claims/2023/all_pas_confirmed_claims_records_2023.zip", {"all.csv": csv_text("claims_b", rows)}),
        ),
        (
            "claims",
            "2023",
            "SW",
            write_zip(folder / "claims/2023/sw_unallocated_confirmed_claims_records_2023.zip", {"sw.csv": csv_text("claims_b", rows[:1])}),
        ),
        (
            "claims",
            "2026",
            "PGE",
            write_zip(
                folder / "claims/2026/pge_confirmed_claims_records_2026.zip",
                {"pge_2026.csv": csv_text("claims_b"), "pge_participant_data_2026.csv": csv_text("participants")},
            ),
        ),
        (
            "filings_archive",
            "2024_2027",
            "all",
            write_zip(folder / "filings_archive/filings_record_2024_2027.zip", {"f.csv": csv_text("filings_2024_2027")}, macos_junk=True),
        ),
    ]
    manifest = manifest_text(
        [manifest_row(*f) for f in files] + [["totals", "2028", "SW", "dash.html", "https://x.example.org/", "1", "0", "yes"]]
    )
    return folder, manifest


@pytest.mark.django_db
def test_staging_checks_and_copies_each_file_once(downloads, cpuc_admin):
    folder, manifest = downloads
    report = service.stage(cpuc_admin, manifest, folder)
    assert report.ok and len(report.staged) == 4 and report.skipped == []
    sw = LegacySource.objects.get(pa="SW")
    assert sw.members == {"sw.csv": "claims_b"} and sw.key.startswith("legacy/cedars/claims/2023/")
    assert LegacySource.objects.get(pa="PGE").members == {"pge_2026.csv": "claims_b", "pge_participant_data_2026.csv": "participants"}
    assert LegacySource.objects.get(dataset="filings_archive").members == {"f.csv": "filings_2024_2027"}
    assert [s.pa for s in service.sources("claims", "2023")] == ["all", "SW"]
    with service.open_member(sw, "sw.csv") as f:
        assert "PGE-2023-0001" in f.read()
    assert AuditEvent.objects.filter(action="legacy.source_staged").count() == 4
    again = service.stage(None, manifest, folder)
    assert again.staged == [] and len(again.skipped) == 4 and LegacySource.objects.count() == 4


@pytest.mark.django_db
def test_staging_refuses_files_that_differ_from_the_manifest(downloads, tmp_path):
    folder, manifest = downloads
    (folder / "claims/2023/sw_unallocated_confirmed_claims_records_2023.zip").write_bytes(b"changed")
    (folder / "filings_archive/filings_record_2024_2027.zip").unlink()
    changed = [h for h in legacy.layouts()["claims_b"] if h != "Site ID"]
    write_zip(folder / "claims/2026/pge_confirmed_claims_records_2026.zip", {"x.csv": ",".join(changed) + "\r\n"})
    report = service.stage(None, manifest, folder)
    assert report.staged == ["all_pas_confirmed_claims_records_2023.zip"]
    assert report.missing == ["filings_record_2024_2027.zip"]
    reasons = dict(report.refused)
    assert "bytes, the manifest says" in reasons["sw_unallocated_confirmed_claims_records_2023.zip"]
    assert "bytes, the manifest says" in reasons["pge_confirmed_claims_records_2026.zip"]
    # same size, different content
    sw = folder / "claims/2023/sw_unallocated_confirmed_claims_records_2023.zip"
    row = [r for r in manifest.splitlines() if "sw_unallocated" in r][0].split(",")
    sw.write_bytes(b"x" * int(row[5]))
    assert "SHA-256 differs" in dict(service.stage(None, manifest, folder).refused)[sw.name]


@pytest.mark.django_db
def test_staging_refuses_a_header_that_is_no_published_layout(tmp_path):
    folder = tmp_path / "d"
    changed = [h for h in legacy.layouts()["claims_b"] if h != "Site ID"] + ["Site Number"]
    z = write_zip(folder / "claims/2024/x.zip", {"x.csv": ",".join(changed) + "\r\n"})
    report = service.stage(None, manifest_text([manifest_row("claims", "2024", "all", z)]), folder, dry_run=True)
    assert "x.csv: Not a published legacy CEDARS layout" in dict(report.refused)["x.zip"]


@pytest.mark.django_db
def test_staging_is_for_system_administrators(downloads, pge_admin):
    with pytest.raises(AccessDenied):
        service.stage(pge_admin, downloads[1], downloads[0])


@pytest.mark.django_db
def test_the_command_reports_and_fails_on_problems(downloads, tmp_path, capsys):
    folder, manifest = downloads
    path = tmp_path / "manifest.csv"
    path.write_text(manifest, encoding="utf-8")
    call_command("stage_legacy_sources", str(path), str(folder), "--dataset", "claims", "--dry-run")
    assert "staged 3, already staged 0, missing 0, refused 0" in capsys.readouterr().out
    assert LegacySource.objects.count() == 0
    (folder / "filings_archive/filings_record_2024_2027.zip").unlink()
    with pytest.raises(CommandError):
        call_command("stage_legacy_sources", str(path), str(folder))
    assert "missing: filings_record_2024_2027.zip" in capsys.readouterr().out
    assert LegacySource.objects.count() == 3


# --- recording an import


def test_a_year_is_published_only_when_its_hard_checks_pass(downloads, claims_deadline, pge, cpuc_admin):
    service.stage(None, downloads[1], downloads[0])
    sources = service.sources("claims", "2023")
    failing = {"checks": [{"name": "rows", "kind": "hard", "ok": False}, {"name": "published totals", "kind": "soft", "ok": False}]}
    kwargs = dict(module="claims", period="2023", deadline=claims_deadline, organisation=pge, files=[], sources=sources, records=2)
    with pytest.raises(service.LegacyError, match="hard checks rows failed"):
        service.record_import(None, reconciliation=failing, **kwargs)
    assert not SubmissionVersion.objects.exists()
    passing = {"checks": [{"name": "rows", "kind": "hard", "ok": True}, {"name": "published totals", "kind": "soft", "ok": False}]}
    imp = service.record_import(None, reconciliation=passing, outside_totals=1, **kwargs)
    v = imp.version
    assert v.imported and v.findings == {"records": 2, "outside_totals": 1}
    assert v.calculation["engine"] == "CET (legacy CEDARS), release not published"
    assert "https://cedars.example.org/reports/download-record-level-report/all/2023/claims/" in v.reason
    assert set(imp.sources.all()) == set(sources)
    # the same sources again: nothing new
    assert service.record_import(cpuc_admin, reconciliation=passing, **kwargs) == imp
    assert LegacyImport.objects.count() == 1 and AuditEvent.objects.filter(action="legacy.imported").count() == 1
    # a changed download: the next version, with the reason
    imp2 = service.record_import(cpuc_admin, reconciliation=passing, **{**kwargs, "sources": sources[:1]})
    assert imp2.version.number == 2 and imp2.version.reason.startswith("Re-imported: the legacy CEDARS download changed.")
