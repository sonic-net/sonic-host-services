from __future__ import absolute_import

import json
from queue import Queue

from dldd.config import DLDDConfig
from dldd.correlation import CorrelationEngine
from dldd.orchestrator import PrimaryOrchestrator
from dldd.planner import build_plans
from dldd.runtime import FaultRecord
from dldd.sonic_hash import SonicHashReader
from dldd.telemetry import SonicStateDB, TelemetryPublisher
from dldd.validation import load_rules
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


def test_status_uses_120_second_atomic_ttl_contract():
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
    assert database.values[publisher.STATUS_KEY]["redis_monitor_polling_interval"] == "60"
    assert database.values[publisher.STATUS_KEY]["rules_inbox_settle_time"] == "30"
    assert database.values[publisher.STATUS_KEY]["active_rules_source"] == "inbox"
    assert database.values[publisher.STATUS_KEY]["activation_result"] == "DEGRADED"
    assert database.values[publisher.STATUS_KEY]["activation_fallback_used"] == "true"
    assert database.values[publisher.STATUS_KEY]["previous_active_rules_checksum"] == "sha256:old"


def test_rule_status_uses_generation_bound_120_second_snapshot():
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


def test_rule_status_replaces_index_and_removes_stale_rule_keys():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
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


def test_rule_named_active_cannot_collide_with_rule_index():
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


def test_active_fault_is_persistent_and_nested_fields_are_json():
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
    assert json.loads(database.values[record.redis_key]["repair_actions"])[0]["action"] == "ACTION_RESEAT"


def test_vendor_component_and_remote_action_identities_are_preserved():
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


def test_fault_serial_can_be_supplied_by_platform_metadata_hook():
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


def test_inactive_fault_gets_retention_ttl():
    database = FakeStateDB()
    config = DLDDConfig(inactive_fault_retention_period=42)
    publisher = TelemetryPublisher(database, config)
    record = fault("INACTIVE")
    publisher.publish_fault(record)
    assert database.ttls[record.redis_key] == 42


def test_complete_fault_row_replacement_removes_stale_optional_fields():
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


def test_nested_byte_evidence_is_json_safe():
    database = FakeStateDB()
    publisher = TelemetryPublisher(database, DLDDConfig())
    record = fault()
    record.events = ({"id": 1, "value_read": b"\x00\xff"},)
    assert publisher.publish_fault(record)
    assert json.loads(database.values[record.redis_key]["events"])[0][
        "value_read"
    ] == [0, 255]


def test_production_hash_replacement_never_deletes_whole_fault_key():
    client = RecordingRedisClient((b"status", b"healthz_artifact"))
    database = SonicStateDB(client)
    database.replace_hash("FAULT_INFO|PSU0|SYMPTOM", {"status": "ACTIVE"}, None)

    names = [operation[0] for operation in client.transaction.operations]
    assert names == ["hset", "hdel", "persist", "execute"]
    assert "delete" not in names


def test_sonic_hash_reader_connects_lazily_once_per_database():
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


def test_state_db_can_share_the_read_only_hash_reader():
    connector = RecordingSonicConnector()
    database = SonicStateDB(
        redis_client=RecordingRedisClient(),
        hash_reader=SonicHashReader(connector=connector),
    )

    assert database.hgetall("FAULT_INFO|PSU0|SYMPTOM") == {
        "status": "ACTIVE"
    }
    assert connector.connections == [("STATE_DB", False)]


def test_production_status_write_sets_ttl_in_one_transaction():
    client = RecordingRedisClient()
    database = SonicStateDB(client)

    database.hset_with_ttl("DLDD_STATUS|process_state", {"state": "OK"}, 120)

    assert client.transaction.operations == [
        ("hset", "DLDD_STATUS|process_state", {"state": "OK"}),
        ("expire", "DLDD_STATUS|process_state", 120),
        ("execute",),
    ]


def test_production_bulk_delete_uses_one_redis_operation():
    client = RecordingRedisClient()
    database = SonicStateDB(client)

    database.delete_many(("DLDD_STATUS|process_state", "DLDD_RULE_STATUS|active"))

    assert client.deleted == [
        ("DLDD_STATUS|process_state", "DLDD_RULE_STATUS|active")
    ]


def test_production_inactive_fault_replacement_sets_retention_ttl():
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


def test_production_fault_scan_uses_nonblocking_iterator():
    client = RecordingRedisClient()
    database = SonicStateDB(client)

    assert list(database.keys("FAULT_INFO|*")) == [
        b"FAULT_INFO|PSU0|SYMPTOM"
    ]
    assert client.scan_pattern == "FAULT_INFO|*"


def test_fault_key_escapes_redis_separator_reversibly():
    record = fault()
    record.component_name = "PSU|0"
    assert record.redis_key == "FAULT_INFO|PSU%7C0|SYMPTOM_OVER_THRESHOLD"


def test_startup_reconciliation_schedules_current_fault_recheck():
    database = FakeStateDB()
    config = DLDDConfig()
    publisher = TelemetryPublisher(database, config)
    record = fault()
    record.component_name = "PSU"
    publisher.publish_fault(record)
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        publisher,
        config,
        "sha256:test",
    )
    orchestrator.reconcile_existing_faults()
    assert (1000001, "PSU") in orchestrator.reconciliation
    command = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert command.command.value == "RECHECK_ONCE"


def test_reconciliation_timeout_restores_active_fault_to_arbiter():
    database = FakeStateDB()
    config = DLDDConfig(fault_evidence_ack_timeout=1)
    publisher = TelemetryPublisher(database, config)
    record = fault()
    record.component_name = "PSU"
    publisher.publish_fault(record)
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    clock = [0.0]
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        publisher,
        config,
        "sha256:test",
        clock=lambda: clock[0],
    )
    orchestrator.reconcile_existing_faults()

    clock[0] = 1.0
    orchestrator.tick()
    clock[0] = 2.0
    orchestrator.tick()

    assert (1000001, "PSU") not in orchestrator.reconciliation
    assert any(key[2] == 1000001 for key in orchestrator.arbiter._active)
    assert orchestrator.faults[(1000001, "PSU")].stale_source is True


def test_startup_loads_retained_inactive_occurrence_history_without_recheck():
    database = FakeStateDB()
    config = DLDDConfig()
    publisher = TelemetryPublisher(database, config)
    record = fault("INACTIVE")
    record.component_name = "PSU"
    record.occurrences = 4
    publisher.publish_fault(record)
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        publisher,
        config,
        "sha256:test",
    )

    orchestrator.reconcile_existing_faults()

    loaded = orchestrator.faults[(1000001, "PSU")]
    assert loaded.status == "INACTIVE"
    assert loaded.occurrences == 4
    assert bundle.monitor_plans["redis"].control_queue.empty()


def test_primary_owned_config_update_refreshes_inactive_fault_ttl():
    database = FakeStateDB()
    initial = DLDDConfig(inactive_fault_retention_period=3600)
    publisher = TelemetryPublisher(database, initial)
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        publisher,
        initial,
        "sha256:test",
    )
    record = fault("INACTIVE")
    record.component_name = "PSU"
    identity = (record.rule_id, record.component_name)
    orchestrator.faults[identity] = record
    orchestrator.published_by_key[(record.component_name, record.symptom)] = record.rule_id
    publisher.publish_fault(record)

    updated = DLDDConfig(inactive_fault_retention_period=42)
    publisher.config = updated
    orchestrator.queue_config_update(updated)
    orchestrator.tick()

    assert orchestrator.config is updated
    assert database.ttls[record.redis_key] == 42
