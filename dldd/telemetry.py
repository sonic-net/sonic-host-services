"""STATE_DB publication and startup reconciliation helpers."""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from .config import DLDDConfig
from .ownership import DLDD_FAULT_PRODUCER
from .rule_schema.errors import bound_diagnostic
from .runtime import FaultRecord
from .sonic_hash import SonicHashReader, decode_db_hash, decode_db_text
from .timestamps import floor_timestamp_fields


LOGGER = logging.getLogger(__name__)


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


def _redis_mapping(values: Mapping[str, Any]) -> Mapping[str, str]:
    """Encode one logical telemetry row for the Redis client boundary."""

    return {name: _redis_value(value) for name, value in values.items()}


class StateDB:
    """Minimal hash/TTL interface used by the daemon."""

    def hset(self, key: str, values: Mapping[str, Any]) -> None:
        """Merge fields into one hash."""

        raise NotImplementedError

    def expire(self, key: str, seconds: int) -> None:
        """Set the hash expiry in seconds."""

        raise NotImplementedError

    def hset_with_ttl(
        self, key: str, values: Mapping[str, Any], seconds: int
    ) -> None:
        """Merge fields and set their hash expiry."""

        self.hset(key, values)
        self.expire(key, seconds)

    def persist(self, key: str) -> None:
        """Remove any expiry from a hash."""

        raise NotImplementedError

    def hdel(self, key: str, fields: Iterable[str]) -> None:
        """Delete selected fields from a hash."""

        raise NotImplementedError

    def replace_hash(
        self, key: str, values: Mapping[str, Any], ttl_seconds: Optional[int]
    ) -> None:
        """Replace a complete logical row and its TTL.

        Test and alternate backends get a correct fallback.  The production
        backend overrides this with one Redis transaction.
        """

        existing = self.hgetall(key)
        existing_fields = {decode_db_text(name) for name in existing}
        stale_fields = existing_fields - set(values)
        self.hset(key, values)
        if stale_fields:
            self.hdel(key, stale_fields)
        if ttl_seconds is None:
            self.persist(key)
        else:
            self.expire(key, ttl_seconds)

    def delete(self, key: str) -> None:
        """Delete one key."""

        raise NotImplementedError

    def delete_many(self, keys: Iterable[str]) -> None:
        """Delete zero or more keys."""

        for key in tuple(keys):
            self.delete(key)

    def hgetall(self, key: str) -> Mapping[str, str]:
        """Return one hash with text field names and values."""

        raise NotImplementedError

    def keys(self, pattern: str) -> Iterable[str]:
        """Return text keys matching a database pattern."""

        raise NotImplementedError


class SonicStateDB(StateDB):
    """Redis write API paired with normalized SONiC read access."""

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
            connection = {"db": database_id}
            if socket_path:
                connection["unix_socket_path"] = socket_path
            else:
                connection.update(
                    host=swsscommon.SonicDBConfig.getDbHostname(database, database_key),
                    port=swsscommon.SonicDBConfig.getDbPort(database, database_key),
                )
            self._redis_client = redis.Redis(**connection)
        return self._redis_client

    def hset(self, key: str, values: Mapping[str, Any]) -> None:
        self._db().hset(key, mapping=_redis_mapping(values))

    def hset_with_ttl(
        self, key: str, values: Mapping[str, Any], seconds: int
    ) -> None:
        client = self._db()
        transaction = client.pipeline(transaction=True)
        transaction.hset(key, mapping=_redis_mapping(values))
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
        mapping = _redis_mapping(values)
        existing = client.hkeys(key)
        existing_fields = {decode_db_text(name) for name in existing}
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
        return decode_db_hash(self._db().hgetall(key))

    def keys(self, pattern: str) -> Iterable[str]:
        if self._hash_reader is not None:
            return self._hash_reader.keys("STATE_DB", pattern)
        return tuple(sorted(
            decode_db_text(key) for key in self._db().scan_iter(match=pattern)
        ))


class TelemetryPublisher:
    """Publish bounded DLDD process and fault records to STATE_DB."""

    STATUS_KEY = "DLDD_STATUS|process_state"
    # Retained only so reset can remove telemetry produced by older images.
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
        rule_count: int = 0,
        active_fault_count: int = 0,
        broken_rules=(),
        source_status=(),
        inflight_count: int = 0,
        reason: str = "",
        active_rules_source: str = "",
        activation_result: str = "",
    ) -> bool:
        broken_rules = list(broken_rules)
        source_status = list(source_status)
        payload = {
            "state": state,
            "running_schema": running_schema,
            "active_rules_file": active_rules_file,
            "active_rules_checksum": active_rules_checksum,
            "active_rules_source": active_rules_source,
            "activation_result": activation_result,
            "rule_count": rule_count,
            "active_fault_count": active_fault_count,
            "rule_exception_count": len(broken_rules),
            "source_exception_count": len(source_status),
            "inflight_count": inflight_count,
            "broken_rules": broken_rules,
            "source_status": source_status,
            "effective_config": asdict(self.config),
            "reason": reason,
        }
        payload = floor_timestamp_fields(payload)
        try:
            self.state_db.replace_hash(self.STATUS_KEY, payload, self.STATUS_TTL)
            return True
        except Exception as error:
            LOGGER.error("unable to publish DLDD_STATUS: %s", error)
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
            key = decode_db_text(raw_key)
            raw = decode_db_hash(self.state_db.hgetall(key))
            decoded: Dict[str, Any] = {}
            for name, value in raw.items():
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
