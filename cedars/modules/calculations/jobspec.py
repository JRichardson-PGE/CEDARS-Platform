"""The job spec a run hands the ReCET worker: spec version 1 of ReCET's contract (`recet/jobspec.py`, `recet worker schema`). Built here
from the run record; the worker validates it again and refuses (exit 2) anything it would not run. tests/test_calculations.py checks
the specs this module builds against the engine's own validator when the engine is installed.

Every input is pinned by SHA-256 and every output goes under the run's folder in object storage, so the receipt the worker writes and
the run record name the same files."""

SPEC_VERSION = 1

# The RunConfig settings a run may set, with their labels (the job spec's `settings` block; ReCET's jobspec.settings_fields()).
SETTINGS = {
    "acc_purpose": "Priced for",
    "allocate_statewide": "Statewide allocation",
    "filing_validation": "CEDARS filing checks",
    "filing_year": "Filing year",
    "include_societal": "Societal cost test",
    "emissions": "Emissions",
    "market_effects": "Market effects",
    "preflight_only": "Check inputs only",
    "base_year": "First year",
    "output_template": "Output template",
    "output_template_name": "Output template name",
}


def build(run, uploads, results, *, packs: dict | None = None, pack_files: dict | None = None) -> dict:
    """The spec for `run`: its inputs in the uploads store, its outputs to the results store, and the reference data it must use:
    `pack_files` ({reference-folder path: {sha256, version}}) is the spec's packs.files, which the worker refuses to run against a
    folder whose files differ; `packs` (refdata.pin of the packs) is echoed into the receipt through the context."""
    inputs = {name: {"uri": uploads.uri(f["key"]), "sha256": f["sha256"]} for name, f in run.inputs.items()}
    spec = {
        "spec_version": SPEC_VERSION,
        "job_id": run.job_id,
        "description": run.name,
        "engine": {"release": run.engine_release, "calculation": run.calculation},
        "inputs": inputs,
        "settings": {"acc_version": run.acc_version, **run.settings},
        "outputs": {"uri": results.uri(run.storage_prefix + "out/"), "format": run.output_format},
        "context": {
            "platform_run_id": str(run.id),
            "organisation": run.organisation.code if run.organisation else None,
            "input_hash": run.input_hash,
        },
    }
    if pack_files:
        spec["packs"] = {"files": pack_files}
    if packs:
        spec["context"]["packs"] = packs
    if "sw_factors" in run.inputs:
        # the version's whole table and nothing else (no built-in row applies: ReCET's sw_factors_replace, PR #75), and the worker
        # refuses a run that would use another table
        spec["settings"]["sw_factors_replace"] = True
        spec["allocation_version"] = run.inputs["sw_factors"]["sha256"]
        spec["context"]["allocation_version"] = run.allocation_version.label if run.allocation_version else None
    return spec
