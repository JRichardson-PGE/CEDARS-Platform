"""The checks written in code: each filing module's own checks (and the PII vault's), which run beside the published rule sets on every
upload. They need what a rule set cannot see (the program registry, the filing's cycle or month, other tables of the filing, earlier
confirmations, eTRM), so they stay in code; they are listed here, read only, so that the Rules pages show every check a filing meets
with its ID, severity and message (`/rules/built-in/`).

Each module registers its list in its `AppConfig.ready()` (`register_coded_checks`); tests/test_coded_checks.py fails on a rule ID the
module's code raises that its list does not name, or a listed one the code no longer raises.

A filing runs the checks its specification version lists (`specifications.apply_checks`). Most checks are CEDARS' own processing (the
columns, keys and IDs, the programs, the links between tables) and always run; a check marked `optional` runs only when the version
lists it, and one with `severities` takes the severity the version gives it, so the CPUC can drop or soften it in a new version."""

from dataclasses import dataclass, replace

SEVERITIES = ("reject", "warning", "reject or warning")
CHOOSABLE = ("reject", "warning")  # the severities a version may give a check that lets it choose


@dataclass(frozen=True)
class CodedCheck:
    """One check: its ID, severity (`reject` stops the version from being confirmed; `reject or warning` when the case decides), the
    tables and fields it looks at, what it finds and how to fix it, in the words its findings use. `optional`: a specification
    version may leave it out; `severities`: the ones a version may give it (blank = its own, fixed)."""

    rule_id: str
    severity: str
    tables: str
    fields: str
    finds: str
    fix: str
    optional: bool = False
    severities: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodedGroup:
    """A module's coded checks: `label` names the filing they run on, `about` says when."""

    module: str
    label: str
    about: str
    checks: tuple[CodedCheck, ...]
    order: int = 0


_groups: dict[str, CodedGroup] = {}


def register_coded_checks(module: str, label: str, about: str, checks, *, order: int = 0) -> None:
    """Called by a module (or service) in its AppConfig.ready() with every check its code raises. Registering again replaces the list."""
    checks = tuple(checks)
    ids = [c.rule_id for c in checks]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{module}: a rule ID is listed twice")
    bad = [c.rule_id for c in checks if c.severity not in SEVERITIES]
    if bad:
        raise ValueError(f"{module}: unknown severity on {bad}")
    bad = [c.rule_id for c in checks if c.severities and (set(c.severities) - set(CHOOSABLE) or c.severity not in c.severities)]
    if bad:
        raise ValueError(f"{module}: a choosable severity must be reject or warning, its own included, on {bad}")
    _groups[module] = CodedGroup(module, label, about, checks, order)


def groups() -> list[CodedGroup]:
    """Every registered list, in the order the modules gave."""
    return sorted(_groups.values(), key=lambda g: (g.order, g.label))


def choosable(check: CodedCheck) -> CodedCheck:
    """The check, which a specification version may leave out or make a warning or a reject."""
    return replace(check, optional=True, severities=CHOOSABLE)


def by_id() -> dict[str, CodedCheck]:
    """Every registered check, by rule ID."""
    return {c.rule_id: c for g in _groups.values() for c in g.checks}


def group(module: str) -> CodedGroup | None:
    return _groups.get(module)
