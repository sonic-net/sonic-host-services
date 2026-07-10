"""Operator-requested cleanup of DLDD-owned runtime state."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .artifacts import DEFAULT_ARTIFACT_DIRECTORY
from .ownership import is_dldd_fault_payload
from .telemetry import StateDB, TelemetryPublisher


@dataclass(frozen=True)
class ResetResult:
    redis_keys: int
    faults: int
    artifacts: int
    local_state_removed: bool


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _keys(state_db: StateDB, pattern: str) -> Iterable[str]:
    return tuple(_text(key) for key in state_db.keys(pattern))


def _clear_artifacts(directory: str) -> int:
    removed = 0
    try:
        names = tuple(os.listdir(directory))
    except FileNotFoundError:
        return 0
    for name in names:
        if not (
            (name.startswith("dldd-") and name.endswith(".tar.gz"))
            or (name.startswith("dldd-") and name.endswith(".json"))
            or (name.startswith(".dldd-") and name.endswith(".tar.gz"))
        ):
            continue
        path = os.path.join(directory, name)
        if os.path.isdir(path) and not os.path.islink(path):
            continue
        os.unlink(path)
        removed += 1
    return removed


def clear_runtime_state(
    state_db: StateDB,
    state_file: str,
    include_faults: bool = False,
    include_artifacts: bool = False,
    artifact_directory: str = DEFAULT_ARTIFACT_DIRECTORY,
) -> ResetResult:
    """Clear DLDD runtime state while preserving rules and configuration.

    Redis keys are discovered before mutation and removed with one backend
    operation.  DLDD ownership markers protect FAULT_INFO rows written by
    another producer.
    """

    keys = set()
    for key in (
        TelemetryPublisher.STATUS_KEY,
        TelemetryPublisher.RULE_STATUS_KEY,
    ):
        if state_db.hgetall(key):
            keys.add(key)
    keys.update(_keys(state_db, TelemetryPublisher.RULE_STATUS_PREFIX + "*"))
    keys.update(_keys(state_db, TelemetryPublisher.RULE_DETAIL_PREFIX + "*"))

    fault_keys = set()
    if include_faults:
        for key in _keys(state_db, "FAULT_INFO|*"):
            if is_dldd_fault_payload(state_db.hgetall(key)):
                fault_keys.add(key)
        keys.update(fault_keys)

    state_db.delete_many(sorted(keys))

    state_removed = False
    try:
        os.unlink(state_file)
        state_removed = True
    except FileNotFoundError:
        pass

    artifacts = _clear_artifacts(artifact_directory) if include_artifacts else 0
    return ResetResult(len(keys), len(fault_keys), artifacts, state_removed)
