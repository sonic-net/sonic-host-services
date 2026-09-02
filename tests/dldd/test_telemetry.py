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
        activation_fallback_used=True,
        previous_active_rules_checksum="sha256:old",
    )
    status = database.values[publisher.STATUS_KEY]
    assert database.ttls[publisher.STATUS_KEY] == 120
    assert status["active_rules_source"] == "inbox"
    assert status["activation_result"] == "DEGRADED"
    assert status["activation_fallback_used"] == "true"
    metric_names = (
        "async_pool_workers",
        "async_pool_busy",
        "async_pool_queued",
        "async_pool_avg_queue_latency_ms",
        "async_pool_avg_execution_time_ms",
        "async_pool_avg_utilization_percent",
    )
    assert [status[name] for name in metric_names] == [
        "0", "0", "0", "0.0", "0.0", "0.0"
    ]

    publisher.publish_status(
        "OK",
        "0.0.1",
        "/active",
        "sha256:test",
        async_pool_metrics={
            "async_pool_workers": 8,
            "async_pool_busy": 3,
            "async_pool_queued": 4,
            "async_pool_avg_queue_latency_ms": 1.25,
            "async_pool_avg_execution_time_ms": 4.5,
            "async_pool_avg_utilization_percent": 12.5,
        },
    )
    status = database.values[publisher.STATUS_KEY]
    assert [status[name] for name in metric_names] == [
        "8", "3", "4", "1.25", "4.5", "12.5"
    ]

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


def test_rule_status_snapshot_replacement_and_key_namespace_contract():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    rules = (
        {
            "rule_id": 1000001,
            "rule": "PSU_FAULT",
            "health": "OK",
            "active_faults": 0,
            "work_items": [],
        },
    )

    assert publisher.publish_rule_status(
        "sha256:test", rules, detail_truncated=True
    )

    row = database.values[publisher.RULE_STATUS_KEY]
    assert database.ttls[publisher.RULE_STATUS_KEY] == 120
    assert row["active_rules_checksum"] == "sha256:test"
    assert json.loads(row["rule_keys"]) == [
        "DLDD_RULE_STATUS|rule|PSU_FAULT"
    ]
    assert row["rule_count"] == "1"
    assert row["detail_truncated"] == "true"
    assert row["published_at"].isdigit()
    summary = database.values["DLDD_RULE_STATUS|rule|PSU_FAULT"]
    detail = database.values["DLDD_RULE_DETAIL|rule|PSU_FAULT"]
    assert summary["rule_id"] == "1000001"
    assert summary["health"] == "OK"
    assert "work_items" not in summary
    assert json.loads(detail["work_items"]) == []
    assert all(
        database.ttls[key] == 120
        for key in (
            publisher.RULE_STATUS_KEY,
            "DLDD_RULE_STATUS|rule|PSU_FAULT",
            "DLDD_RULE_DETAIL|rule|PSU_FAULT",
        )
    )

    assert publisher.clear_rule_status()
    assert publisher.RULE_STATUS_KEY not in database.values
    assert "DLDD_RULE_STATUS|rule|PSU_FAULT" not in database.values
    assert "DLDD_RULE_DETAIL|rule|PSU_FAULT" not in database.values

    database.hset(
        publisher.RULE_STATUS_KEY,
        {"rules": [{"rule": "legacy-monolith"}]},
    )
    publisher.publish_rule_status(
        "sha256:first",
        (
            {"rule_id": 1, "rule": "RULE_ONE", "work_items": []},
            {"rule_id": 2, "rule": "RULE_TWO", "work_items": []},
        ),
    )
    assert "rules" not in database.values[publisher.RULE_STATUS_KEY]
    assert "DLDD_RULE_STATUS|rule|RULE_TWO" in database.values
    assert "DLDD_RULE_DETAIL|rule|RULE_TWO" in database.values

    publisher.publish_rule_status(
        "sha256:second",
        ({"rule_id": 1, "rule": "RULE_ONE", "work_items": []},),
    )
    assert json.loads(
        database.values[publisher.RULE_STATUS_KEY]["rule_keys"]
    ) == ["DLDD_RULE_STATUS|rule|RULE_ONE"]
    assert "DLDD_RULE_STATUS|rule|RULE_TWO" not in database.values
    assert "DLDD_RULE_DETAIL|rule|RULE_TWO" not in database.values

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

    publisher.publish_rule_status(
        "sha256:test",
        (
            {
                "rule_id": 1000001,
                "rule": "TIMESTAMP_RULE",
                "last_attempt": 107.2,
                "work_items": [
                    {
                        "next_due": 109.9,
                        "sampling_interval": 60.25,
                    }
                ],
            },
        ),
    )
    detail = json.loads(
        database.values["DLDD_RULE_DETAIL|rule|TIMESTAMP_RULE"]["work_items"]
    )
    assert detail[0] == {
        "next_due": 109,
        "sampling_interval": 60.25,
    }

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
