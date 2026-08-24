from __future__ import absolute_import

import builtins
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from dldd.config import DLDDConfig
from dldd.runtime import FaultRecord
from dldd.sonic_hash import SonicHashReader
from dldd.telemetry import (
    SonicStateDB,
    StateDB,
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


class RecordingSonicConnector(object):
    def __init__(self):
        self.connections = []
        self.reads = []

    def connect(self, database, wait_for_init):
        self.connections.append((database, wait_for_init))

    def get_all(self, database, key):
        self.reads.append((database, key))
        return {"status": "ACTIVE"}


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


def test_state_db_interface_contract():
    for operation in (
        lambda database: database.hset("KEY", {}),
        lambda database: database.expire("KEY", 1),
        lambda database: database.persist("KEY"),
        lambda database: database.hdel("KEY", ("field",)),
        lambda database: database.delete("KEY"),
        lambda database: database.hgetall("KEY"),
        lambda database: database.keys("KEY*"),
    ):
        with pytest.raises(NotImplementedError):
            operation(StateDB())

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
    assert database.ttls[publisher.STATUS_KEY] == 120
    assert json.loads(database.values[publisher.STATUS_KEY]["broken_rules"]) == []
    assert (
        database.values[publisher.STATUS_KEY][
            "redis_monitor_polling_interval"
        ]
        == "60"
    )
    assert database.values[publisher.STATUS_KEY]["rules_inbox_settle_time"] == "30"
    assert database.values[publisher.STATUS_KEY]["active_rules_source"] == "inbox"
    assert database.values[publisher.STATUS_KEY]["activation_result"] == "DEGRADED"
    assert database.values[publisher.STATUS_KEY]["activation_fallback_used"] == "true"
    assert (
        database.values[publisher.STATUS_KEY][
            "previous_active_rules_checksum"
        ]
        == "sha256:old"
    )
    assert database.values[publisher.STATUS_KEY]["async_pool_workers"] == "0"
    assert database.values[publisher.STATUS_KEY]["async_pool_busy"] == "0"
    assert database.values[publisher.STATUS_KEY]["async_pool_queued"] == "0"
    assert (
        database.values[publisher.STATUS_KEY][
            "async_pool_avg_queue_latency_ms"
        ]
        == "0.0"
    )
    assert (
        database.values[publisher.STATUS_KEY][
            "async_pool_avg_execution_time_ms"
        ]
        == "0.0"
    )
    assert (
        database.values[publisher.STATUS_KEY][
            "async_pool_avg_utilization_percent"
        ]
        == "0.0"
    )

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
    assert database.values[publisher.STATUS_KEY]["async_pool_workers"] == "8"
    assert database.values[publisher.STATUS_KEY]["async_pool_busy"] == "3"
    assert database.values[publisher.STATUS_KEY]["async_pool_queued"] == "4"
    assert (
        database.values[publisher.STATUS_KEY][
            "async_pool_avg_queue_latency_ms"
        ]
        == "1.25"
    )
    assert (
        database.values[publisher.STATUS_KEY][
            "async_pool_avg_execution_time_ms"
        ]
        == "4.5"
    )
    assert (
        database.values[publisher.STATUS_KEY][
            "async_pool_avg_utilization_percent"
        ]
        == "12.5"
    )

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    database.fail_writes_with(RuntimeError("STATE_DB unavailable"))

    assert not publisher.publish_status("OK", "schema", "/active", "checksum")
    assert not publisher.publish_rule_status("checksum", ())
    assert not publisher.clear_rule_status()
    assert not publisher.publish_fault(fault())
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

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    assert publisher.publish_rule_status(
        "sha256:test",
        ({"rule_id": 1, "rule": "active", "work_items": []},),
    )
    assert publisher.RULE_STATUS_KEY in database.values
    assert "DLDD_RULE_STATUS|rule|active" in database.values
    assert json.loads(
        database.values[publisher.RULE_STATUS_KEY]["rule_keys"]
    ) == ["DLDD_RULE_STATUS|rule|active"]


def test_publications_floor_timestamps_and_preserve_duration_precision():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    publisher.publish_status(
        "DEGRADED",
        "0.0.1",
        "/active",
        "sha256:test",
        broken_rules=({"last_attempt": 100.9},),
        source_status=(
            {
                "since": 101.8,
                "grace_deadline": 102.7,
                "last_success": 99.6,
            },
        ),
        inflight_fault_evidence=(
            {
                "hold_deadline": 103.6,
                "local_action_state": {
                    "started_at": 104.5,
                    "wait_until": 105.4,
                },
            },
        ),
        service_diagnostics=({"observed_at": 106.3},),
    )
    status = database.values[publisher.STATUS_KEY]
    assert json.loads(status["broken_rules"])[0]["last_attempt"] == 100
    assert json.loads(status["source_status"])[0] == {
        "since": 101,
        "grace_deadline": 102,
        "last_success": 99,
    }
    inflight = json.loads(status["inflight_fault_evidence"])[0]
    assert inflight["hold_deadline"] == 103
    assert inflight["local_action_state"] == {
        "started_at": 104,
        "wait_until": 105,
    }
    assert json.loads(status["service_diagnostics"])[0]["observed_at"] == 106

    publisher.publish_rule_status(
        "sha256:test",
        (
            {
                "rule_id": 1000001,
                "rule": "TIMESTAMP_RULE",
                "last_attempt": 107.2,
                "last_success": 108.1,
                "work_items": [
                    {
                        "next_due": 109.9,
                        "sampling_interval": 60.25,
                    }
                ],
            },
        ),
    )
    summary = database.values["DLDD_RULE_STATUS|rule|TIMESTAMP_RULE"]
    detail = json.loads(
        database.values["DLDD_RULE_DETAIL|rule|TIMESTAMP_RULE"]["work_items"]
    )
    assert summary["last_attempt"] == "107"
    assert summary["last_success"] == "108"
    assert detail[0] == {
        "next_due": 109,
        "sampling_interval": 60.25,
    }

    record = fault()
    record.origin_time = 110.8
    record.last_detection_time = 111.7
    record.events = (
        {"event_timestamp": 112.6, "match_period": 5.5},
    )
    publisher.publish_fault(
        record,
        remote_action_time_window=30.5,
        local_action_details={"completed_at": 113.4},
    )
    published_fault = database.values[record.redis_key]
    assert published_fault["producer"] == "dldd"
    assert published_fault["origin_time"] == "110"
    assert published_fault["last_detection_time"] == "111"
    assert json.loads(published_fault["events"])[0] == {
        "event_timestamp": 112,
        "match_period": 5.5,
    }
    assert published_fault["remote_action_time_window"] == "30.5"
    assert json.loads(published_fault["local_action_state"])["completed_at"] == 113


def test_fault_payload_identity_json_replacement_and_inactive_ttl_contract(caplog):
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    publisher.publish_fault(record, serial_number="serial")
    assert record.redis_key not in database.ttls
    payload = database.values[record.redis_key]
    assert payload["component_type"] == "PSU"
    assert payload["component_name"] == "PSU0"
    assert payload["component_serial_number"] == "serial"
    assert "component_info" not in payload
    assert json.loads(
        database.values[record.redis_key]["repair_actions"]
    )[0]["action"] == "ACTION_RESEAT"

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    record.component_type = "VENDOR_FABRIC_MODULE"
    record.repair_actions = (
        "vendor-healthz:ACTION_REPAIR_FABRIC_MODULE",
    )

    publisher.publish_fault(record)

    payload = database.values[record.redis_key]
    actions = json.loads(payload["repair_actions"])
    assert payload["component_type"] == "VENDOR_FABRIC_MODULE"
    assert actions == [
        {"action": "vendor-healthz:ACTION_REPAIR_FABRIC_MODULE"}
    ]

    database = FakeStateDB()
    publisher = TelemetryPublisher(
        database,
        DLDDConfig(),
        serial_resolver=lambda component_type, component_name: "SERIAL-1",
    )
    record = fault()
    publisher.publish_fault(record)
    payload = database.values[record.redis_key]
    assert payload["component_serial_number"] == "SERIAL-1"
    assert record.serial_number == "SERIAL-1"

    database = FakeStateDB()

    def fail_serial(component_type, component_name):
        raise RuntimeError("inventory is unavailable")

    publisher = TelemetryPublisher(
        database, DLDDConfig(), serial_resolver=fail_serial
    )
    record = fault()

    assert publisher.publish_fault(record)
    assert database.values[record.redis_key]["component_serial_number"] == ""
    assert "inventory is unavailable" in caplog.text

    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    record.events = ({"id": 1, "value_read": b"\x00\xff"},)
    assert publisher.publish_fault(record)
    assert json.loads(database.values[record.redis_key]["events"])[0][
        "value_read"
    ] == [0, 255]


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


def test_production_state_db_hash_replacement_and_transaction_contract():
    client = RecordingRedisClient((b"status", b"healthz_artifact"))
    database = SonicStateDB(client)
    database.replace_hash("FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None)

    names = [operation[0] for operation in client.transaction.operations]
    assert names == ["hset", "hdel", "persist", "execute"]
    assert "delete" not in names

    client = RecordingRedisClient((b"status",))
    database = SonicStateDB(client)

    database.replace_hash("KEY", {"status": "ACTIVE"}, None)

    assert client.transaction.operations == [
        ("hset", "KEY", {"status": "ACTIVE"}),
        ("persist", "KEY"),
        ("execute",),
    ]

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


    # Direct operations and pipelines share the same Redis boundary.
    client = RecordingRedisClient()
    database = SonicStateDB(client)

    database.hset("KEY", {"enabled": True, "items": [1, 2]})
    database.expire("KEY", 10)
    database.persist("KEY")
    database.hdel("KEY", ())
    database.hdel("KEY", ("old",))
    assert database.hgetall("KEY") == {"status": "ACTIVE"}
    database.delete("KEY")
    database.delete_many(())

    assert client.direct == [
        ("hset", "KEY", {"enabled": "true", "items": "[1,2]"}),
        ("expire", "KEY", 10),
        ("persist", "KEY"),
        ("hdel", "KEY", ("old",)),
        ("hgetall", "KEY"),
    ]
    assert client.deleted == [("KEY",)]

    client = RecordingRedisClient()
    database = SonicStateDB(client)
    database.hset_with_ttl("DLDD_STATUS|process_state", {"state": "OK"}, 120)
    assert client.transaction.operations == [
        ("hset", "DLDD_STATUS|process_state", {"state": "OK"}),
        ("expire", "DLDD_STATUS|process_state", 120),
        ("execute",),
    ]

    database.delete_many(
        ("DLDD_STATUS|process_state", "DLDD_RULE_STATUS|active")
    )
    assert client.deleted == [
        ("DLDD_STATUS|process_state", "DLDD_RULE_STATUS|active")
    ]


def test_production_state_db_endpoint_dependency_and_hash_reader_contract(
    monkeypatch,
):
    for socket_path, expected in (
        ("/var/run/redis.sock", {"unix_socket_path": "/var/run/redis.sock", "db": 6}),
        ("", {"host": "127.0.0.1", "port": 6379, "db": 6}),
    ):
        clients = []

        class RedisFactory(object):
            def __new__(cls, **kwargs):
                clients.append(kwargs)
                return RecordingRedisClient()

        redis_module = ModuleType("redis")
        redis_module.Redis = RedisFactory
        swss = SimpleNamespace(
            SonicDBKey=type("SonicDBKey", (), {}),
            SonicDBConfig=SimpleNamespace(
                getDbId=lambda database, key: 6,
                getDbSock=lambda database, key: socket_path,
                getDbHostname=lambda database, key: "127.0.0.1",
                getDbPort=lambda database, key: 6379,
            ),
        )
        swss_package = ModuleType("swsscommon")
        swss_package.swsscommon = swss
        monkeypatch.setitem(sys.modules, "redis", redis_module)
        monkeypatch.setitem(sys.modules, "swsscommon", swss_package)

        database = SonicStateDB()
        database.hset("KEY", {"status": "ACTIVE"})

        assert clients == [expected]

    original_import = builtins.__import__

    def missing_dependencies(name, *args, **kwargs):
        if name in ("redis", "swsscommon"):
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_dependencies)
    with pytest.raises(RuntimeError, match="STATE_DB dependencies are unavailable"):
        SonicStateDB()._db()


    # Hash reads connect lazily once per database and may be shared by StateDB.
    connector = RecordingSonicConnector()
    reader = SonicHashReader(connector=connector)

    assert connector.connections == []
    assert reader.read("STATE_DB", "FAULT_INFO|A") == {"status": "ACTIVE"}
    reader.read("STATE_DB", "FAULT_INFO|B")
    reader.read("APPL_DB", "TABLE|KEY")

    assert connector.connections == [
        ("STATE_DB", False),
        ("APPL_DB", False),
    ]
    assert connector.reads == [
        ("STATE_DB", "FAULT_INFO|A"),
        ("STATE_DB", "FAULT_INFO|B"),
        ("APPL_DB", "TABLE|KEY"),
    ]

    connector = RecordingSonicConnector()
    database = SonicStateDB(
        redis_client=RecordingRedisClient(),
        hash_reader=SonicHashReader(connector=connector),
    )

    assert database.hgetall("FAULT_INFO|PSU0|SYMPTOM") == {
        "status": "ACTIVE"
    }
    assert connector.connections == [("STATE_DB", False)]


def test_fault_scan_iteration_failure_and_malformed_payload_contract():
    client = RecordingRedisClient()
    database = SonicStateDB(client)

    assert list(database.keys("FAULT_INFO|*")) == [
        "FAULT_INFO|PSU0|SYMPTOM"
    ]
    assert client.scan_pattern == "FAULT_INFO|*"


    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    database.fail_reads_with(RuntimeError("scan failed"))
    with pytest.raises(RuntimeError, match="scan failed"):
        list(publisher.read_faults())

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
        list(
            TelemetryPublisher(
                PartialReadDatabase(), DLDDConfig()
            ).read_faults()
        )


    class MalformedPayloadDatabase(FakeStateDB):
        def keys(self, pattern):
            assert pattern == "FAULT_INFO|*"
            return [b"FAULT_INFO|SENSOR0|SYMPTOM_UNKNOWN"]

        def hgetall(self, key):
            assert key == "FAULT_INFO|SENSOR0|SYMPTOM_UNKNOWN"
            return {
                b"producer": b"dldd",
                b"status": b"ACTIVE",
                b"events": b"not-json",
                b"local_action_state": b'{"state":"IDLE"}',
            }

    rows = list(
        TelemetryPublisher(
            MalformedPayloadDatabase(), DLDDConfig()
        ).read_faults()
    )

    assert rows == [
        {
            "producer": "dldd",
            "status": "ACTIVE",
            "events": "not-json",
            "local_action_state": {"state": "IDLE"},
            "redis_key": "FAULT_INFO|SENSOR0|SYMPTOM_UNKNOWN",
        }
    ]


    database = FakeStateDB()
    database.hset(
        "FAULT_INFO|BAD|SYMPTOM_UNKNOWN",
        {
            "status": "ACTIVE",
            "events": "not-json",
            "local_action_state": '{"state":"IDLE"}',
        },
    )

    rows = list(TelemetryPublisher(database, DLDDConfig()).read_faults())

    assert rows == [
        {
            "status": "ACTIVE",
            "events": "not-json",
            "local_action_state": {"state": "IDLE"},
            "redis_key": "FAULT_INFO|BAD|SYMPTOM_UNKNOWN",
        }
    ]


    record = fault()
    record.component_name = "PSU|0"
    assert record.redis_key == "FAULT_INFO|PSU%7C0|SYMPTOM_OVER_THRESHOLD"
