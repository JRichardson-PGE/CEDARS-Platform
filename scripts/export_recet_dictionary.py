"""Export ReCET's field dictionary as the platform's catalog file (decision L: ReCET's names are canonical).

    uv run python scripts/export_recet_dictionary.py data/dictionary/      (needs the pinned ReCET engine installed: engine/recet.txt)

Writes recet-<version>.json: every Measure and ProgramCost input field and every measure-level output column, with its definition, unit,
source, cost-test treatment, time basis, aggregation, rounding and years in use (ReCET's field_docs and field_attributes), its type and
length from CEDARS' own claims specification where CEDARS has the field, the names it is also known by (CET's E3 names, earlier ReCET
output names), and the templates that use it (the Calculations module's Measure and ProgramCost files, in CET's column order).
Load it with `manage.py load_dictionary`."""

import json
import re
import sys
from pathlib import Path

import recet
from recet import field_attributes, field_docs, fieldnames, output_templates, templates

SPEC_LINE = re.compile(r"^\s*(\w+)\s+(nvarchar\((\d+)\)|numeric|bit|CedarsYearQuarter)(?=[\s,)]|$)(.*?)[,)]*;?\s*$", re.I)


def spec_types() -> dict[str, dict]:
    """Field types from CEDARS' claims and budget-filing specifications (ReCET's copies of cet_spec / cei_spec .<date>.sql): nvarchar(n),
    numeric, bit, CedarsYearQuarter. The claims spec wins where both have a field."""
    root = Path(recet.__file__).parent / "data" / "cet_rules"
    specs = [sorted(root.glob("claims_*/cet_spec.*.sql"))[-1], sorted(root.glob("budget_filing_*/cei_spec.*.sql"))[-1]]
    lines = [line for spec in specs for line in spec.read_text(encoding="utf-8", errors="replace").splitlines()]
    out = {}
    for line in lines:
        m = SPEC_LINE.match(line)
        if not m:
            continue
        name, kind, length, rest = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if kind.startswith("nvarchar"):
            t = {"type": "text", "max_length": int(length)}
        elif kind == "numeric":
            t = {"type": "number"}
        elif kind == "bit":
            t = {"type": "boolean"}
        else:
            t = {"type": "year_quarter"}
        t["required"] = "notempty" in rest.lower()
        out.setdefault(fieldnames.recet_name(name), t)
    return out


def main(out_dir: Path) -> Path:
    types = spec_types()
    aliases: dict[str, list[dict]] = {}
    for old, new in fieldnames.LEGACY_TO_RECET.items():
        aliases.setdefault(new, []).append({"name": old, "system": "CET"})
    for old, new in fieldnames.OUTPUT_RENAMES.items():
        aliases.setdefault(new, []).append({"name": old, "system": "ReCET before 0.1.50"})

    fields, seen = [], set()

    def add(name, kind, group, usage):
        if name in seen:
            for f in fields:
                if f["name"] == name:
                    f["usage"] += usage
            return
        seen.add(name)
        d = field_docs.definition(name)
        text = d.text if d else ""
        attrs = field_attributes.attributes(name, text)
        t = types.get(name, {})
        fields.append(
            {
                "name": name,
                "kind": kind,
                "group": group,
                "definition": text,
                "doc_link": d.link if d else "",
                "data_type": t.get("type", "number" if attrs["unit"] not in ("", "text") and kind == "output" else "text"),
                "max_length": t.get("max_length"),
                **attrs,
                "aliases": aliases.get(name, []),
                "usage": usage,
            }
        )

    calc_required = set(getattr(__import__("recet.preflight", fromlist=["x"]), "CALCULATION_MEASURE_COLUMNS", []))
    for i, c in enumerate(templates.MEASURE_COLUMNS):
        add(c, "input", "Measure", [{"module": "calculations", "table": "Measure", "position": i, "required": c in calc_required}])
    for i, c in enumerate(templates.PROGRAMCOST_COLUMNS):
        add(
            c,
            "input",
            "ProgramCost",
            [{"module": "calculations", "table": "ProgramCost", "position": i, "required": c in ("PA", "PrgID", "PrgYear")}],
        )
    for group, column, _definition, _link in output_templates.catalog():
        if group == "Input columns":
            if column not in seen:
                add(column, "output", "Run settings", [])
            continue
        add(column, "output", group, [])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"recet-{recet.__version__}.json"
    payload = {"source": f"ReCET {recet.__version__}", "fields": fields}
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    p = main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/dictionary"))
    print(f"wrote {p}")
