from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from cedars.services.accounts.service import AccessDenied
from cedars.services.registry import service
from cedars.services.registry.models import Program


def _shown(value) -> str:
    if value in (None, "", {}):
        return "(blank)"
    return str(value)


def _lines(diff: dict) -> list[str]:
    """One line per attribute that changes; a dict attribute (flags) one line per key that changes."""
    out = []
    for f, (old, new) in diff.items():
        if isinstance(old, dict) and isinstance(new, dict) and old and new:
            out += [
                f"{f} {k}: {_shown(old.get(k))} -> {_shown(new.get(k))}" for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)
            ]
        else:
            out.append(f"{f}: {_shown(old)} -> {_shown(new)}")
    return out


class Command(BaseCommand):
    help = (
        "Make each statewide program the source of truth for the IDs allocated to it (a PA's code in front of its PrgID, e.g. "
        "PGE_SW_FS for SW_FS): the first time, fill the statewide program's record from its lead PA's ID, then have every PA's ID follow "
        "it from the effective date (today by default), as a system change with a reason. Statewide programs with no lead PA recorded are "
        "listed and skipped. Each ID keeps the dates it covers; only its attributes change. Run it again after recording more leads; IDs "
        "already following are left alone. Without --apply it makes every change and rolls it back, so it says exactly what --apply "
        "would change."
    )

    def add_arguments(self, parser):
        parser.add_argument("prg_ids", nargs="*", metavar="PrgID", help="Only these statewide programs (default: every one)")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--effective", type=date.fromisoformat, help="YYYY-MM-DD; default today")
        parser.add_argument("--no-fill", action="store_true", help="Leave the statewide programs' own records as they are")
        parser.add_argument("--apply", action="store_true")

    def handle(self, prg_ids, reason, effective, no_fill, apply, **opts):
        programs = Program.objects.filter(statewide=True).select_related("lead").order_by("prg_id")
        if prg_ids:
            programs = programs.filter(prg_id__in=prg_ids)
            missing = set(prg_ids) - set(programs.values_list("prg_id", flat=True))
            if missing:
                raise CommandError(f"Not statewide programs in the registry: {', '.join(sorted(missing))}")
        filled = linked = skipped = failed = 0
        for sw in programs:
            try:  # each statewide program in its own transaction: one that cannot be linked leaves the rest
                r = service.follow_statewide(None, sw, effective=effective, reason=reason, apply=apply, fill=not no_fill)
            except (AccessDenied, DatabaseError) as e:
                self.stderr.write(f"failed {sw.prg_id}: {e}; nothing changed for it")
                failed += 1
                continue
            if r.skipped:
                self.stdout.write(f"skipped {sw.prg_id}: {r.skipped}")
                skipped += 1
                continue
            if not r.linked:
                continue
            self.stdout.write(f"{sw.prg_id} (lead {sw.lead.code})")
            if r.filled:
                self.stdout.write(f"  filled from {r.source.prg_id} from {r.filled_from:%Y-%m-%d}:")
                for line in _lines(r.filled):
                    self.stdout.write(f"    {line}")
                filled += 1
            for prg_id, day, diff in r.linked:
                self.stdout.write(f"  {prg_id} follows it from {day:%Y-%m-%d}" + (":" if diff else " (same attributes)"))
                for line in _lines(diff):
                    self.stdout.write(f"    {line}")
                linked += 1
            if r.already:
                self.stdout.write(f"  already following: {', '.join(r.already)}")
        fill_word, link_word = ("filled", "linked") if apply else ("would fill", "would link")
        self.stdout.write(
            f"{fill_word} {filled} statewide program{'s' if filled != 1 else ''} and {link_word} {linked} ID{'s' if linked != 1 else ''}; "
            f"skipped {skipped}" + (f"; failed {failed}" if failed else "")
        )
