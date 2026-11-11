"""docs/ARCHITECTURE_AS_BUILT.md describes the platform as built, and is kept current in the pull request that changes it (CLAUDE.md;
Jake, 2026-11-06). These tests fail when the code gains a part the document does not name (an app, a model, a management command, an
extension hook) and hold the dependency rules it states (modules never import each other; services never import a module; the site
shell imports only the modules the document names). They cannot see a changed flow; the author keeps that true."""

import ast
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "cedars"
DOC = ROOT / "docs" / "ARCHITECTURE_AS_BUILT.md"
TEXT = DOC.read_text(encoding="utf-8")
NAMED = set(re.findall(r"`([^`\n]+)`", TEXT))  # what the document names in backticks, outside the diagrams too
MERMAID = re.findall(r"```mermaid\n(.*?)```", TEXT, re.S)
DIAGRAM_TYPES = ("flowchart", "graph", "sequenceDiagram", "stateDiagram-v2", "erDiagram", "classDiagram")

# register_* functions that are a service's own functions, not a point where a service calls back into a module
NOT_HOOKS = {
    "register_pack",  # refdata: registers a reference-data pack (a command of the same name is listed)
    "register_set",  # refdata.cost_tests: registers a cost-test set as a pack
    "register_csv",  # releases.mandates: the register of mandates as CSV
}
# Files whose generic `register` is a hook: the document's hooks table has a row "| `register` | ... `<file name>` |"
GENERIC_HOOKS = ["core/queue.py", "core/health.py", "core/search.py", "services/submissions/corrections.py"]
# What the site shell (cedars.core) may import from the modules: the front page
CORE_MAY_IMPORT = {"cedars.modules.data"}


def _sources(folder: Path):
    for p in sorted(folder.rglob("*.py")):
        if "migrations" not in p.parts:
            yield p


def _imports(path: Path) -> set[str]:
    """Every module the file imports (top level or inside a function), relative imports made absolute."""
    package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                name = ".".join(base + ([node.module] if node.module else []))
            else:
                name = node.module or ""
            out.add(name)
            out.update(f"{name}.{a.name}" for a in node.names)
    return out


def _module_of(name: str) -> str | None:
    """cedars.modules.<module> for an import inside a module, else None."""
    parts = name.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 and parts[:2] == ["cedars", "modules"] else None


def test_every_platform_app_is_described():
    missing = [a for a in settings.INSTALLED_APPS if a.startswith("cedars.") and a not in NAMED]
    assert not missing, f"Describe these apps in docs/ARCHITECTURE_AS_BUILT.md (section 3): {missing}"


def test_every_model_is_named():
    missing = [
        f"{cfg.label}.{m.__name__}"
        for cfg in apps.get_app_configs()
        if cfg.name.startswith("cedars.")
        for m in cfg.get_models()
        if m.__name__ not in NAMED
    ]
    assert not missing, f"Name these models in docs/ARCHITECTURE_AS_BUILT.md (section 4.4, and an ERD when they relate): {missing}"


def test_every_management_command_is_named():
    commands = sorted(p.stem for p in CODE.rglob("management/commands/*.py") if p.stem != "__init__")
    assert commands, "no management commands found: the test is looking in the wrong place"
    missing = [c for c in commands if c not in NAMED]
    assert not missing, f"Name these management commands in docs/ARCHITECTURE_AS_BUILT.md (section 12): {missing}"


def test_every_extension_hook_is_named():
    hooks = set()
    for p in [*_sources(CODE / "services"), *_sources(CODE / "core")]:
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("register_") and node.name not in NOT_HOOKS:
                hooks.add(node.name)
    assert hooks, "no hooks found: the test is looking in the wrong place"
    missing = sorted(h for h in hooks if h not in NAMED)
    assert not missing, f"Add these hooks to the table in docs/ARCHITECTURE_AS_BUILT.md (section 3.2): {missing}"
    for rel in GENERIC_HOOKS:
        tree = ast.parse((CODE / rel).read_text(encoding="utf-8"))
        assert any(isinstance(n, ast.FunctionDef) and n.name == "register" for n in tree.body), f"{rel} has no register()"
        row = re.compile(r"^\| `register` \|[^|\n]*`" + re.escape(Path(rel).name) + "`", re.M)
        assert row.search(TEXT), f"The hooks table (section 3.2) has no row for {rel}'s register()"


def test_modules_never_import_each_other_and_services_never_import_a_module():
    crossing = []
    for folder in sorted(p for p in (CODE / "modules").iterdir() if p.is_dir() and p.name != "__pycache__"):
        own = f"cedars.modules.{folder.name}"
        for p in _sources(folder):
            crossing += [f"{p.relative_to(ROOT)} imports {m}" for m in {_module_of(n) for n in _imports(p)} - {None, own}]
    for p in _sources(CODE / "services"):
        crossing += [f"{p.relative_to(ROOT)} imports {m}" for m in {_module_of(n) for n in _imports(p)} - {None}]
    for p in _sources(CODE / "core"):
        crossing += [f"{p.relative_to(ROOT)} imports {m}" for m in {_module_of(n) for n in _imports(p)} - {None} - CORE_MAY_IMPORT]
    assert not crossing, (
        "A module changes data only through a service and never imports another module; a service never imports a module "
        f"(register a hook instead, docs/ARCHITECTURE_AS_BUILT.md section 3): {sorted(crossing)}"
    )


def test_the_site_shell_imports_only_the_modules_the_document_names():
    for m in CORE_MAY_IMPORT:
        assert f"`{m}`" in TEXT, f"Section 3.1 says why cedars.core imports {m}"


def test_every_diagram_names_its_type():
    assert len(MERMAID) >= 10, "the document's diagrams are missing"
    bad = [d.splitlines()[0] if d.strip() else "(empty)" for d in MERMAID if not d.lstrip().startswith(DIAGRAM_TYPES)]
    assert not bad, f"Mermaid blocks that do not start with a diagram type: {bad}"
    assert TEXT.count("```") % 2 == 0, "an unclosed code block"
