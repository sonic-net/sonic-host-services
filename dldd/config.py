"""DLDD configuration precedence and validation."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class DLDDConfig:
    """Validated runtime configuration assembled from vendor and CONFIG_DB data."""

    individual_max_failure_threshold: int = 10
    broken_rules_max_threshold: int = 5
    redis_monitor_polling_interval: int = 60
    file_monitor_polling_interval: int = 60
    common_monitor_polling_interval: int = 60
    source_unavailable_grace_period: int = 300
    source_recovery_samples: int = 1
    inactive_fault_retention_period: int = 3600
    fault_evidence_ack_timeout: int = 120
    active_fault_recheck_interval: int = 60
    rules_inbox_settle_time: int = 30

    @classmethod
    def from_sources(
        cls,
        config_db: Optional[Mapping[str, Any]] = None,
        vendor_defaults: Optional[Mapping[str, Any]] = None,
    ) -> "DLDDConfig":
        values = asdict(cls())
        for source in (vendor_defaults or {}, config_db or {}):
            for key, value in source.items():
                if key in values and value not in (None, ""):
                    values[key] = int(value)
        cls._validate(values)
        return cls(**values)

    @staticmethod
    def _validate(values: Mapping[str, int]) -> None:
        for key, value in values.items():
            if value < 0 or value > 0xFFFFFFFF:
                raise ValueError("{} must be an unsigned 32-bit integer".format(key))
        for key in (
            "redis_monitor_polling_interval",
            "file_monitor_polling_interval",
            "common_monitor_polling_interval",
            "source_recovery_samples",
            "fault_evidence_ack_timeout",
            "active_fault_recheck_interval",
            "rules_inbox_settle_time",
        ):
            if values[key] < 1:
                raise ValueError("{} must be at least 1".format(key))

    @property
    def polling_intervals(self) -> Mapping[str, int]:
        return {
            "redis": self.redis_monitor_polling_interval,
            "file": self.file_monitor_polling_interval,
            "common": self.common_monitor_polling_interval,
        }


def load_vendor_defaults(path: str) -> Mapping[str, Any]:
    """Load optional vendor defaults as a validated mapping."""

    if not os.path.exists(path):
        return {}
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("PyYAML is required for vendor defaults: {}".format(error))
    with open(path, "r", encoding="utf-8") as stream:
        document = yaml.safe_load(stream) or {}
    values = document.get("dldd_config", {})
    if not isinstance(values, dict):
        raise ValueError("dldd_config must be a mapping")
    return values


class ConfigDBProvider:
    """Thin swsscommon boundary, replaceable in tests and vendor builds."""

    def __init__(self, connector=None) -> None:
        self._connector = connector

    def _get_connector(self):
        if self._connector is None:
            try:
                from swsscommon import swsscommon
            except ImportError as error:
                raise RuntimeError("swsscommon is unavailable: {}".format(error))
            connector = swsscommon.ConfigDBConnector()
            connector.connect(wait_for_init=True)
            self._connector = connector
        return self._connector

    def load(self) -> Mapping[str, Any]:
        table = self._get_connector().get_table("DLDD_CONFIG") or {}
        return table.get("global", {})

    def listen(self, callback) -> None:
        connector = self._get_connector()

        def handle(_table, key, data):
            if key == "global":
                # Re-read the complete row because notifications may be partial.
                callback(self.load())

        connector.subscribe("DLDD_CONFIG", handle)
        connector.listen()

    def reset(self) -> None:
        self._connector = None
