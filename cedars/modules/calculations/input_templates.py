"""The input templates New run offers (BR-06.1): the installed engine's own templates (`recet.templates.write_template`, the files
ReCET's own New run and `recet template` give), never a copy kept here, so a template always has the pinned engine's columns. Each file
is written once per engine version and ACC into a temporary folder and served from there.

Offered: the job workbook, the Measure.csv header and the optional input files New run takes (run parameters, 8,760 electric profiles,
gas monthly profiles). Program metadata and statewide factors are not offered: the program registry and the allocation version are
every run's (forms.SUPPLIED_FIELDS)."""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Template:
    kind: str  # recet.templates.KINDS
    file: str  # the engine's own file name (recet/web/app.py TEMPLATE_FILES)
    label: str
    by_acc: bool = False  # the file differs by ACC (run parameters are the ACC version's built-in rows)


TEMPLATES = {
    t.kind: t
    for t in (
        Template("workbook", "template_job_workbook.xlsx", "Job workbook (Measure, ProgramCost and the optional sheets)", by_acc=True),
        Template("measure", "template_Measure_header.csv", "Measure.csv header"),
        Template("run-parameters", "template_run_parameters.csv", "Run parameters", by_acc=True),
        Template("profiles-8760", "template_electric_8760_profiles.csv", "8,760 electric profiles"),
        Template("profiles-gas", "template_gas_monthly_profiles.csv", "Gas monthly profiles"),
    )
}


def engine():
    """recet.templates and the engine's version, or None when the engine is not installed."""
    try:
        import recet
        from recet import templates
    except ImportError:
        return None
    return templates, recet.__version__


def path(kind: str, acc: str) -> Path:
    """The template file, written by the engine on first use. Raises KeyError for a template not offered, LookupError without the engine."""
    t = TEMPLATES[kind]
    eng = engine()
    if eng is None:
        raise LookupError("The engine is not installed on this server.")
    templates, version = eng
    folder = Path(tempfile.gettempdir()) / "cedars-input-templates" / version / (acc if t.by_acc else "any")
    out = folder / t.file
    if not out.exists():
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".{secrets.token_hex(4)}{out.suffix}"  # written aside, then moved in place: a reader never sees half a file
        templates.write_template(kind, tmp, acc)
        os.replace(tmp, out)
    return out
