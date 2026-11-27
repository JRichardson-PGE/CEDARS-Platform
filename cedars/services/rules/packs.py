"""Rule packs: sets of rules kept as data files (`packs/<name>.json`) that an administrator adds to a draft rule set in one step, such as
CEDARS' 2026 claims data rules on measures, which CEDARS publishes only as sentences. A pack names the rule-set code it is for, the rules it
adds (each with where it comes from) and the rules of CEDARS' files it replaces; adding it takes the replaced rules out of the draft and
adds its rules, each audited as any rule added or removed is. A published rule set is never changed: a pack reaches filings with the draft
it is added to (a specification version's rules)."""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from django.db import transaction

from cedars.services import audit

from .models import RuleSet

FOLDER = Path(__file__).parent / "packs"


@dataclass(frozen=True)
class Pack:
    slug: str
    name: str
    code: str
    about: str
    replaces: dict  # rule ID -> why
    rules: tuple  # {severity, table, field, definition, source, fix?}


@dataclass(frozen=True)
class PackState:
    pack: Pack
    missing: list  # the pack's rules (dicts) the rule set does not have
    replaced: list  # rule IDs of the rule set the pack replaces

    @property
    def added(self) -> bool:
        return not self.missing and not self.replaced


@cache
def packs() -> tuple[Pack, ...]:
    out = []
    for f in sorted(FOLDER.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out.append(Pack(f.stem, d["name"], d["code"], d.get("about", ""), dict(d.get("replaces") or {}), tuple(d["rules"])))
    return tuple(out)


def pack(slug: str) -> Pack | None:
    return next((p for p in packs() if p.slug == slug), None)


def rule_id_of(d: dict) -> str:
    """The ID the pack's rule `d` has in a rule set (the engine's content hash of its test)."""
    from . import service

    definition = {**d["definition"], "field": d["field"]}
    return service._id_and_text("rule", d["severity"], d["table"], d["field"], definition)[0]


def state(rs: RuleSet, p: Pack) -> PackState:
    have = set(rs.rules.values_list("rule_id", flat=True))
    return PackState(p, [d for d in p.rules if rule_id_of(d) not in have], sorted(have & set(p.replaces)))


def for_rule_set(rs: RuleSet) -> list[PackState]:
    """The packs for a rule set's code, with what adding each would change."""
    return [state(rs, p) for p in packs() if p.code == rs.code]


@transaction.atomic
def add(actor, rs: RuleSet, slug: str, reason: str = "") -> PackState:
    """Add a pack to a draft rule set: the rules it replaces leave, its rules the draft lacks join. Returns what it changed."""
    from . import service

    p = pack(slug)
    if p is None or p.code != rs.code:
        raise service.RuleError("There is no such set of rules for these rules.")
    if not (reason or "").strip():
        raise service.RuleError("Say why the rules are added: the reason is kept with the version.")
    service._require_admin(actor)
    service._require_draft(rs)
    st = state(rs, p)
    for rid in st.replaced:
        r = rs.rules.get(rule_id=rid)
        service.remove_rule(actor, r, reason=f"{p.name}: {p.replaces[rid]}. {reason.strip()}")
    for d in st.missing:
        service.add_rule(actor, rs, kind="rule", severity=d["severity"], table=d["table"], field=d["field"],
                         definition=d["definition"], fix=d.get("fix", ""), reason=f"{p.name}. {reason.strip()}",
                         source=d.get("source") or p.name)  # fmt: skip
    audit.record("rule_set.pack_added", obj=rs, after={"pack": p.slug, "added": len(st.missing), "removed": st.replaced},
                 reason=reason, actor=actor)  # fmt: skip
    return st
