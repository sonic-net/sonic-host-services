"""Operator-requested cleanup of DLDD-owned runtime state."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .artifacts import DEFAULT_ARTIFACT_DIRECTORY
from .filesystem import unlink_if_exists
from .ownership import is_dldd_fault_payload
from .sonic_hash import decode_db_text
from .telemetry import StateDB, TelemetryPublisher


@dataclass(frozen=True)
class ResetResult:
    redis_keys: int
    faults: int
    artifacts: int
    local_state_removed: bool


def _keys(state_db: StateDB, pattern: str) -> Iterable[str]:
    return tuple(decode_db_text(key) for key in state_db.keys(pattern))


def _clear_artifacts(directory: str) -> int:
    removed = 0
    try:
        names = tuple(os.listdir(directory))
    except FileNotFoundError:
        return 0
    for name in names:
        if not (
            name.startswith("dldd-") and name.endswith((".tar.gz", ".json"))
            or name.startswith(".dldd-") and name.endswith(".tar.gz")
        ):
            continue
        path = os.path.join(directory, name)
        if os.path.isdir(path) and not os.path.islink(path):
            continue
        removed += int(unlink_if_exists(path))
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

    keys = {
        key
        for key in (TelemetryPublisher.STATUS_KEY, TelemetryPublisher.RULE_STATUS_KEY)
        if state_db.hgetall(key)
    }
    keys.update(_keys(state_db, TelemetryPublisher.RULE_STATUS_PREFIX + "*"))
    keys.update(_keys(state_db, TelemetryPublisher.RULE_DETAIL_PREFIX + "*"))

    fault_keys = set()
    if include_faults:
        fault_keys = {
            key
            for key in _keys(state_db, "FAULT_INFO|*")
            if is_dldd_fault_payload(state_db.hgetall(key))
        }
        keys.update(fault_keys)

    state_db.delete_many(sorted(keys))

    state_removed = unlink_if_exists(state_file)

    artifacts = _clear_artifacts(artifact_directory) if include_artifacts else 0
    return ResetResult(len(keys), len(fault_keys), artifacts, state_removed)
