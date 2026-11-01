"""Time the input editor's eTRM lookup (BR-06.22) on a pack: the package search, each offering choice (a loose index scan) and the walk
from a package to one permutation, over a sample of packages, smallest to largest. With --synthetic N it first loads a draft pack of N
made-up permutations shaped like eTRM's (several hundred measure packages, a few very large; offering keys with eTRM's kinds of values)
through the same loader as a real download, times that load too, and discards the pack afterwards unless --keep. eTRM's CET downloads
held about 2.1 million permutations for program year 2026 alone, so `--synthetic 2100000` is the real size."""

import csv
import random
import statistics
import tempfile
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from cedars.services.refdata import etrm
from cedars.services.refdata import service as refdata
from cedars.services.refdata.models import EtrmPackage, EtrmPermutation, PackVersion

USE_CATEGORIES = ["HC", "WH", "WP", "CR", "AP", "LG", "SV", "BE", "FS", "RE", "CA", "WB", "PR", "MI"]
# each key's pool of values; a package draws how many of each it has
POOLS = {
    "Offering ID": [chr(65 + i) for i in range(26)] + [f"A{i}" for i in range(20)],
    "Measure Application Type": ["NR", "AR", "NC", "AOE", "BRO-Eq", "BRO-Op", "ER"],
    "Delivery Type": ["DnDeemed", "DnDeemDI", "UpDeemed", "MidDeemed", "DnCust", "DnDeemCust"],
    "Sector": ["Com", "Res", "Ind", "Ag"],
    "Building Type": ["Any", "Asm", "ECC", "EPr", "ERC", "EUn", "Gro", "Hsp", "Htl", "MBT", "MFm", "MLI", "Mtl", "Nrs", "OfL", "OfS"]
    + ["RFF", "RSD", "Rt3", "RtL", "RtS", "SCn", "SFm", "SUn", "WRf", "Com", "Ind", "Res", "DMo", "Agr"],
    "Building Vintage": ["Ex", "New", "Any", "v75", "v85", "v96", "v03", "v07", "v11", "v14"],
    "Building HVAC": ["Any", "cWtd", "cAvg", "dxGF", "dxHP", "dxEH", "hpWtd", "rWtd", "ncNH", "rDXGF", "rDXHP", "rNCEH"],
    "Building Location": [f"CZ{z:02d}" for z in range(1, 17)] + ["IOU", "Any", "PGE", "SCE", "SCG", "SDG"],
    "Net to Gross Ratio ID": [
        "Com-Default>2yrs",
        "Res-Default>2yrs",
        "Ind-Default>2yrs",
        "Com-Default<=2yrs",
        "NR-Upstream",
        "ALL-Default",
    ],
    "GSIA ID": ["Def-GSIA", "Res-GSIA-Ltg", "Com-GSIA-Ltg", "NR-GSIA-Upstream"],
    "Program Administrator Type": ["Any", "IOU", "REN", "CCA"],
    "Program Administrator": ["Any", "PGE", "SCE", "SCG", "SDGE", "BayREN", "SoCalREN", "MCE"],
}
LARGEST = 250_000  # the largest synthetic package (eTRM's largest use-category download, water heating, held 942,036 for 2026)
# how many values of each key a package uses, at most, in order: the climate zones are the big multiplier
SPREAD = [8, 3, 4, 2, 30, 6, 10, 22, 3, 2, 2, 3]


def sizes(total: int, packages: int, rng: random.Random) -> list[int]:
    """Package sizes adding up to `total`: heavy-tailed, as eTRM's are (most packages small, a few with hundreds of thousands)."""
    cap = max(1, min(LARGEST, total // 4))
    w = [rng.paretovariate(1.1) for _ in range(packages)]
    out = [1] * packages
    while sum(out) < total:  # share what is left among the packages not yet at the cap, by weight
        room = [i for i in range(packages) if out[i] < cap]
        if not room:
            raise CommandError("too few packages for that many permutations")
        left, s = total - sum(out), sum(w[i] for i in room)
        for i in room:
            out[i] = min(cap, out[i] + max(1, int(left * w[i] / s)))
    while sum(out) > total:
        out[out.index(max(out))] -= sum(out) - total
    return out


def write_downloads(folder: Path, total: int, packages: int, seed: int) -> list[Path]:
    """Synthetic permutations downloads, one file per use category (as eTRM's CET downloads are cut), each with eTRM's banner line."""
    rng = random.Random(seed)
    keys = list(POOLS)
    cols = sorted({etrm.ID, etrm.VERSION, etrm.START, etrm.END, *etrm.KEPT})
    files, writers = {}, {}
    for uc in USE_CATEGORIES:
        p = folder / f"{uc}_permutations.csv"
        f = open(p, "w", newline="", encoding="utf-8")  # noqa: SIM115 - closed below
        f.write('"Exported from: synthetic eTRM permutations for timing"\n')
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        files[uc], writers[uc] = f, w
    for n, size in enumerate(sizes(total, packages, rng)):
        uc = USE_CATEGORIES[min(len(USE_CATEGORIES) - 1, int(rng.expovariate(0.4)))]
        mv = f"SW{uc}{n:03d}-0{rng.randint(1, 4)}"
        values = []
        for key, most in zip(keys, SPREAD, strict=True):
            values.append(rng.sample(POOLS[key], rng.randint(1, min(most, len(POOLS[key])))))
        # grow the smallest-spread keys until the package can hold `size` distinct offerings
        radix = [len(v) for v in values]
        while _product(radix) < size:
            i = min(range(len(keys)), key=lambda i: radix[i] / len(POOLS[keys[i]]))
            if radix[i] == len(POOLS[keys[i]]):
                raise CommandError("a package is larger than the key pools allow")
            values[i] = values[i] + [v for v in POOLS[keys[i]] if v not in values[i]][:1]
            radix[i] += 1
        start = f"{rng.choice([2023, 2024, 2025, 2026])}-01-01"
        end = "" if rng.random() < 0.8 else f"{rng.choice([2024, 2025, 2026])}-12-31"
        common = {
            etrm.VERSION: mv,
            etrm.START: start,
            etrm.END: end,
            "Measure Name": f"Synthetic measure {n}",
            "Use Category": uc,
            "Normalized Unit": rng.choice(["Each", "Cap-Tons", "Area-ft2", "Household"]),
            "First Baseline - Electric Savings": "63.3",
            "EUL Years": "15.00",
            "NTG kWh": "0.6000",
            "Upstream Flag (True / False)": "False",
        }
        for code in rng.sample(range(_product(radix)), size):
            row = dict(common)
            parts = []
            for key, opts in zip(keys, values, strict=True):
                code, i = divmod(code, len(opts))
                row[key] = opts[i]
                parts.append(opts[i])
            row["Offering Description"] = f"Offering {row['Offering ID']} of {mv}"
            row[etrm.ID] = "-".join([mv, *parts])
            writers[uc].writerow(row)
    for f in files.values():
        f.close()
    return [p for p in (folder / f"{uc}_permutations.csv" for uc in USE_CATEGORIES) if p.stat().st_size > 200]


def _product(xs):
    out = 1
    for x in xs:
        out *= x
    return out


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.2f} ms"


class Command(BaseCommand):
    help = __doc__.split("\n\n")[0]

    def add_arguments(self, parser):
        parser.add_argument("--pack", type=int, help="the pack to time (default: the one the input editor uses)")
        parser.add_argument("--synthetic", type=int, help="load a synthetic draft pack of this many permutations first")
        parser.add_argument("--packages", type=int, default=600, help="measure packages in the synthetic pack")
        parser.add_argument("--keep", action="store_true", help="keep the synthetic pack")
        parser.add_argument("--walks", type=int, default=200, help="package-to-permutation walks to time")
        parser.add_argument("--seed", type=int, default=2026)

    def handle(self, pack, synthetic, packages, keep, walks, seed, **opts):
        out = self.stdout.write
        made = None
        if synthetic:
            with tempfile.TemporaryDirectory() as tmp:
                t = time.perf_counter()
                paths = write_downloads(Path(tmp), synthetic, packages, seed)
                out(f"wrote {synthetic:,} synthetic permutations in {len(paths)} files: {time.perf_counter() - t:.0f} s")
                t = time.perf_counter()
                made = etrm.load(None, paths, label="Synthetic eTRM permutations (timing)", version=f"synthetic-{seed}")
                out(f"loaded with etrm.load (rows, offering keys, packages, index): {time.perf_counter() - t:.0f} s; {made.notes}")
            target = made
        else:
            target = PackVersion.objects.get(pk=pack) if pack else etrm.editor_pack()
            if target is None:
                raise CommandError("No eTRM permutations pack is loaded.")
        try:
            self.report(target, walks, random.Random(seed))
        finally:
            if made is not None and not keep:
                refdata.discard_draft(None, made, "synthetic pack for lookup timings")
                out("discarded the synthetic pack")

    def report(self, pack, walks, rng):
        out = self.stdout.write
        with connection.cursor() as cur:
            cur.execute("ANALYZE refdata_etrmpermutation")
            cur.execute("SELECT count(*) FROM refdata_etrmpermutation")
            all_rows = cur.fetchone()[0]
            cur.execute(
                "SELECT pg_size_pretty(pg_relation_size('refdata_perm_offering')), "
                "pg_size_pretty(pg_total_relation_size('refdata_etrmpermutation'))"
            )
            index_size, table_size = cur.fetchone()
        pkgs = list(EtrmPackage.objects.filter(pack=pack).order_by("permutations"))
        n = sum(k.permutations for k in pkgs)
        largest, median = pkgs[-1].permutations, pkgs[len(pkgs) // 2].permutations
        out(f"\npack {pack}: {n:,} permutations in {len(pkgs):,} packages (largest {largest:,}, median {median:,})")
        out(f"table: {all_rows:,} rows in every pack, {table_size} with its indexes; offering-key index {index_size}")

        def timed(fn, *a, repeat=5):
            fn(*a)  # warm
            best = []
            for _ in range(repeat):
                t = time.perf_counter()
                r = fn(*a)
                best.append(time.perf_counter() - t)
            return min(best), r

        out("\npackage search (EtrmPackage):")
        for q, year in (("", None), ("", 2026), ("SWHC", None), ("synthetic measure 4", 2026)):
            s, r = timed(etrm.packages, pack, q, year)
            out(f"  search {q!r} year {year}: {_ms(s)} ({len(r)} listed)")

        chosen = [pkgs[0], pkgs[len(pkgs) // 2], *pkgs[-3:]]
        out("\nfirst choice (offering) and a full walk, by package size:")
        for k in chosen:
            s, opts = timed(etrm.choices, pack, k.measure_version_id, "offering", {})
            plain = self.plain_distinct(pack, k.measure_version_id)
            w, steps = timed(self.walk, pack, k.measure_version_id, rng)
            out(
                f"  {k.label} ({k.permutations:,} permutations): offering choices {_ms(s)} ({len(opts)} values; plain SELECT DISTINCT "
                f"{_ms(plain)}); walk to one permutation {_ms(w)} ({steps} page views)"
            )

        per_step, per_view, per_walk = [], [], []
        sample = [rng.choice(pkgs) for _ in range(walks)]
        for k in sample:
            t = time.perf_counter()
            per_view.extend(self.walk(pack, k.measure_version_id, rng, per_step))
            per_walk.append(time.perf_counter() - t)
        out(f"\n{walks} walks over packages drawn at random ({len(per_step):,} choices, {len(per_view):,} page views):")
        for name, xs in (("one choice (loose index scan)", per_step), ("one page view (narrow)", per_view), ("whole walk", per_walk)):
            xs = sorted(xs)
            p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
            out(f"  {name}: median {_ms(statistics.median(xs))}, 95th percentile {_ms(p95)}, max {_ms(xs[-1])}")

    def walk(self, pack, mvid, rng, per_step=None):
        """Choose each open step at random, a page view at a time, as a person would; returns each view's time (or their count)."""
        chosen, views = {}, []
        while True:
            t = time.perf_counter()
            n = etrm.narrow(pack, mvid, chosen, per_step)
            views.append(time.perf_counter() - t)
            if n.open_step is None:
                assert n.permutation is not None
                return views if per_step is not None else len(views)
            chosen[n.open_step.field] = rng.choice(n.open_step.options)

    def plain_distinct(self, pack, mvid):
        qs = EtrmPermutation.objects.filter(pack=pack, measure_version_id=mvid).values_list("offering", flat=True).distinct()
        list(qs.all())
        t = time.perf_counter()
        list(qs.all())  # a new queryset each time: a queryset keeps its results
        return time.perf_counter() - t
