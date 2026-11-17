"""A run's output files on its page, in three groups with a line on what each file is (UI review 3): the results people read, the
checks of the inputs, and the records of how the run was made. The names are the engine's (ReCET's jobs.py lists them); a file the engine
adds later that is not named here is listed under Other files with no line, so nothing a run wrote is hidden.

An ACC comparison writes each ACC's files in a folder of its own (run_acc2024/...): a file keeps its group and its line, and the folder
is named beside it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (group, pattern on the file's own name, what it is). The first match wins, so a longer name comes before a shorter one it starts with.
KINDS: list[tuple[str, str, str]] = [
    ("results", r"measure_level_", "One row per measure: its inputs, how it was priced, and every output."),
    ("results", r"portfolio_summary_", "Totals and ratios for the whole run, by IOU territory, by credited PA and by program segment."),
    ("results", r"key_metrics_", "The headline results with their definitions, and the main drivers by program, sector and end use."),
    ("results", r"field_definitions_", "What each column of the measure-level file means, with its documentation link."),
    (
        "results",
        r"statewide_reconciliation_",
        "Each statewide program's allocated fields: the total before allocation and each IOU's share.",
    ),
    ("results", r"components_summary_", "The portfolio value of each avoided-cost component, checked against the totals."),
    ("results", r"portfolio_8760_peaks_", "The peak hours of each hourly portfolio profile."),
    ("results", r"portfolio_8760_unshaped_", "The measures left out of the hourly portfolio profile, and why."),
    ("results", r"portfolio_8760_", "The portfolio's net kWh for each hour of the year, by PA and climate zone."),
    ("results", r"cet_comparison_measures_", "Each measure's outputs beside the CET run's, matched by CET_ID."),
    ("results", r"cet_comparison_summary_", "The run's totals beside the CET run's."),
    ("results", r"comparison_summary_", "The portfolio totals on each ACC, and their differences."),
    ("results", r"comparison_", "Each measure's outputs on each ACC, and their differences."),
    ("checks", r"preflight_findings", "Every finding of the input check, with the rows and cells it names."),
    ("checks", r"preflight\.txt$", "The input check that ran before pricing, as text."),
    ("checks", r"preflight\.json$", "The same input check, as data."),
    ("checks", r"filing_validation_", "The filing checks' findings for each record."),
    (
        "records",
        r"receipt\.json$",
        "What produced the results: the engine release, the reference data, the allocation table and each file's SHA-256.",
    ),
    (
        "records",
        r"run_settings\.json$",
        "Everything needed to reproduce the run: its options, the ACC files, the files supplied, versions and timings.",
    ),
    ("records", r"log\.txt$", "What the engine printed while it ran."),
]
GROUPS = [
    ("results", "Results", "What the run found, to read or take into another tool."),
    ("checks", "Checks", "What the input check found before pricing."),
    ("records", "Records", "How the run was made, to reproduce or audit it."),
    ("other", "Other files", "Files the run wrote that this page does not describe."),
]
_COMPILED = [(g, re.compile(p), what) for g, p, what in KINDS]


@dataclass
class Group:
    key: str
    label: str
    intro: str
    files: list[dict] = field(default_factory=list)


def describe(name: str) -> tuple[str, str, str, int]:
    """(group, what the file is, the folder it is in, its place in KINDS) for one output file's path."""
    folder, _, base = name.rpartition("/")
    for i, (group, pattern, what) in enumerate(_COMPILED):
        if pattern.match(base):
            return group, what, folder, i
    return "other", "", folder, len(_COMPILED)


def grouped(files: list[dict]) -> list[Group]:
    """The run's files (service.output_files) in their groups, each file with `what` and `folder` added, the main ones first (the
    measure-level file, then the summary ...) and a comparison's folders together. Empty groups are left out."""
    groups = {k: Group(k, label, intro) for k, label, intro in GROUPS}
    placed = []
    for f in files:
        group, what, folder, i = describe(f["name"])
        placed.append((folder, i, f["name"], group, {**f, "what": what, "folder": folder}))
    for _folder, _i, _name, group, f in sorted(placed, key=lambda x: x[:3]):
        groups[group].files.append(f)
    return [g for g in groups.values() if g.files]
