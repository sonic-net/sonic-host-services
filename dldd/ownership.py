"""Stable ownership markers for shared SONiC state."""

from __future__ import annotations

from typing import Mapping

from .sonic_hash import decode_db_text


DLDD_FAULT_PRODUCER = "dldd"


def is_dldd_fault_payload(payload: Mapping) -> bool:
    """Return whether a FAULT_INFO payload is explicitly owned by DLDD."""

    producer = payload.get("producer")
    if producer is None:
        producer = payload.get(b"producer")
    return (
        producer is not None
        and decode_db_text(producer) == DLDD_FAULT_PRODUCER
    )
