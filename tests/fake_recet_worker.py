"""A stand-in for `recet worker run SPEC --work DIR` in tests (the engine is private and not installed in every checkout). It follows
the worker's contract for file:// specs: reads the spec and its inputs, writes progress.json while it "runs", then the run's files
and receipt.json to the outputs folder, and exits 0 (FAKE_RECET_EXIT=1 for a failed run, 2 for a refused spec; FAKE_RECET_SLEEP
seconds of "pricing" so a test can stop it)."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


def path(uri):
    return Path(unquote(urlparse(uri).path)) if uri.startswith("file://") else Path(uri)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write_results(out, scale):
    """The kept results ReCET writes beside the measure-level table (out/_results/): a small measure-level parquet and its group sums,
    built by the engine's own results_view when it is installed (the Results pages need it; skipped otherwise)."""
    try:
        import pandas as pd
        from recet import results_view as rv
    except ImportError:
        return
    rows = []
    for i, (pa, prg, sector, year) in enumerate(
        [
            ("PGE", "PGE_Res_HVAC", "Residential", 2028),
            ("PGE", "PGE_Res_HVAC", "Residential", 2029),
            ("PGE", "PGE_Com_Ltg", "Commercial", 2028),
            ("SCE", "SCE_Com_Ltg", "Commercial", 2028),
            ("SCE", "SCE_Com_Ltg", "Commercial", 2029),
            ("SCE", "SCE_Ind", "Industrial", 2029),
        ]
    ):
        ben = 1000.0 * (i + 1) * scale
        rows.append(
            {
                "CEInputID": f"M{i}",
                "MeasDescription": f"measure {i}",
                "PrgID": prg,
                "CreditedPA": pa,
                "Prg_ProgramName": prg.replace("_", " "),
                "Prg_PrimarySector": sector,
                "Prg_ProgramSegment": "Resource Acquisition",
                "Used_Year": year,
                "ElecBen": ben,
                "GasBen": ben / 10,
                "OtherBen": 0.0,
                "TaxCredits": 0.0,
                "TRCBen": ben * 1.1,
                "PACBen": ben * 1.1,
                "TRCCost": 800.0 * (i + 1),
                "PACCost": 500.0 * (i + 1),
                "TRCCostNoAdmin": 700.0 * (i + 1),
                "PACCostNoAdmin": 400.0 * (i + 1),
                "TotalSystemBenefit": ben * 1.1,
                "TotalExpenditures": 600.0 * (i + 1),
                "FirstYearNetKWh": 100.0 * (i + 1),
                "FirstYearNetKW": 0.1 * (i + 1),
                "FirstYearNetThm": 5.0 * (i + 1),
            }
        )
    frame = pd.DataFrame(rows)
    results = out / "_results"
    results.mkdir(exist_ok=True)
    source = results / "measure_level_run.parquet"
    frame.to_parquet(source, index=False)
    rv.group_sums(frame).to_parquet(rv._kept_path(source), index=False)


def main(argv):
    assert argv[0] == "run", argv
    spec = json.loads(path(argv[1]).read_text())
    code = int(os.environ.get("FAKE_RECET_EXIT", "0"))
    if code == 2:
        print("recet worker: job refused: engine.release 9.9.9 was asked for; this worker is recet 0.1.78", file=sys.stderr)
        return 2
    out = path(spec["outputs"]["uri"])
    out.mkdir(parents=True, exist_ok=True)
    inputs = {}
    for name, v in spec["inputs"].items():
        local = path(v["uri"])
        digest = sha(local)
        if digest != v["sha256"]:
            print(f"recet worker: job refused: inputs.{name}: SHA-256 differs", file=sys.stderr)
            return 2
        inputs[name] = {"uri": v["uri"], "sha256": digest, "bytes": local.stat().st_size}
    (out / "progress.json").write_text(json.dumps({"stage": "pricing", "fraction": 0.5, "seconds_left": 3}))
    time.sleep(float(os.environ.get("FAKE_RECET_SLEEP", "0")))
    files = {}
    if code == 0:
        (out / "measure_level_run.csv").write_text("CEInputID,TRCRatio\nM1,1.5\n")
        (out / "run_log.txt").write_text("priced 1 measure\n")
        write_results(out, 1.1 if spec["settings"].get("acc_version") == "2026" else 1.0)
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name not in ("receipt.json", "progress.json"):
            rel = p.relative_to(out).as_posix()
            files[rel] = {"uri": str(p), "sha256": sha(p), "bytes": p.stat().st_size}
    receipt = {
        "receipt_version": 1,
        "job_id": spec["job_id"],
        "status": "succeeded" if code == 0 else "failed",
        "error": None if code == 0 else "ValueError: Measure.csv has no CEInputID column",
        "context": spec.get("context"),
        "started": "2026-10-30T00:00:00+00:00",
        "finished": "2026-10-30T00:00:05+00:00",
        "seconds": 5.0,
        "engine": {"release": spec["engine"]["release"], "calculation": spec["engine"]["calculation"], "image": "", "commit": "abc123"},
        "inputs": inputs,
        "settings": spec.get("settings"),
        "packs": {"files": {"acc/2024/electric_hourly.parquet": {"sha256": "f" * 64, "bytes": 1}}},
        "allocation": {
            "table": "supplied" if "sw_factors" in inputs else "statewide_factors_20260918",
            "sha256": inputs.get("sw_factors", {}).get("sha256", ""),
        },
        "outputs": files,
        "totals": {"TRCRatio": 1.5} if code == 0 else {},
        "notes": [],
    }
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2))
    (out / "progress.json").write_text(json.dumps({"stage": "done", "fraction": 1.0, "seconds_left": 0}))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
