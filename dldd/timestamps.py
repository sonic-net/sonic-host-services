"""Canonical timestamp formatting at DLDD external boundaries."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any, Mapping


_TIMESTAMP_FIELDS = frozenset(
    (
        "expireat",
        "first_seen",
        "last_attempt",
        "last_detection_time",
        "last_success",
        "next_due",
        "origin_time",
        "since",
        "timestamp",
        "wait_until",
    )
)


def floor_timestamp(value: Any) -> Any:
    """Floor a finite numeric timestamp while preserving absent/invalid data."""

    if isinstance(value, bool) or not isinstance(value, Real):
        return value
    if not math.isfinite(float(value)):
        return value
    return math.floor(value)


def _is_timestamp_field(name: Any) -> bool:
    name = str(name).replace("-", "_")
    return (
        name in _TIMESTAMP_FIELDS
        or name.endswith("_at")
        or name.endswith("_deadline")
        or name.endswith("_timestamp")
    )


def floor_timestamp_fields(value: Any) -> Any:
    """Recursively floor values whose field names identify timestamps.

    Durations, intervals, and monotonic scheduler values are intentionally not
    matched. They retain their original precision and type.
    """

    if isinstance(value, Mapping):
        result = {}
        for name, item in value.items():
            normalized = floor_timestamp_fields(item)
            if _is_timestamp_field(name):
                normalized = floor_timestamp(normalized)
            result[name] = normalized
        return result
    if isinstance(value, tuple):
        return tuple(floor_timestamp_fields(item) for item in value)
    if isinstance(value, list):
        return [floor_timestamp_fields(item) for item in value]
    return value
