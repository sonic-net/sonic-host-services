"""Stable ownership markers for shared SONiC state."""

from __future__ import annotations

from typing import Mapping


DLDD_FAULT_PRODUCER = "dldd"


def is_dldd_fault_payload(payload: Mapping) -> bool:
    """Return whether a FAULT_INFO payload is explicitly owned by DLDD."""

    producer = payload.get("producer")
    if producer is None:
        producer = payload.get(b"producer")
    if isinstance(producer, bytes):
        producer = producer.decode("utf-8", "replace")
    return producer == DLDD_FAULT_PRODUCER
