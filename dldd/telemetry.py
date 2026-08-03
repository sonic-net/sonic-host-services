"""STATE_DB publication and startup reconciliation helpers."""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.parse import quote

from .config import DLDDConfig
from .models import ValueConfig
from .ownership import DLDD_FAULT_PRODUCER
from .rule_schema.errors import bound_diagnostic
from .runtime import FaultRecord
from .sonic_hash import SonicHashReader
from .timestamps import floor_timestamp_fields


LOGGER = logging.getLogger(__name__)


EMPTY_ASYNC_POOL_METRICS = {
    "async_pool_workers": 0,
    "async_pool_busy": 0,
    "async_pool_queued": 0,
    "async_pool_avg_queue_latency_ms": 0.0,
    "async_pool_avg_execution_time_ms": 0.0,
    "async_pool_avg_utilization_percent": 0.0,
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return list(value)
    if isinstance(value, Mapping):
        return {str(name): _json_safe(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _redis_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            _json_safe(value), sort_keys=True, separators=(",", ":")
        )
    return str(value)


class StateDB:
    """Minimal hash/TTL interface used by the daemon."""

    def hset(self, key: str, values: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def expire(self, key: str, seconds: int) -> None:
        raise NotImplementedError

    def hset_with_ttl(
        self, key: str, values: Mapping[str, Any], seconds: int
    ) -> None:
        self.hset(key, values)
        self.expire(key, seconds)

    def persist(self, key: str) -> None:
        raise NotImplementedError

    def hdel(self, key: str, fields: Iterable[str]) -> None:
        raise NotImplementedError

    def replace_hash(
        self, key: str, values: Mapping[str, Any], ttl_seconds: Optional[int]
    ) -> None:
        """Replace a complete logical row and its TTL.

        Test and alternate backends get a correct fallback.  The production
        backend overrides this with one Redis transaction.
        """

        existing = self.hgetall(key)
        existing_fields = {
            name.decode() if isinstance(name, bytes) else name
            for name in existing
        }
        stale_fields = existing_fields - set(values)
        self.hset(key, values)
        if stale_fields:
            self.hdel(key, stale_fields)
        if ttl_seconds is None:
            self.persist(key)
        else:
            self.expire(key, ttl_seconds)

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def delete_many(self, keys: Iterable[str]) -> None:
        for key in tuple(keys):
            self.delete(key)

    def hgetall(self, key: str) -> Mapping[str, str]:
        raise NotImplementedError

    def keys(self, pattern: str) -> Iterable[str]:
        raise NotImplementedError


class SonicStateDB(StateDB):
    def __init__(self, redis_client=None, hash_reader=None) -> None:
        self._redis_client = redis_client
        self._hash_reader = hash_reader or (
            SonicHashReader() if redis_client is None else None
        )

    def _db(self):
        if self._redis_client is None:
            try:
                import redis
                from swsscommon import swsscommon
            except ImportError as error:
                raise RuntimeError(
                    "STATE_DB dependencies are unavailable: {}".format(error)
                )

            database = "STATE_DB"
            database_key = swsscommon.SonicDBKey()
            database_id = swsscommon.SonicDBConfig.getDbId(
                database, database_key
            )
            socket_path = swsscommon.SonicDBConfig.getDbSock(
                database, database_key
            )
            if socket_path:
                self._redis_client = redis.Redis(
                    unix_socket_path=socket_path,
                    db=database_id,
                )
            else:
                self._redis_client = redis.Redis(
                    host=swsscommon.SonicDBConfig.getDbHostname(
                        database, database_key
                    ),
                    port=swsscommon.SonicDBConfig.getDbPort(
                        database, database_key
                    ),
                    db=database_id,
                )
        return self._redis_client

    def hset(self, key: str, values: Mapping[str, Any]) -> None:
        mapping = {name: _redis_value(value) for name, value in values.items()}
        self._db().hset(key, mapping=mapping)

    def hset_with_ttl(
        self, key: str, values: Mapping[str, Any], seconds: int
    ) -> None:
        client = self._db()
        mapping = {name: _redis_value(value) for name, value in values.items()}
        transaction = client.pipeline(transaction=True)
        transaction.hset(key, mapping=mapping)
        transaction.expire(key, seconds)
        transaction.execute()

    def expire(self, key: str, seconds: int) -> None:
        self._db().expire(key, seconds)

    def persist(self, key: str) -> None:
        self._db().persist(key)

    def hdel(self, key: str, fields: Iterable[str]) -> None:
        fields = tuple(fields)
        if not fields:
            return
        self._db().hdel(key, *fields)

    def replace_hash(
        self, key: str, values: Mapping[str, Any], ttl_seconds: Optional[int]
    ) -> None:
        client = self._db()
        mapping = {name: _redis_value(value) for name, value in values.items()}
        existing = client.hkeys(key)
        existing_fields = {
            name.decode() if isinstance(name, bytes) else name
            for name in existing
        }
        stale_fields = tuple(sorted(existing_fields - set(mapping)))
        transaction = client.pipeline(transaction=True)
        transaction.hset(key, mapping=mapping)
        if stale_fields:
            transaction.hdel(key, *stale_fields)
        if ttl_seconds is None:
            transaction.persist(key)
        else:
            transaction.expire(key, ttl_seconds)
        transaction.execute()

    def delete(self, key: str) -> None:
        self._db().delete(key)

    def delete_many(self, keys: Iterable[str]) -> None:
        keys = tuple(keys)
        if keys:
            self._db().delete(*keys)

    def hgetall(self, key: str) -> Mapping[str, str]:
        if self._hash_reader is not None:
            return self._hash_reader.read("STATE_DB", key)
        return self._db().hgetall(key)

    def keys(self, pattern: str) -> Iterable[str]:
        return self._db().scan_iter(match=pattern)


class TelemetryPublisher:
    STATUS_KEY = "DLDD_STATUS|process_state"
    RULE_STATUS_KEY = "DLDD_RULE_STATUS|active"
    RULE_STATUS_PREFIX = "DLDD_RULE_STATUS|rule|"
    RULE_DETAIL_PREFIX = "DLDD_RULE_DETAIL|rule|"
    STATUS_TTL = 120

    def __init__(
        self,
        state_db: StateDB,
        config: DLDDConfig,
        serial_resolver: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        self.state_db = state_db
        self.config = config
        self.serial_resolver = serial_resolver

    def publish_status(
        self,
        state: str,
        running_schema: str,
        active_rules_file: str,
        active_rules_checksum: str,
        broken_rules=(),
        source_status=(),
        inflight_fault_evidence=(),
        service_diagnostics=(),
        reason: str = "",
        local_action_default_timeout: Optional[int] = None,
        active_rules_source: str = "",
        activation_result: str = "",
        activation_fallback_used: bool = False,
        previous_active_rules_checksum: str = "",
        async_pool_metrics: Optional[Mapping[str, float]] = None,
    ) -> bool:
        pool_metrics = dict(EMPTY_ASYNC_POOL_METRICS)
        if async_pool_metrics:
            pool_metrics.update(async_pool_metrics)
        payload = {
            "state": state,
            "running_schema": running_schema,
            "active_rules_file": active_rules_file,
            "active_rules_checksum": active_rules_checksum,
            "active_rules_source": active_rules_source,
            "activation_result": activation_result,
            "activation_fallback_used": activation_fallback_used,
            "previous_active_rules_checksum": previous_active_rules_checksum,
            **pool_metrics,
            **asdict(self.config),
            "local_action_default_timeout": local_action_default_timeout,
            "broken_rules": list(broken_rules),
            "source_status": list(source_status),
            "inflight_fault_evidence": list(inflight_fault_evidence),
            "service_diagnostics": list(service_diagnostics),
            "reason": reason,
        }
        payload = floor_timestamp_fields(payload)
        try:
            self.state_db.hset_with_ttl(self.STATUS_KEY, payload, self.STATUS_TTL)
            return True
        except Exception as error:
            LOGGER.error("unable to publish DLDD_STATUS: %s", error)
            return False

    def publish_rule_status(
        self,
        active_rules_checksum: str,
        rules=(),
        detail_truncated: bool = False,
    ) -> bool:
        """Publish one bounded hash per rule plus a small active index."""

        published_at = floor_timestamp_fields(
            {"published_at": time.time()}
        )["published_at"]
        status_keys = []
        detail_keys = []
        try:
            for index, rule_value in enumerate(rules):
                rule = dict(rule_value)
                identity = str(
                    rule.get("rule")
                    or rule.get("rule_id")
                    or "unknown-{}".format(index)
                )
                suffix = quote(identity, safe="")
                status_key = self.RULE_STATUS_PREFIX + suffix
                detail_key = self.RULE_DETAIL_PREFIX + suffix
                work_items = list(rule.pop("work_items", ()) or ())
                summary = {
                    **rule,
                    "active_rules_checksum": active_rules_checksum,
                    "detail_key": detail_key,
                    "published_at": published_at,
                }
                detail = {
                    "active_rules_checksum": active_rules_checksum,
                    "rule_id": rule.get("rule_id"),
                    "rule": rule.get("rule", ""),
                    "work_items": work_items,
                    "published_at": published_at,
                }
                self.state_db.replace_hash(
                    detail_key,
                    floor_timestamp_fields(detail),
                    self.STATUS_TTL,
                )
                self.state_db.replace_hash(
                    status_key,
                    floor_timestamp_fields(summary),
                    self.STATUS_TTL,
                )
                status_keys.append(status_key)
                detail_keys.append(detail_key)

            index_payload = {
                "active_rules_checksum": active_rules_checksum,
                "rule_keys": status_keys,
                "rule_count": len(status_keys),
                "detail_truncated": detail_truncated,
                "published_at": published_at,
            }
            self.state_db.replace_hash(
                self.RULE_STATUS_KEY,
                index_payload,
                self.STATUS_TTL,
            )
            keep = set(status_keys + detail_keys + [self.RULE_STATUS_KEY])
            for pattern in (
                self.RULE_STATUS_PREFIX + "*",
                self.RULE_DETAIL_PREFIX + "*",
            ):
                for raw_key in tuple(self.state_db.keys(pattern)):
                    key = (
                        raw_key.decode()
                        if isinstance(raw_key, bytes)
                        else raw_key
                    )
                    if key not in keep:
                        self.state_db.delete(key)
            return True
        except Exception as error:
            LOGGER.error("unable to publish DLDD_RULE_STATUS: %s", error)
            return False

    def clear_rule_status(self) -> bool:
        try:
            keys = {self.RULE_STATUS_KEY}
            for pattern in (
                self.RULE_STATUS_PREFIX + "*",
                self.RULE_DETAIL_PREFIX + "*",
            ):
                keys.update(
                    raw_key.decode()
                    if isinstance(raw_key, bytes)
                    else raw_key
                    for raw_key in self.state_db.keys(pattern)
                )
            for key in keys:
                self.state_db.delete(key)
            return True
        except Exception as error:
            LOGGER.error("unable to clear DLDD_RULE_STATUS: %s", error)
            return False

    def publish_fault(
        self,
        fault: FaultRecord,
        serial_number: Optional[str] = None,
        remote_action_time_window: int = 0,
        local_action_details: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        if serial_number is None:
            serial_number = fault.serial_number
            if not serial_number and self.serial_resolver is not None:
                try:
                    serial_number = str(
                        self.serial_resolver(
                            fault.component_type, fault.component_name
                        )
                        or ""
                    )
                    fault.serial_number = serial_number
                except Exception as error:
                    LOGGER.warning(
                        "unable to resolve serial number for %s: %s",
                        fault.component_name,
                        error,
                    )
        payload = {
            "producer": DLDD_FAULT_PRODUCER,
            "rule": fault.rule_name,
            "rule_id": fault.rule_id,
            "rule_version": fault.rule_version,
            "schema_version": fault.schema_version,
            "active_rules_checksum": fault.active_rules_checksum,
            "component_type": fault.component_type,
            "component_name": fault.component_name,
            "component_serial_number": serial_number,
            "error_type": fault.error_type,
            "events": list(fault.events),
            "remote_action_time_window": remote_action_time_window,
            "repair_actions": [
                {"action": action} for action in fault.repair_actions
            ],
            "actions_taken": list(fault.actions_taken),
            "local_action_state": dict(
                local_action_details
                or fault.local_action_details
                or {
                    "state": fault.local_action_state,
                    "action_suppressed": fault.action_suppressed,
                    "last_error": "",
                }
            ),
            "severity": fault.severity,
            "symptom": fault.symptom,
            "status": fault.status,
            "origin_time": fault.origin_time,
            "last_detection_time": fault.last_detection_time,
            "occurrences": fault.occurrences,
            "description": fault.description,
            "reason": bound_diagnostic(str(fault.reason), 512),
        }
        if fault.healthz_artifact is not None:
            payload["healthz_artifact"] = dict(fault.healthz_artifact)
        if fault.stale_source:
            payload["source_stale"] = True
        payload = _json_safe(floor_timestamp_fields(payload))
        try:
            ttl = (
                None
                if fault.status == "ACTIVE"
                else self.config.inactive_fault_retention_period
            )
            self.state_db.replace_hash(fault.redis_key, payload, ttl)
            return True
        except Exception as error:
            LOGGER.error("unable to publish %s: %s", fault.redis_key, error)
            return False

    def read_faults(self) -> Iterable[Mapping[str, Any]]:
        # Startup reconciliation needs one complete view of retained faults.
        # Let scan or row-read failures reach the service retry boundary rather
        # than silently starting monitors from a partial snapshot.
        keys = tuple(self.state_db.keys("FAULT_INFO|*"))
        rows = []
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            raw = self.state_db.hgetall(key)
            decoded: Dict[str, Any] = {}
            for raw_name, raw_value in raw.items():
                name = raw_name.decode() if isinstance(raw_name, bytes) else raw_name
                value = raw_value.decode() if isinstance(raw_value, bytes) else raw_value
                if name in (
                    "events",
                    "repair_actions",
                    "actions_taken",
                    "local_action_state",
                    "healthz_artifact",
                ):
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        pass
                decoded[name] = value
            decoded["redis_key"] = key
            rows.append(decoded)
        return tuple(rows)


def value_config_payload(config: ValueConfig) -> Mapping[str, Any]:
    return config.as_payload()
