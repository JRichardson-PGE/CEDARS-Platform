"""8,760 arrays: stored once each in default storage (S3 in production) under their SHA-256, as raw little-endian float64 (70,080
bytes), so a worker can read or memory-map one without a parser. A shape is never changed or deleted while anything points at it."""

import calendar
import hashlib
from datetime import date, timedelta

import numpy as np
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .models import HOURS, Shape


def as_array(values) -> np.ndarray:
    a = np.ascontiguousarray(np.asarray(values, dtype="<f8"))
    if a.shape != (HOURS,):
        raise ValueError(f"an 8,760 has 8,760 values, not {a.size}")
    if not np.isfinite(a).all():
        raise ValueError("an 8,760 cannot hold blank, infinite or not-a-number values")
    return a


def store(values) -> Shape:
    """The Shape for these values, writing the array only if no shape has exactly these values yet."""
    a = as_array(values)
    data = a.tobytes()
    sha = hashlib.sha256(data).hexdigest()
    shape = Shape.objects.filter(sha256=sha).first()
    if shape is None:
        shape = Shape(sha256=sha, total=float(a.sum()), minimum=float(a.min()), maximum=float(a.max()))
        if not default_storage.exists(shape.storage_name):
            default_storage.save(shape.storage_name, ContentFile(data))
        shape, _ = Shape.objects.get_or_create(
            sha256=sha, defaults={"total": shape.total, "minimum": shape.minimum, "maximum": shape.maximum}
        )
    return shape


def load(shape: Shape) -> np.ndarray:
    with default_storage.open(shape.storage_name, "rb") as f:
        a = np.frombuffer(f.read(), dtype="<f8")
    if a.size != HOURS:
        raise ValueError(f"shape {shape.sha256} holds {a.size} values")
    return a


def monthly_shares(values, year: int = 2009) -> list[tuple[str, float]]:
    """(month name, share of the year's total) on the given calendar (a leap year's 29 February is not in an 8,760, so its hours run on
    from 1 March), for showing a shape as text."""
    a = np.asarray(values, dtype=float)
    total = a.sum()
    out, start = [], 0
    for m in range(1, 13):
        days = calendar.monthrange(year, m)[1] - (1 if (m == 2 and calendar.isleap(year)) else 0)
        part = a[start : start + 24 * days].sum()
        out.append((calendar.month_name[m], float(part / total) if total else 0.0))
        start += 24 * days
    return out


def peak_hour(values, year: int = 2009) -> tuple[int, str]:
    """(hour of year 1-8760, its date and hour) of the largest value."""
    i = int(np.argmax(np.asarray(values)))
    day = date(year, 1, 1) + timedelta(days=i // 24)
    if calendar.isleap(year) and day >= date(year, 2, 29):
        day += timedelta(days=1)
    return i + 1, f"{day:%a %d %b}, hour ending {i % 24 + 1}"
