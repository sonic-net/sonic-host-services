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
    def __init__(self):
        self.operations = []

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


class RecordingRedisClient(object):
    def __init__(self, fields=()):
        self.fields = fields
        self.transaction = RecordingPipeline()
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
