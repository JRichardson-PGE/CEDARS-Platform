"""The budget filing layout as this module's code has it (jobfile.py, the engine's input tables in the field catalog), for the Budget and
application filings specification's starting version (specifications.register_layout). Until filings read their version, a change
here changes the starting document: tests/test_specifications.py fails until it is made again and published as a new version."""

from . import jobfile

KEYS = {"Measure": ["CEInputID"], "ProgramCost": ["PrgID", "PrgYear"]}


def layout() -> dict:
    return {
        "specification": {
            "name": "Budget and application filings",
            "about": "The budget filings, advice letters and applications each program administrator files for a filing cycle, as the "
            "engine's Measure and ProgramCost inputs.",
            "position": 1,
        },
        "version": {
            "name": "Starting version",
            "summary": "Generated from what the platform checks today: the engine's Measure and ProgramCost input tables, the columns "
            "an upload is refused without, the budget filing rule set and the built-in checks.",
            "source": "The budgets module's layout (jobfile.py), the pinned engine's field catalog, the budget filing rule set and the "
            "built-in checks",
            "rule_set_code": "budget_filing",
        },
        "tables": [
            {
                "name": t,
                "required": True,
                "keys": KEYS[t],
                "refuse_without": list(jobfile.REQUIRED[t]),
                "catalog": {"module": "calculations", "table": t},
            }
            for t in jobfile.TABLES
        ],
    }
