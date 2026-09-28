from __future__ import absolute_import

import json

import pytest

from dldd.config import DLDDConfig
from dldd.runtime import FaultRecord
from dldd.telemetry import (
    SonicStateDB,
    TelemetryPublisher,
)
from tests.dldd_fakes import FakeStateDB


class RecordingPipeline(object):
    def __init__(self, client):
        self.client = client
        self.operations = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def watch(self, *keys):
        self.operations.append(("watch", keys))

    def type(self, key):
        self.operations.append(("type", key))
        if key == "HEALTHZ_TRANSITIONS":
            return self.client.stream_type
        return self.client.fault_type

    def hgetall(self, key):
        self.operations.append(("hgetall", key))
        return {b"status": self.client.status} if self.client.status else {}

    def multi(self):
        self.operations.append(("multi",))

    def xadd(self, key, fields, maxlen, approximate):
        self.operations.append(("xadd", key, fields, maxlen, approximate))

    def hset(self, key, mapping):
        self.operations.append(("hset", key, mapping))

    def hdel(self, key, *fields):
        self.operations.append(("hdel", key, fields))

    def persist(self, key):
        self.operations.append(("persist", key))

    def expire(self, key, seconds):
        self.operations.append(("expire", key, seconds))

    def execute(self):
        self.operations.append(("execute",))
        if self.client.execute_error is not None:
            raise self.client.execute_error


class RecordingRedisClient(object):
    def __init__(
        self, fields=(), status=None, fault_type=b"hash",
        stream_type=b"stream", execute_error=None,
    ):
        self.fields = fields
        self.status = status
        self.fault_type = fault_type
        self.stream_type = stream_type
        self.execute_error = execute_error
        self.transaction = RecordingPipeline(self)
        self.scan_pattern = None
        self.deleted = []
        self.direct = []

    def hset(self, key, mapping):
        self.direct.append(("hset", key, mapping))

    def expire(self, key, seconds):
        self.direct.append(("expire", key, seconds))

    def persist(self, key):
        self.direct.append(("persist", key))

    def hdel(self, key, *fields):
        self.direct.append(("hdel", key, fields))

    def hgetall(self, key):
        self.direct.append(("hgetall", key))
        return {b"status": b"ACTIVE"}

    def hkeys(self, key):
        return self.fields

    def hget(self, key, field):
        assert field == "status"
        return self.status

    def pipeline(self, transaction=True):
        assert transaction
        return self.transaction

    def scan_iter(self, match):
        self.scan_pattern = match
        return iter((b"FAULT_INFO|PSU0|SYMPTOM",))

    def delete(self, *keys):
        self.deleted.append(keys)


def fault(status="ACTIVE"):
    return FaultRecord(
        rule_id=1000001,
        rule_name="PSU_FAULT",
        rule_version="1.0.0",
        schema_version="0.0.1",
        active_rules_checksum="sha256:test",
        component_type="PSU",
        component_name="PSU0",
        symptom="SYMPTOM_OVER_THRESHOLD",
        severity="CRITICAL",
        priority=1,
        error_type="POWER",
        status=status,
        events=({"id": 1},),
        repair_actions=("ACTION_RESEAT",),
    )

def test_status_publication_ttl_failure_and_reason_boundary_contract(caplog):
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    publisher.publish_status(
        "OK",
        "0.0.1",
        "/active",
        "sha256:test",
        active_rules_source="inbox",
        activation_result="DEGRADED",
        rule_count=12,
        active_fault_count=2,
        broken_rules=({"rule": "bad"},),
        source_status=({"source": "redis"},),
    )
    status = database.values[publisher.STATUS_KEY]
    assert database.ttls[publisher.STATUS_KEY] == 120
    assert status["active_rules_source"] == "inbox"
    assert status["activation_result"] == "DEGRADED"
    assert status["rule_count"] == "12"
    assert status["active_fault_count"] == "2"
    assert status["rule_exception_count"] == "1"
    assert status["source_exception_count"] == "1"
    assert "async_pool_workers" not in status

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    database.fail_writes_with(RuntimeError("STATE_DB unavailable"))

    assert not publisher.publish_status("OK", "schema", "/active", "checksum")
    assert "STATE_DB unavailable" in caplog.text

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault("INACTIVE")
    record.reason = "x" * 2048
    assert publisher.publish_fault(record)
    assert len(database.values[record.redis_key]["reason"].encode()) <= 512


def test_publications_floor_timestamps_and_preserve_duration_precision():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    publisher.publish_status(
        "DEGRADED",
        "0.0.1",
        "/active",
        "sha256:test",
        broken_rules=({"last_attempt": 100.9},),
    )
    status = database.values[publisher.STATUS_KEY]
    assert json.loads(status["broken_rules"])[0]["last_attempt"] == 100

    record = fault()
    record.origin_time = 110.8
    record.events = (
        {"event_timestamp": 112.6, "match_period": 5.5},
    )
    publisher.publish_fault(
        record,
        remote_action_time_window=30.5,
    )
    published_fault = database.values[record.redis_key]
    assert published_fault["producer"] == "dldd"
    assert published_fault["origin_time"] == "110"
    assert json.loads(published_fault["events"])[0] == {
        "event_timestamp": 112,
        "match_period": 5.5,
    }
    assert published_fault["remote_action_time_window"] == "30.5"


def test_fault_payload_identity_json_replacement_and_inactive_ttl_contract():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    publisher.publish_fault(record, serial_number="serial")
    assert record.redis_key not in database.ttls
    payload = database.values[record.redis_key]
    assert payload["producer"] == "dldd"
    assert payload["component_type"] == "PSU"
    assert payload["component_name"] == "PSU0"
    assert payload["component_serial_number"] == "serial"
    assert "component_info" not in payload
    assert json.loads(
        database.values[record.redis_key]["repair_actions"]
    )[0]["action"] == "ACTION_RESEAT"

    record.repair_actions = ("vendor-healthz:ACTION_REPAIR_FABRIC_MODULE",)
    record.events = ({"id": 1, "value_read": b"\x00\xff"},)
    publisher.publish_fault(record)
    payload = database.values[record.redis_key]
    assert json.loads(payload["repair_actions"])[0]["action"].startswith(
        "vendor-healthz:"
    )
    assert json.loads(payload["events"])[0]["value_read"] == [0, 255]

    # Inactive publication applies retention and complete hash replacement.
    database = FakeStateDB()
    config = DLDDConfig(inactive_fault_retention_period=42)
    publisher = TelemetryPublisher(database, config)
    record = fault("INACTIVE")
    publisher.publish_fault(record)
    assert database.ttls[record.redis_key] == 42

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    record.healthz_artifact = {"artifact_id": "old", "state": "COMPLETED"}
    publisher.publish_fault(record)
    assert "healthz_artifact" in database.values[record.redis_key]

    record.healthz_artifact = None
    publisher.publish_fault(record)
    assert "healthz_artifact" not in database.values[record.redis_key]
    assert database.delete_calls == 0

    record.component_name = "PSU|0"
    assert record.redis_key == "FAULT_INFO|PSU%7C0|SYMPTOM_OVER_THRESHOLD"


def test_fault_stream_records_only_first_publication_and_status_changes():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    record.healthz_artifact = {"artifact_id": "first.tar.gz"}

    assert publisher.publish_fault(record, observation_time=100.8)
    stream = database.streams[publisher.FAULT_TRANSITIONS_STREAM]
    assert len(stream) == 1
    transition = stream[0][1]
    assert transition == {
        "producer": "dldd",
        "source_key": record.redis_key,
        "transition_id": record.healthz_transition_id,
        "component": "PSU0",
        "component_type": "PSU",
        "symptom": "SYMPTOM_OVER_THRESHOLD",
        "active": "1",
        "observed_at": "100",
        "artifact_id": "first.tar.gz",
    }
    assert database.values[record.redis_key]["healthz_transition_id"] == transition["transition_id"]
    assert database.values[record.redis_key]["healthz_transition_artifact_id"] == "first.tar.gz"

    record.reason = "metadata refresh"
    assert publisher.publish_fault(record, observation_time=101)
    assert len(stream) == 1

    record.status = "INACTIVE"
    record.inactive_deadline = 102 + publisher.config.inactive_fault_retention_period
    assert publisher.publish_fault(record, observation_time=102)
    assert len(stream) == 2
    assert stream[-1][1]["active"] == "0"
    assert stream[-1][1]["observed_at"] == "102"
    # The retained row carries the prior artifact; consumers must not treat
    # it as a new archive on the recovery event.
    assert "artifact_id" not in stream[-1][1]
    assert database.values[record.redis_key]["healthz_transition_artifact_id"] == ""

    record.status = "ACTIVE"
    record.occurrences += 1
    record.healthz_artifact = None
    assert publisher.publish_fault(record, observation_time=103)
    assert len(stream) == 3
    assert stream[-1][1]["active"] == "1"
    assert "artifact_id" not in stream[-1][1]


def test_first_inactive_publication_and_failed_write_retry_stream_once():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault("INACTIVE")
    record.healthz_artifact = {"artifact_id": "recovered.tar.gz"}
    database.fail_writes_with(RuntimeError("STATE_DB unavailable"))

    assert not publisher.publish_fault(record, observation_time=202)
    assert record.redis_key not in database.values
    assert not database.streams

    database.clear_failures()
    assert publisher.publish_fault(record, observation_time=202)
    assert publisher.publish_fault(record, observation_time=202)
    stream = database.streams[publisher.FAULT_TRANSITIONS_STREAM]
    assert len(stream) == 1
    assert stream[0][1]["active"] == "0"
    assert stream[0][1]["artifact_id"] == "recovered.tar.gz"

    # An expired row is a first publication again. The watched Redis
    # transaction makes the status comparison at the write boundary.
    database.delete(record.redis_key)
    assert publisher.publish_fault(record, observation_time=203)
    assert len(stream) == 2
    assert stream[-1][1]["transition_id"] == stream[0][1]["transition_id"]
    assert stream[-1][1]["observed_at"] == "202"


def test_inactive_metadata_refresh_keeps_original_retention_deadline(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("dldd.telemetry.time.time", lambda: now[0])
    config = DLDDConfig(inactive_fault_retention_period=42)
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, config)
    record = fault("INACTIVE")
    record.inactive_deadline = 1042.0

    assert publisher.publish_fault(record, observation_time=1000)
    assert database.ttls[record.redis_key] == 42
    transition_id = record.healthz_transition_id

    now[0] = 1015.0
    record.healthz_artifact = {"artifact_id": "archive.tar.gz", "state": "MISSING"}
    assert publisher.publish_fault(record)
    assert database.ttls[record.redis_key] == 27
    assert len(database.streams[publisher.FAULT_TRANSITIONS_STREAM]) == 1
    assert record.healthz_transition_id == transition_id


def test_delayed_first_inactive_publication_uses_remaining_deadline(monkeypatch):
    monkeypatch.setattr("dldd.telemetry.time.time", lambda: 1100.0)
    database = FakeStateDB()
    publisher = TelemetryPublisher(
        database, DLDDConfig(inactive_fault_retention_period=42)
    )
    record = fault("INACTIVE")
    record.inactive_deadline = 1042.0

    assert publisher.publish_fault(
        record, observation_time=1000, publication_time=1100
    )
    assert database.ttls[record.redis_key] == 1
    first = database.streams[publisher.FAULT_TRANSITIONS_STREAM][0][1]
    assert first["active"] == "0"
    assert first["observed_at"] == "1000"
    assert first["retain_until"] == "1101"
    assert database.values[record.redis_key]["inactive_since"] == "1000.0"

    publisher.replay_fault_transition(tuple(publisher.read_faults())[0])
    replay = database.streams[publisher.FAULT_TRANSITIONS_STREAM][-1][1]
    assert replay["transition_id"] == first["transition_id"]
    assert replay["retain_until"] == "1101"


def test_startup_replays_only_the_retained_transition_and_latest_observation():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    record.last_detection_time = 100
    record.healthz_artifact = {"artifact_id": "archive.tar.gz"}
    assert publisher.publish_fault(record, observation_time=100)
    first_id = record.healthz_transition_id

    record.last_detection_time = 105
    assert publisher.publish_fault(record, observation_time=105)
    assert [entry[1].get("kind") for entry in database.streams[publisher.FAULT_TRANSITIONS_STREAM]] == [
        None, "observation",
    ]

    retained = tuple(publisher.read_faults())[0]
    database.streams[publisher.FAULT_TRANSITIONS_STREAM].clear()
    publisher.replay_fault_transition(retained)
    replayed = [entry[1] for entry in database.streams[publisher.FAULT_TRANSITIONS_STREAM]]
    assert replayed[0]["transition_id"] == first_id
    assert replayed[0]["artifact_id"] == "archive.tar.gz"
    assert replayed[0]["replay"] == "1"
    assert replayed[1] == {
        "kind": "observation",
        "producer": "dldd",
        "source_key": record.redis_key,
        "component": "PSU0",
        "observed_at": "105",
        "replay": "1",
    }


def test_production_state_db_hash_replacement_and_transaction_contract():
    client = RecordingRedisClient((b"status", b"healthz_artifact"))
    database = SonicStateDB(client)
    database.replace_hash("FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None)

    names = [operation[0] for operation in client.transaction.operations]
    assert names == ["hset", "hdel", "persist", "execute"]
    assert "delete" not in names

    client = RecordingRedisClient((b"status", b"healthz_artifact"))
    database = SonicStateDB(client)
    database.replace_hash(
        "FAULT_INFO|PSU0|SYMPTOM", {"status": "INACTIVE"}, 42
    )
    assert client.transaction.operations == [
        ("hset", "FAULT_INFO|PSU0|SYMPTOM", {"status": "INACTIVE"}),
        ("hdel", "FAULT_INFO|PSU0|SYMPTOM", ("healthz_artifact",)),
        ("expire", "FAULT_INFO|PSU0|SYMPTOM", 42),
        ("execute",),
    ]

    client = RecordingRedisClient()
    database = SonicStateDB(client)
    transition = {
        "producer": "dldd", "transition_id": "transition-1",
        "active": "1", "observed_at": "100",
    }
    database.replace_fault(
        "FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None,
        transition,
    )
    names = [operation[0] for operation in client.transaction.operations]
    assert names == [
        "watch", "hgetall", "watch", "type", "multi", "xadd",
        "hset", "persist", "execute",
    ]
    assert client.transaction.operations[5] == (
        "xadd", "HEALTHZ_TRANSITIONS",
        transition, 10000, False,
    )
    assert not client.direct

    client = RecordingRedisClient(status=b"ACTIVE", fields=(b"status",))
    SonicStateDB(client).replace_fault(
        "FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None,
        transition,
    )
    assert "xadd" not in [operation[0] for operation in client.transaction.operations]

    client = RecordingRedisClient(stream_type=b"string")
    database = SonicStateDB(client)
    with pytest.raises(TypeError, match="HEALTHZ_TRANSITIONS"):
        database.replace_fault(
            "FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None,
            transition,
        )
    assert "execute" not in [operation[0] for operation in client.transaction.operations]

    client = RecordingRedisClient(execute_error=RuntimeError("EXEC failed"))
    with pytest.raises(RuntimeError, match="EXEC failed"):
        SonicStateDB(client).replace_fault(
            "FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None,
            transition,
        )
    # A Redis EXEC error can still apply other queued commands; no rollback
    # or claim that the fault row stayed unchanged follows from this failure.
    names = [operation[0] for operation in client.transaction.operations]
    assert names.index("xadd") < names.index("hset") < names.index("execute")


def test_fault_scan_is_atomic_on_row_failure():
    client = RecordingRedisClient()
    database = SonicStateDB(client)

    assert list(database.keys("FAULT_INFO|*")) == [
        "FAULT_INFO|PSU0|SYMPTOM"
    ]
    assert client.scan_pattern == "FAULT_INFO|*"


    class PartialReadDatabase(FakeStateDB):
        def keys(self, pattern):
            # A later row failure must abort the whole snapshot even after a
            # valid row was decoded.
            return [b"FAULT_INFO|GOOD", b"FAULT_INFO|BAD"]

        def hgetall(self, key):
            if key == "FAULT_INFO|BAD":
                raise RuntimeError("row failed")
            return {
                b"status": b"ACTIVE",
                b"events": b"not-json",
                b"local_action_state": b'{"state":"IDLE"}',
            }

    with pytest.raises(RuntimeError, match="row failed"):
        list(TelemetryPublisher(PartialReadDatabase(), DLDDConfig()).read_faults())
