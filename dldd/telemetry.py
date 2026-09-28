"""STATE_DB publication and startup reconciliation helpers."""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import math
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from .config import DLDDConfig
from .ownership import DLDD_FAULT_PRODUCER
from .rule_schema.errors import bound_diagnostic
from .runtime import FaultRecord
from .sonic_hash import SonicHashReader, decode_db_hash, decode_db_text
from .timestamps import floor_timestamp, floor_timestamp_fields


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


def _artifact_id(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    return str(value.get("artifact_id") or "") if isinstance(value, Mapping) else ""


def _later_detection(current: str, previous: Optional[str]) -> bool:
    try:
        return float(current) > float(previous or 0)
    except (TypeError, ValueError):
        return False


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

    def replace_fault(
        self,
        key: str,
        values: Mapping[str, Any],
        ttl_seconds: Optional[int],
        transition: Mapping[str, str],
    ) -> Mapping[str, str]:
        """Compare, append a change, and replace the row atomically."""

        raise NotImplementedError

    def append_healthz_transition(self, transition: Mapping[str, str]) -> None:
        """Replay one persisted current transition after DLDD restarts."""

        raise NotImplementedError

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

    def fault_exists(self, key: str) -> bool:
        """Check whether the published fault row is still retained."""

        return bool(self.hgetall(key))

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
        existing = client.hkeys(key)
        with client.pipeline(transaction=True) as transaction:
            self._queue_hash(
                transaction, key, _redis_mapping(values), existing, ttl_seconds
            )
            transaction.execute()

    def replace_fault(
        self,
        key: str,
        values: Mapping[str, Any],
        ttl_seconds: Optional[int],
        transition: Mapping[str, str],
    ) -> Mapping[str, str]:
        from redis.exceptions import WatchError

        client = self._db()
        mapping = dict(_redis_mapping(values))
        stream = TelemetryPublisher.FAULT_TRANSITIONS_STREAM
        for _ in range(5):
            with client.pipeline(transaction=True) as transaction:
                try:
                    transaction.watch(key)
                    previous = decode_db_hash(transaction.hgetall(key))
                    changed = previous.get("status") != mapping["status"]
                    migration = (
                        not changed
                        and not previous.get("healthz_transition_id")
                        and transition.get("replay") == "1"
                    )
                    event = dict(transition)
                    current = dict(mapping)
                    if changed or migration:
                        current["healthz_transition_id"] = event["transition_id"]
                        current["healthz_transition_observed_at"] = event["observed_at"]
                        new_artifact = _artifact_id(current.get("healthz_artifact")) if changed else ""
                        old_artifact = _artifact_id(previous.get("healthz_artifact"))
                        current["healthz_transition_artifact_id"] = (
                            new_artifact if new_artifact != old_artifact else ""
                        )
                        if current["healthz_transition_artifact_id"]:
                            event["artifact_id"] = current["healthz_transition_artifact_id"]
                    else:
                        for field in (
                            "healthz_transition_id",
                            "healthz_transition_observed_at",
                            "healthz_transition_artifact_id",
                        ):
                            if field in previous:
                                current[field] = previous[field]
                            else:
                                current.pop(field, None)
                    observation = (
                        not changed and not migration
                        and current["status"] == "ACTIVE"
                        and _later_detection(
                            current.get("last_detection_time", ""),
                            previous.get("last_detection_time"),
                        )
                    )
                    if changed or migration or observation:
                        transaction.watch(stream)
                        if decode_db_text(transaction.type(stream)) not in (
                            "none", "stream"
                        ):
                            raise TypeError("HEALTHZ_TRANSITIONS key is not a stream")
                    transaction.multi()
                    if changed or migration:
                        transaction.xadd(
                            stream,
                            event,
                            maxlen=TelemetryPublisher.FAULT_TRANSITIONS_MAXLEN,
                            approximate=False,
                        )
                    elif observation:
                        transaction.xadd(
                            stream,
                            {
                                "kind": "observation",
                                "producer": current["producer"],
                                "source_key": key,
                                "component": current["component_name"],
                                "observed_at": current["last_detection_time"],
                            },
                            maxlen=TelemetryPublisher.FAULT_TRANSITIONS_MAXLEN,
                            approximate=False,
                        )
                    self._queue_hash(transaction, key, current, previous, ttl_seconds)
                    transaction.execute()
                    return {
                        field: current.get(field, "")
                        for field in (
                            "healthz_transition_id",
                            "healthz_transition_observed_at",
                            "healthz_transition_artifact_id",
                        )
                    }
                except WatchError:
                    continue
        raise RuntimeError("FAULT_INFO changed during publication")

    def append_healthz_transition(self, transition: Mapping[str, str]) -> None:
        self._db().xadd(
            TelemetryPublisher.FAULT_TRANSITIONS_STREAM,
            transition,
            maxlen=TelemetryPublisher.FAULT_TRANSITIONS_MAXLEN,
            approximate=False,
        )

    @staticmethod
    def _queue_hash(transaction, key, mapping, existing, ttl_seconds):
        stale_fields = tuple(
            sorted({decode_db_text(name) for name in existing} - set(mapping))
        )
        transaction.hset(key, mapping=mapping)
        if stale_fields:
            transaction.hdel(key, *stale_fields)
        if ttl_seconds is None:
            transaction.persist(key)
        else:
            transaction.expire(key, ttl_seconds)

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

    def fault_exists(self, key: str) -> bool:
        return bool(self._db().exists(key))

    def keys(self, pattern: str) -> Iterable[str]:
        if self._hash_reader is not None:
            return self._hash_reader.keys("STATE_DB", pattern)
        return tuple(sorted(
            decode_db_text(key) for key in self._db().scan_iter(match=pattern)
        ))


class TelemetryPublisher:
    """Publish bounded DLDD process and fault records to STATE_DB."""

    STATUS_KEY = "DLDD_STATUS|process_state"
    # Reset also removes the deprecated aggregate rule row.
    RULE_STATUS_KEY = "DLDD_RULE_STATUS|active"
    RULE_STATUS_PREFIX = "DLDD_RULE_STATUS|rule|"
    RULE_DETAIL_PREFIX = "DLDD_RULE_DETAIL|rule|"
    STATUS_TTL = 120
    FAULT_TRANSITIONS_STREAM = "HEALTHZ_TRANSITIONS"
    # Exact MAXLEN bounds the stream even if the consumer is unavailable.
    FAULT_TRANSITIONS_MAXLEN = 10000

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
        observation_time: Optional[float] = None,
        replay: bool = False,
        publication_time: Optional[float] = None,
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
        observed_at = observation_time
        if observed_at is None:
            if fault.status == "ACTIVE":
                observed_at = fault.last_detection_time
            elif fault.inactive_deadline is not None:
                observed_at = (
                    fault.inactive_deadline
                    - self.config.inactive_fault_retention_period
                )
            else:
                observed_at = time.time()
        new_transition = fault.healthz_transition_status != fault.status
        if new_transition:
            fault.healthz_transition_id = uuid.uuid4().hex
            fault.healthz_transition_status = fault.status
            fault.healthz_transition_observed_at = int(floor_timestamp(observed_at))
            fault.healthz_transition_artifact_id = ""
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
            "healthz_transition_id": fault.healthz_transition_id,
            "healthz_transition_observed_at": fault.healthz_transition_observed_at,
            "healthz_transition_artifact_id": fault.healthz_transition_artifact_id,
        }
        if fault.healthz_artifact is not None:
            payload["healthz_artifact"] = dict(fault.healthz_artifact)
        if fault.status == "INACTIVE" and fault.inactive_deadline is not None:
            payload["inactive_since"] = (
                fault.inactive_deadline
                - self.config.inactive_fault_retention_period
            )
        if fault.stale_source:
            payload["source_stale"] = True
        payload = _json_safe(floor_timestamp_fields(payload))
        try:
            published_at = time.time() if publication_time is None else publication_time
            ttl = (
                None
                if fault.status == "ACTIVE"
                else self.config.inactive_fault_retention_period
            )
            if ttl is not None and fault.inactive_deadline is not None:
                ttl = max(1, min(ttl, math.ceil(
                    fault.inactive_deadline - published_at
                )))
            transition = {
                "producer": DLDD_FAULT_PRODUCER,
                "source_key": fault.redis_key,
                "transition_id": fault.healthz_transition_id,
                "component": fault.component_name,
                "component_type": fault.component_type,
                "symptom": fault.symptom,
                "active": "1" if fault.status == "ACTIVE" else "0",
                "observed_at": str(fault.healthz_transition_observed_at),
            }
            if replay:
                transition["replay"] = "1"
            if fault.status == "INACTIVE" and fault.inactive_deadline is not None:
                transition["retain_until"] = str(math.ceil(published_at + ttl))
            committed = self.state_db.replace_fault(
                fault.redis_key, payload, ttl, transition
            )
            fault.healthz_transition_id = committed.get("healthz_transition_id", "")
            fault.healthz_transition_observed_at = int(float(
                committed.get("healthz_transition_observed_at") or 0
            ))
            fault.healthz_transition_artifact_id = committed.get(
                "healthz_transition_artifact_id", ""
            )
            return True
        except Exception as error:
            LOGGER.error("unable to publish %s: %s", fault.redis_key, error)
            return False

    def replay_fault_transition(self, payload: Mapping[str, Any]) -> None:
        """Re-emit only a retained row's last known transition with its ID."""

        transition_id = str(payload.get("healthz_transition_id") or "")
        if not transition_id:
            return  # Pre-integration rows carry no recoverable event identity.
        transition = {
            "producer": DLDD_FAULT_PRODUCER,
            "source_key": str(payload["redis_key"]),
            "transition_id": transition_id,
            "component": str(payload["component_name"]),
            "component_type": str(payload["component_type"]),
            "symptom": str(payload["symptom"]),
            "active": "1" if payload["status"] == "ACTIVE" else "0",
            "observed_at": str(payload["healthz_transition_observed_at"]),
            "replay": "1",
        }
        artifact_id = str(payload.get("healthz_transition_artifact_id") or "")
        if artifact_id:
            transition["artifact_id"] = artifact_id
        if payload["status"] == "INACTIVE" and payload.get("inactive_since"):
            transition["retain_until"] = str(math.ceil(max(
                float(payload["inactive_since"])
                + self.config.inactive_fault_retention_period,
                time.time() + 1,
            )))
        self.state_db.append_healthz_transition(transition)
        if payload["status"] == "ACTIVE" and _later_detection(
            str(payload.get("last_detection_time") or ""),
            str(payload["healthz_transition_observed_at"]),
        ):
            self.state_db.append_healthz_transition({
                "kind": "observation",
                "producer": DLDD_FAULT_PRODUCER,
                "source_key": str(payload["redis_key"]),
                "component": str(payload["component_name"]),
                "observed_at": str(payload["last_detection_time"]),
                "replay": "1",
            })

    def read_faults(self) -> Iterable[Mapping[str, Any]]:
        # Fail the snapshot rather than reconcile partial fault state.
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
