from __future__ import absolute_import

import fnmatch
import json
from copy import deepcopy
from concurrent.futures import Future
from queue import Queue
from types import SimpleNamespace

from dldd.actions import ActionResult, ActionSequenceResult
from dldd.artifacts import ArtifactRequest
from dldd.config import DLDDConfig
from dldd.correlation import CorrelationEngine, SignatureExecution
from dldd.logic import parse_logic
from dldd.models import Operation
from dldd.orchestrator import PrimaryOrchestrator
from dldd.planner import build_plans
from dldd.runtime import (
    CollectedValue,
    EvaluationResult,
    EvaluationResultType,
    FaultEvidenceEvent,
    ValueConfig,
)
from dldd.telemetry import StateDB, TelemetryPublisher
from dldd.validation import load_rules


class FakeStateDB(StateDB):
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def hset(self, key, values):
        self.values[key] = {
            name: json.dumps(value) if isinstance(value, (dict, list, tuple)) else str(value)
            for name, value in values.items()
        }

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def persist(self, key):
        self.ttls.pop(key, None)

    def hdel(self, key, fields):
        for field in fields:
            self.values.get(key, {}).pop(field, None)

    def delete(self, key):
        self.values.pop(key, None)

    def hgetall(self, key):
        return self.values.get(key, {})

    def keys(self, pattern):
        return [key for key in self.values if fnmatch.fnmatch(key, pattern)]


class CompletedActionRunner(object):
    def submit(self, rule_name, actions, default_timeout):
        future = Future()
        future.dldd_worker_id = "worker-1"
        future.set_result(
            ActionSequenceResult(
                "worker-1",
                "COMPLETED",
                100.0,
                101.0,
                (
                    ActionResult(
                        "cli", "SUCCESS", 100.0, 101.0, command=["reset"]
                    ),
                ),
            )
        )
        return future


class FakeArtifactClient(object):
    def request(self, metadata, logs, queries):
        return ArtifactRequest("dldd-test.tar.gz", "REQUESTED", 101.0)

    def status(self, artifact_id):
        return ArtifactRequest(artifact_id, "COMPLETED", 101.0, 102.0)


class RecordingArtifactClient(FakeArtifactClient):
    def __init__(self):
        self.metadata = None

    def request(self, metadata, logs, queries):
        self.metadata = metadata
        return super(RecordingArtifactClient, self).request(metadata, logs, queries)


class NeverCompletingActionRunner(object):
    def submit(self, rule_name, actions, default_timeout):
        future = Future()
        future.dldd_worker_id = "stuck-worker"
        return future


def test_action_payload_vendor_data_cannot_override_typed_dispatch_fields():
    def executor(operation):
        return operation

    operation = Operation(
        type="dse",
        command="PSU:reset()",
        timeout=10,
        executor=executor,
        options={
            "type": "cli",
            "command": "unsafe",
            "timeout": 999,
            "executor": None,
            "token": "vendor-data",
        },
    )

    payload = PrimaryOrchestrator._operation_payload(operation)

    assert payload["type"] == "dse"
    assert payload["command"] == "PSU:reset()"
    assert payload["timeout"] == 10
    assert payload["executor"] is executor
    assert payload["token"] == "vendor-data"


def test_query_payload_drops_reserved_vendor_data_without_canonical_values():
    operation = Operation(
        type="vendor_dump",
        options={
            "argv": ["/bin/false"],
            "path": {"unsafe": True},
            "max_output_bytes": 999,
            "hook": "diagnostics",
        },
    )

    payload = PrimaryOrchestrator._operation_payload(operation)

    assert payload == {"type": "vendor_dump", "hook": "diagnostics"}


def evidence(item, kind, sequence, from_recheck=False):
    value = CollectedValue(51.5, 51.5, item.value_config)
    return FaultEvidenceEvent(
        signature_id=item.rule_id,
        event_id=item.event_id,
        component_name=item.component_name,
        source_id=item.source_id,
        correlation_key=item.correlation_key,
        monitor_id="redis",
        plan_generation="sha256:test",
        work_state_generation=sequence,
        sequence=sequence,
        event_timestamp=100.0 + sequence,
        enqueue_timestamp=100.0 + sequence,
        result=EvaluationResult(
            kind,
            value=value,
            evaluator_type="comparison",
            operator=">",
            expected=50.0,
            completed_at=100.0 + sequence,
        ),
        from_recheck=from_recheck,
    )


def test_zero_logic_lookback_correlates_currently_active_events():
    """An older event remains true while its source still reports a fault."""

    conditions = SimpleNamespace(
        events=(
            SimpleNamespace(id=1, match_count=1, match_period=0),
            SimpleNamespace(id=2, match_count=1, match_period=0),
        ),
        logic_tree=parse_logic("1 AND 2"),
        logic_lookback_time=0,
    )
    execution = SignatureExecution(
        SimpleNamespace(conditions=conditions),
        "PSU",
        {1: ("event-a",), 2: ("event-b",)},
        "sha256:test",
    )
    correlation = CorrelationEngine({(1000001, "PSU"): execution})
    common = {
        "rule_id": 1000001,
        "component_name": "PSU",
        "source_id": "STATE_DB:PSU_INFO",
        "value_config": ValueConfig(),
    }
    event_b = SimpleNamespace(
        **common, event_id=2, correlation_key="event-b"
    )
    event_a = SimpleNamespace(
        **common, event_id=1, correlation_key="event-a"
    )

    first = correlation.consume(
        evidence(event_b, EvaluationResultType.MATCH, 0)
    )
    combined = correlation.consume(
        evidence(event_a, EvaluationResultType.MATCH, 300)
    )
    cleared = correlation.consume(
        evidence(event_b, EvaluationResultType.NO_MATCH, 301)
    )

    assert not first.active
    assert combined.active
    assert combined.changed
    assert not cleared.active
    assert cleared.changed


def test_local_action_holds_rechecks_and_publishes_recovered_inactive_fault():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    clock = [0.0]
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        action_runner=CompletedActionRunner(),
        artifact_client=FakeArtifactClient(),
        local_action_default_timeout=300,
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )

    orchestrator.process_event(evidence(item, EvaluationResultType.MATCH, 1))
    assert (item.rule_id, item.component_name) in orchestrator.pending
    assert orchestrator.faults[(item.rule_id, item.component_name)].status == "CANDIDATE"
    assert "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD" not in database.values
    hold = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert hold.command.value == "HOLD"

    orchestrator.tick()
    clock[0] = 61.0
    orchestrator.tick()
    recheck = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert recheck.command.value == "RECHECK_ONCE"

    orchestrator.process_event(
        evidence(item, EvaluationResultType.NO_MATCH, 2, from_recheck=True)
    )
    fault_key = "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"
    assert database.values[fault_key]["status"] == "INACTIVE"
    assert json.loads(database.values[fault_key]["repair_actions"]) == []
    action_state = json.loads(database.values[fault_key]["local_action_state"])
    assert action_state["state"] == "COMPLETED"
    assert action_state["worker_id"] == "worker-1"
    assert action_state["started_at"] == 100.0
    assert action_state["completed_at"] == 101.0
    assert database.values[fault_key]["origin_time"] == "101"
    assert database.values[fault_key]["last_detection_time"] == "102"
    assert json.loads(database.values[fault_key]["events"])[0]["value_read"] == 51.5
    assert json.loads(database.values[fault_key]["healthz_artifact"])["state"] == "REQUESTED"


def test_artifact_metadata_has_timestamp_and_full_component_info():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    artifact_client = RecordingArtifactClient()
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(FakeStateDB(), DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        artifact_client=artifact_client,
        wall_clock=lambda: 1234.5,
    )
    execution = next(iter(bundle.signatures.values()))

    request = orchestrator._request_artifact(execution)

    assert request["state"] == "REQUESTED"
    assert artifact_client.metadata["timestamp"] == 1234
    assert artifact_client.metadata["component_info"] == {
        "component": "PSU",
        "name": execution.component_name,
    }


def test_normal_evidence_is_quarantined_while_local_action_owns_signature():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        action_runner=CompletedActionRunner(),
        local_action_default_timeout=300,
        clock=lambda: 0.0,
    )

    orchestrator.process_event(evidence(item, EvaluationResultType.MATCH, 1))
    bundle.monitor_plans["redis"].control_queue.get_nowait()
    orchestrator.process_event(evidence(item, EvaluationResultType.NO_MATCH, 2))

    command = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert command.command.value == "HOLD"
    assert command.reason == "local_action_evidence_quarantined"
    assert (item.rule_id, item.component_name) in orchestrator.pending
    assert not database.values


def test_action_gated_high_severity_candidate_blocks_lower_fault_owner():
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    lower = deepcopy(document["signatures"][0])
    lower_metadata = lower["signature"]["metadata"]
    lower_metadata["id"] += 1
    lower_metadata["name"] = "LOWER_PRIORITY_PSU_FAULT"
    lower_metadata["severity"] = "MINOR"
    lower["signature"]["actions"]["repair_actions"].pop("local_actions", None)
    document["signatures"].append(lower)
    rules = load_rules(json.dumps(document))
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    items = {item.rule_id: item for item in bundle.work_items.values()}
    high = items[1000001]
    low = items[1000002]
    database = FakeStateDB()
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        action_runner=CompletedActionRunner(),
        local_action_default_timeout=300,
    )

    orchestrator.process_event(evidence(high, EvaluationResultType.MATCH, 1))
    orchestrator.process_event(evidence(low, EvaluationResultType.MATCH, 1))

    assert (high.rule_id, high.component_name) in orchestrator.pending
    assert not database.values


def test_missing_post_action_recheck_retries_then_publishes_conservatively():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    clock = [0.0]
    config = DLDDConfig(fault_evidence_ack_timeout=5)
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, config),
        config,
        "sha256:test",
        action_runner=CompletedActionRunner(),
        local_action_default_timeout=300,
        clock=lambda: clock[0],
    )

    orchestrator.process_event(evidence(item, EvaluationResultType.MATCH, 1))
    orchestrator.tick()
    clock[0] = 61.0
    orchestrator.tick()
    clock[0] = 66.0
    orchestrator.tick()
    clock[0] = 71.0
    orchestrator.tick()

    assert (item.rule_id, item.component_name) not in orchestrator.pending
    key = "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"
    assert database.values[key]["status"] == "ACTIVE"
    assert any(
        item["reason"] == "post_action_recheck_timed_out"
        for item in orchestrator.service_diagnostics
    )


def test_late_event_outside_window_is_discarded_and_counted():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    correlation = CorrelationEngine(bundle.signatures)

    assert correlation.consume(evidence(item, EvaluationResultType.MATCH, 2)).active
    decision = correlation.consume(evidence(item, EvaluationResultType.NO_MATCH, -300))

    assert decision.active
    assert not decision.changed
    assert correlation.late_events_discarded == 1
    assert correlation.diagnostics[-1]["reason"] == "late_event_discarded"


def test_suppressed_rule_async_update_cannot_overwrite_fault_owner():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        action_runner=CompletedActionRunner(),
    )
    orchestrator.process_event(evidence(item, EvaluationResultType.MATCH, 1))
    identity = (item.rule_id, item.component_name)
    record = orchestrator.faults[identity]
    record.status = "ACTIVE"
    orchestrator.published_by_key[(record.component_name, record.symptom)] = 9999999

    assert orchestrator._publish_fault_record(identity, record, 0)
    assert not database.values


def test_vendor_lifecycle_hook_suspends_and_resumes_expected_source_outage():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    maintenance = [True]
    clock = [0.0]
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(FakeStateDB(), DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        source_lifecycle_probe=lambda unused: maintenance[0],
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )

    orchestrator.process_event(
        evidence(item, EvaluationResultType.SOURCE_UNAVAILABLE, 1)
    )
    suspended = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert suspended.command.value == "SUSPEND"
    assert orchestrator.source_status[item.source_id]["state"] == "SUSPENDED"
    assert orchestrator.source_status[item.source_id]["graceful"] is True
    assert orchestrator.service_state() == "DEGRADED"

    maintenance[0] = False
    clock[0] = 5.0
    orchestrator.tick()
    resumed = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert resumed.command.value == "RESUME"
    assert orchestrator.source_status[item.source_id]["state"] == "UNAVAILABLE"


def test_republishing_same_active_state_does_not_change_last_detection_time():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    correlation = CorrelationEngine(bundle.signatures)
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        correlation,
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )
    first = correlation.consume(evidence(item, EvaluationResultType.MATCH, 1))
    orchestrator._publish_decision(first)
    repeated = correlation.consume(evidence(item, EvaluationResultType.MATCH, 2))

    orchestrator._publish_decision(repeated)

    record = orchestrator.faults[(item.rule_id, item.component_name)]
    assert record.last_detection_time == 101.0


def test_missing_action_runner_still_waits_for_mandatory_recheck():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    clock = [0.0]
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        action_runner=None,
        local_action_default_timeout=300,
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )

    orchestrator.process_event(evidence(item, EvaluationResultType.MATCH, 1))
    assert (item.rule_id, item.component_name) in orchestrator.pending
    assert not database.values
    bundle.monitor_plans["redis"].control_queue.get_nowait()
    orchestrator.tick()
    clock[0] = 61.0
    orchestrator.tick()
    assert bundle.monitor_plans["redis"].control_queue.get_nowait().command.value == "RECHECK_ONCE"
    orchestrator.process_event(
        evidence(item, EvaluationResultType.NO_MATCH, 2, from_recheck=True)
    )

    fault = database.values["FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"]
    assert fault["status"] == "INACTIVE"
    assert json.loads(fault["local_action_state"])["state"] == "FAILED"


def test_stuck_action_future_advances_to_recheck_instead_of_holding_forever():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    clock = [0.0]
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(FakeStateDB(), DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        action_runner=NeverCompletingActionRunner(),
        local_action_default_timeout=300,
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )

    orchestrator.process_event(evidence(item, EvaluationResultType.MATCH, 1))
    pending = orchestrator.pending[(item.rule_id, item.component_name)]
    clock[0] = pending.action_deadline + 1
    orchestrator.tick()

    assert pending.phase == "WAITING_FOR_RECHECK"
    assert pending.action_result.state == "FAILED"
    assert any(
        item["reason"] == "local_action_deadline_expired"
        for item in orchestrator.service_diagnostics
    )


def test_primary_processing_exception_isolated_to_work_key():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    queue = Queue()
    queue.put(evidence(item, EvaluationResultType.MATCH, 1))
    correlation = CorrelationEngine(bundle.signatures)
    correlation.consume = lambda event: (_ for _ in ()).throw(RuntimeError("boom"))
    orchestrator = PrimaryOrchestrator(
        queue,
        bundle.monitor_plans,
        bundle.work_items,
        correlation,
        TelemetryPublisher(FakeStateDB(), DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )

    assert orchestrator.process_batch() == 1

    command = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert command.target_state.value == "DEGRADED"
    assert orchestrator.broken_rules[item.correlation_key]["state"] == "DEGRADED"


def test_reconciliation_ignores_foreign_and_malformed_fault_rows():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    database = FakeStateDB()
    database.hset(
        "FAULT_INFO|FOREIGN|SYMPTOM_UNKNOWN",
        {
            "status": "ACTIVE",
            "component_type": "FOREIGN",
            "component_name": "FOREIGN",
        },
    )
    database.hset(
        "FAULT_INFO|BROKEN|SYMPTOM_UNKNOWN",
        {
            "rule": "BROKEN",
            "rule_id": 1000001,
            "schema_version": "0.0.1",
            "active_rules_checksum": "sha256:test",
            "component_type": "BROKEN",
            "component_name": "",
            "status": "ACTIVE",
        },
    )
    foreign_before = dict(database.values["FAULT_INFO|FOREIGN|SYMPTOM_UNKNOWN"])
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )

    orchestrator.reconcile_existing_faults()

    assert database.values["FAULT_INFO|FOREIGN|SYMPTOM_UNKNOWN"] == foreign_before
    assert any(
        item["reason"] == "malformed_persisted_fault_skipped"
        for item in orchestrator.service_diagnostics
    )


def test_stale_fault_reconciliation_clears_actions_and_preserves_time_window():
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:new",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    key = "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"
    database.hset(
        key,
        {
            "rule": item.rule_name,
            "rule_id": item.rule_id,
            "rule_version": item.rule_version,
            "schema_version": "0.0.1",
            "active_rules_checksum": "sha256:old",
            "component_type": item.component_type,
            "component_name": item.component_name,
            "component_serial_number": "",
            "symptom": item.symptom,
            "status": "ACTIVE",
            "origin_time": 10,
            "last_detection_time": 11,
            "occurrences": 2,
            "repair_actions": [{"action": "ACTION_REPLACE"}],
            "remote_action_time_window": 77,
        },
    )
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:new",
        wall_clock=lambda: 100.0,
    )

    orchestrator.reconcile_existing_faults()

    assert database.values[key]["status"] == "INACTIVE"
    assert json.loads(database.values[key]["repair_actions"]) == []
    assert database.values[key]["remote_action_time_window"] == "77"
