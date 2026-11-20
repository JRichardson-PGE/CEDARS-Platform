# The batch worker

`recet worker` is how a batch system runs ReCET: the CEDARS platform's Calculations module calls it for every filing and analysis run. It reads a
versioned spec, fetches the inputs, refuses the job when anything differs from what the spec pins, runs it, and sends the outputs with a receipt to the
spec's outputs folder. The platform keeps the queue, the run records and the storage layout; the worker only runs one spec.

| Command | What it does |
|---|---|
| `recet worker run job.json` | price a job from a job spec (section 1); outputs, `receipt.json`, `progress.json` and `log.txt` to its outputs URI |
| `recet worker check check.json` | run a filing's rules from a check spec (section 4); `findings.json` and `receipt.json` to its outputs URI |
| `recet worker validate job.json` | check a job spec without running it |
| `recet worker schema` | print the job spec's JSON schema (`recet/data/job_spec/job_spec_v1.schema.json`) |

A spec, and every input, is a local path, a `file://` URI or an `s3://` URI. S3 needs `pip install ".[s3]"` (boto3 is imported only for S3); credentials
come from the environment or the instance or task role.

## 1. The job spec

    {"spec_version": 1, "job_id": "2028-claims-pge-0001",
     "engine": {"release": "0.1.97", "calculation": "recet"},
     "inputs": {"job_zip": {"uri": "s3://bucket/jobs/0001/job.zip", "sha256": "..."}},
     "settings": {"acc_version": "2024", "acc_purpose": "claims"},
     "packs": {"files": {"acc/2024/electric_hourly.parquet": {"sha256": "..."}}},
     "allocation_version": "statewide_factors_20260918",
     "outputs": {"uri": "s3://bucket/runs/0001/", "format": "csv.zip"},
     "context": {"platform_run_id": 123}}

| Key | Meaning |
|---|---|
| `spec_version` | always 1 (required) |
| `job_id` | names the run folder and the receipt (required) |
| `engine.release` | the ReCET version (`recet --version`) that must run the job; a worker of another release refuses it |
| `engine.calculation` | ReCET's own engine (`recet`), or a CET replica such as `cet-25.1` |
| `inputs` | each a URI or `{"uri", "sha256"}` (required). `job_zip` (Measure.csv and optionally ProgramCost.csv, as the web app takes a job), or `measures` and `programcost`, or `workbook`; optionally `profiles_8760`, `profiles_gas`, `run_parameters`, `sw_factors`, `programs`, `acc_electric`, `acc_gas`, `cet_outputs`, and `cost_tests` (a set's YAML file, analysis runs only) |
| `settings` | the run settings of [Input fields](INPUT_FIELDS.md) section 3 other than files, folders, names, the engine and the output format; `compare_with` prices the job on a second ACC too |
| `packs.reference_dir` | the reference data folder on the worker (default: its `RECET_DATA`) |
| `packs.files` | reference files the job is pinned to, by their path in the folder; the worker refuses the job when a file differs |
| `allocation_version` | the statewide factor table the run must use: the built-in table's name, or a supplied `sw_factors` file's SHA-256 |
| `outputs.uri` | the folder the run's files go to (required) |
| `outputs.format` | `csv`, `csv.zip` (default), `parquet` or `xlsx` |
| `context` | the caller's own references; never read, echoed into the receipt as given |

The schema is the authority; `recet/jobspec.py` holds the rules the schema cannot express.

## 2. What the worker writes

**While it runs**, the worker sends `progress.json` to the outputs URI at each stage change and at most every 10 seconds: `stage`, `label`, `done` and
`total` (parts, while pricing in parts), `fraction`, `seconds_left` and `updated`. It sends `log.txt` with it whenever the log has grown: what the run
printed, each line timed.

**At the end** it sends the run folder (the outputs of [Output fields](OUTPUT_FIELDS.md), the whole log) and `receipt.json`. A spec with
`compare_with` writes each ACC's run in its own `run_acc<ACC>/` folder with the comparison beside them, as `recet run --compare-with` does; the
receipt's totals are the primary ACC's.

**The receipt** says exactly what produced the outputs:

- the engine: release, calculation, the image and commit (from `RECET_IMAGE` and `RECET_COMMIT`), Python and library versions;
- every input's URI, SHA-256, size and row count, and the spec's own SHA-256;
- the run's full settings, the ACC, and the ACC each record was priced on (by ACC version and program year);
- every reference file used with its size and SHA-256 (from the reference folder's `manifest.json`, else computed), and the packaged data's hash;
- the run parameters, the statewide factor table, the program list, the filing rule set and the cost-test set;
- every output file's URI, SHA-256 and size, the totals, the notes and the timings;
- the spec's `context`, as given.

A failed run still gets a receipt, with status `failed` and the error, sent with whatever the run wrote.

## 3. Exit codes

| Code | Meaning |
|---|---|
| 0 | succeeded (or, for `validate`, the spec is valid) |
| 1 | the run failed; a receipt was written |
| 2 | the spec was refused; nothing was run, and the reasons are on stderr |

`recet worker check` uses the same codes.

## 4. The check spec

`recet worker check` runs a filing's rules in this release instead of pricing it (`recet/checkjob.py`). The CEDARS platform keeps each filing's rules
as data and has the worker of the release its specification names check the filing, so the findings come from the release that prices it, whatever
release the website has installed.

| Key | Meaning |
|---|---|
| `check_spec_version` | always 1 |
| `job_id` | letters, digits, `.`, `_` and `-` |
| `engine.release` | the worker refuses another release |
| `rule_set` | `code`, `version` and `rules`, each rule with its `id`, `severity`, `table`, `field`, `text`, `kind`, `source` and `definition` |
| `tables` | the tables checked, each a URI or `{"uri", "sha256"}`: UTF-8 CSV with a header, read as text |
| `context` | what the rule language reads: `filing_year`, `upload_date`, `filer`, each table's `keys`, other `tables` that conditions read but that are not checked, the `programs` records, `lists` the engine does not ship and `value_list_changes` over the shipped lists |
| `skip` | per table, rules not to run (they read a column the file lacks) |
| `outputs.uri` | where `findings.json` and `receipt.json` go |
| `caller` | echoed into the receipt as given |

`findings.json` lists, per table, its row count and each failing rule's rows (0-based) and values, and notes on rules that could not be checked. The rule
language (`recet/filing_rules.py`) reads CEDARS' rule files and, for rules kept as data, conditions on another table of the filing (linked by the
tables' keys), on the program record or on who files; groups of conditions (all or any, one level); and tests for text, flags, dates, a date within so
many years of the upload, and a code in effect on a date.

## 5. The image

The worker image (`Dockerfile.worker`) holds the locked `requirements/worker.txt` (the engine and boto3) and no web app. Pushing the tag `vX.Y.Z`
that matches `recet/__init__.py` makes `.github/workflows/worker-image.yml` build it for amd64 and arm64 and publish it as
`ghcr.io/jrichardson-pge/recet-worker:X.Y.Z`, with `RECET_IMAGE` and `RECET_COMMIT` set so every receipt names the image. Mount the reference data
read-only at `/data`:

    docker run --rm -v /path/to/reference:/data:ro ghcr.io/jrichardson-pge/recet-worker:X.Y.Z run s3://bucket/jobs/0001/job.json
