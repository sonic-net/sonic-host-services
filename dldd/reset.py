"""Operator-requested cleanup of DLDD-owned runtime state."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Iterable

from .artifacts import DEFAULT_ARTIFACT_DIRECTORY
from .filesystem import unlink_if_exists
from .ownership import DLDD_FAULT_PRODUCER, is_dldd_fault_payload
from .sonic_hash import decode_db_text
from .telemetry import StateDB, TelemetryPublisher, positive_detection_time


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
            (name.startswith("dldd-") and name.endswith((".tar.gz", ".json")))
            or (name.startswith(".dldd-") and name.endswith(".tar.gz"))
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

    Redis keys are discovered before mutation. DLDD ownership markers protect
    FAULT_INFO rows written by another producer.
    """

    keys = {
        key
        for key in (TelemetryPublisher.STATUS_KEY, TelemetryPublisher.RULE_STATUS_KEY)
        if state_db.hgetall(key)
    }
    keys.update(_keys(state_db, TelemetryPublisher.RULE_STATUS_PREFIX + "*"))
    keys.update(_keys(state_db, TelemetryPublisher.RULE_DETAIL_PREFIX + "*"))

    faults = {}
    if include_faults:
        for key in _keys(state_db, "FAULT_INFO|*"):
            payload = state_db.hgetall(key)
            if not is_dldd_fault_payload(payload):
                continue
            if payload.get("status") not in ("ACTIVE", "INACTIVE"):
                raise ValueError("invalid DLDD fault status in {}".format(key))
            if not all(
                payload.get(field) for field in ("component_name", "symptom")
            ):
                raise ValueError("incomplete DLDD fault in {}".format(key))
            faults[key] = payload
        keys.update(faults)

        # A deleted telemetry row is not evidence of recovery to Healthz.
        # Publish only an explicit operator clear of an ACTIVE row. An
        # already inactive row is deleted without inventing or replaying an
        # assessment after a possible stream gap.
        transitions = []
        observed_at = str(int(time.time()))
        for key, payload in sorted(faults.items()):
            if payload["status"] != "ACTIVE":
                continue
            episode = (
                payload.get("origin_time")
                or payload.get("last_detection_time")
                or "legacy"
            )
            transition = {
                "producer": DLDD_FAULT_PRODUCER,
                "source_key": key,
                "transition_id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "dldd-clear-state:{}:{}:{}:{}".format(
                        key, episode, payload.get("occurrences", ""),
                        payload.get("rule_id", ""),
                    ),
                ).hex,
                "component": payload["component_name"],
                "component_type": payload.get("component_type", ""),
                "symptom": payload["symptom"],
                "active": "0",
                "observed_at": observed_at,
            }
            last_unhealthy_at = positive_detection_time(
                payload.get("last_detection_time")
            )
            if last_unhealthy_at:
                transition["last_unhealthy_at"] = last_unhealthy_at
            transitions.append(transition)

    if include_faults:
        state_db.clear_with_transitions(sorted(keys), faults, transitions)
    else:
        state_db.delete_many(sorted(keys))

    state_removed = unlink_if_exists(state_file)

    artifacts = _clear_artifacts(artifact_directory) if include_artifacts else 0
    return ResetResult(len(keys), len(faults), artifacts, state_removed)
