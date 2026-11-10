"""Reading one utility and climate zone's hourly avoided costs back out of a registered electric ACC pack.

The pack's file is the engine's own (ReCET's reference folder, acc/<year>/electric_hourly.parquet): one row per utility, climate zone,
cost test, year and hour, the values in $/MWh. The extraction writes each (utility, zone, cost test) as its own row group, so one
series is read from the row groups whose statistics can hold it, without reading the rest of the file.
"""

from dataclasses import dataclass

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import files
from .models import PackVersion

FILE = "electric_hourly.parquet"
HOURS = 8760
DEFAULT_CALENDAR = 2018  # the weekdays an ACC follows when its file has no hour stamps (ReCET real_impact_profiles.ACC_CALENDAR_YEAR)


@dataclass(frozen=True)
class HourlySeries:
    """{year: 8,760 values in $/kWh} for one utility, zone and cost test, and the calendar year whose weekdays the hours follow."""

    years: dict
    calendar_year: int


def hourly(pack: PackVersion, utility: str, zone: str, test: str = "TRC", column: str = "total_avoided_cost") -> HourlySeries | None:
    """One series of the pack's file, divided by 1,000 ($/MWh to $/kWh, as the engine prices it); None when the pack has no such
    utility, zone and test (or no such file)."""
    if pack.kind != PackVersion.Kind.ACC_ELECTRIC:
        raise ValueError(f"{pack} is not an electric ACC pack.")
    name = files.manifest_name(pack, FILE)
    if name is None:
        return None
    with files.open_file(pack, name) as f:
        pf = pq.ParquetFile(f)
        names = pf.schema_arrow.names
        if column not in names:
            return None
        cols = ["iou", "climate_zone", "cost_test", "year", "hour_of_year", column] + (["datetime"] if "datetime" in names else [])
        key_index = {n: names.index(n) for n in ("iou", "climate_zone", "cost_test")}
        wanted = {"iou": utility, "climate_zone": zone, "cost_test": test}
        parts = []
        for i in range(pf.num_row_groups):
            meta = pf.metadata.row_group(i)
            if any(_excludes(meta.column(key_index[k]).statistics, v) for k, v in wanted.items()):
                continue
            t = pf.read_row_group(i, columns=cols)
            mask = None
            for k, v in wanted.items():
                col = t[k]
                if hasattr(col.type, "value_type"):
                    col = col.cast(col.type.value_type)
                m = pc.equal(col, v)
                mask = m if mask is None else pc.and_(mask, m)
            t = t.filter(mask)
            if t.num_rows:
                parts.append(t)
    if not parts:
        return None
    years, calendar = {}, None
    for t in parts:
        year = t["year"].to_numpy().astype(int)
        hour = t["hour_of_year"].to_numpy().astype(int)
        value = t[column].to_numpy(zero_copy_only=False).astype(float)
        for y in np.unique(year):
            sel = year == y
            arr = years.setdefault(int(y), np.full(HOURS, np.nan))
            arr[hour[sel] - 1] = value[sel] / 1000.0
        if "datetime" in t.column_names and t.num_rows:
            first = pc.min(t["datetime"]).as_py()
            if first is not None:
                calendar = first.year if calendar is None else min(calendar, first.year)
    if any(np.isnan(a).any() for a in years.values()):
        return None
    return HourlySeries(years=dict(sorted(years.items())), calendar_year=calendar or DEFAULT_CALENDAR)


def _excludes(stats, value: str) -> bool:
    """True when a row group's statistics show it cannot hold `value`."""
    if stats is None or not stats.has_min_max:
        return False
    lo, hi = stats.min, stats.max
    if isinstance(lo, bytes):
        lo, hi = lo.decode(), hi.decode()
    return not (lo <= value <= hi)
