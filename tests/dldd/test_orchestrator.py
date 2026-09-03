from __future__ import absolute_import

import json
from copy import deepcopy
from concurrent.futures import Future
from dataclasses import replace
from queue import Queue
from types import SimpleNamespace

import pytest

from dldd.actions import ActionResult, ActionSequenceResult
from dldd.artifacts import ArtifactReference
from dldd.config import DLDDConfig
from dldd.correlation import CorrelationEngine, SignatureExecution
from dldd.logic import parse_logic
from dldd.orchestrator import PrimaryOrchestrator
from dldd.planner import build_plans
from dldd.runtime import (
    CollectedValue,
    DSEExpansionEvent,
    DSEWorkTemplate,
    EvaluationResult,
    EvaluationResultType,
    FaultEvidenceEvent,
    FaultRecord,
    MonitorCommandType,
    MonitorExecutionPlan,
    MonitorWorkState,
    MonitorWorkStateRecord,
    ValueConfig,
)
from dldd.telemetry import TelemetryPublisher
from dldd.validation import load_rules
from tests.dldd_fakes import FakeStateDB


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
                        "cli", "COMPLETED", 100.0, 101.0
                    ),
                ),
            )
        )
        return future


class FakeArtifactClient(object):
    def request(self, metadata, logs, queries):
        return ArtifactReference(
            "dldd-test.tar.gz", 101.0, "/var/lib/sonic/dldd/artifacts/dldd-test.tar.gz"
        )


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


def test_dynamic_expansion_registration_and_fault_reconciliation():
    """Register an expanded item before evidence and reconcile retained faults."""

    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    static_bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    base = next(iter(static_bundle.work_items.values()))
    item = replace(
        base,
        component_name="DYNAMIC0",
        source_type="dse",
        source_id="dse:dynamic0",
        dse_binding=SimpleNamespace(instance="DYNAMIC0"),
        correlation_key=(
            "1000001:1:DYNAMIC0:SYMPTOM_OVER_THRESHOLD:dse:dynamic0"
        ),
    )
    common = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {},
        {item.correlation_key: MonitorWorkStateRecord()},
        Queue(),
    )
    orchestrator = PrimaryOrchestrator(
        Queue(),
        {"common": common},
        {},
        CorrelationEngine({}),
        TelemetryPublisher(FakeStateDB(), DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )
    expansion = DSEExpansionEvent(
        monitor_id="common",
        plan_generation="sha256:test",
        template_id="template",
        signature=rules.materialized_rules[0].signature,
        added_items=(item,),
    )

    orchestrator.process_expansion(expansion)
    orchestrator.process_event(
        evidence(item, EvaluationResultType.NO_MATCH, 1)
    )

    assert orchestrator.work_items[item.correlation_key] == item
    execution = orchestrator.correlation.executions[(1000001, "DYNAMIC0")]
    assert execution.signature.schema_version == item.schema_version
    assert execution.event_keys == {1: (item.correlation_key,)}
    assert common.control_queue.get_nowait().correlation_key == item.correlation_key
    # A retained dynamic fault waits for its owning expansion before recheck.
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    static_bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    base = next(iter(static_bundle.work_items.values()))
    item = replace(
        base,
        component_name="DYNAMIC0",
        source_type="dse",
        source_id="dse:dynamic0",
        dse_binding=SimpleNamespace(instance="DYNAMIC0"),
        correlation_key=(
            "1000001:1:DYNAMIC0:SYMPTOM_OVER_THRESHOLD:dse:dynamic0"
        ),
    )
    template = DSEWorkTemplate(
        "template",
        replace(item, correlation_key="template:1000001:1"),
        rules.materialized_rules[0].signature,
        SimpleNamespace(),
    )
    common = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {},
        {},
        Queue(),
        templates_by_key={"template": template},
    )
    database = FakeStateDB()
    fault_key = "FAULT_INFO|DYNAMIC0|SYMPTOM_OVER_THRESHOLD"
    database.hset(
        fault_key,
        {
            "producer": "dldd",
            "rule": item.rule_name,
            "rule_id": item.rule_id,
            "rule_version": item.rule_version,
            "schema_version": rules.schema_version,
            "active_rules_checksum": "sha256:test",
            "component_type": item.component_type,
            "component_name": item.component_name,
            "symptom": item.symptom,
            "status": "ACTIVE",
            "origin_time": 10,
            "last_detection_time": 11,
        },
    )
    orchestrator = PrimaryOrchestrator(
        Queue(),
        {"common": common},
        {},
        CorrelationEngine({}),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )

    orchestrator.reconcile_existing_faults()

    identity = (item.rule_id, item.component_name)
    assert database.values[fault_key]["status"] == "ACTIVE"
    assert identity in orchestrator.pending_dynamic_faults
    assert identity not in orchestrator.reconciliation

    # The owning monitor installs its local child before the FIFO registration
    # event is consumed by the primary thread.
    common.add_expanded_item(item)
    orchestrator.process_expansion(
        DSEExpansionEvent(
            monitor_id="common",
            plan_generation="sha256:test",
            template_id="template",
            signature=rules.materialized_rules[0].signature,
            added_items=(item,),
        )
    )

    assert identity not in orchestrator.pending_dynamic_faults
    assert identity in orchestrator.reconciliation
    assert common.control_queue.get_nowait().correlation_key == item.correlation_key


def test_expanded_common_predicate_owner_routing_and_collision_rejection():
    """Route cloned predicates to their owner and reject duplicate ownership."""

    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    base = next(iter(bundle.work_items.values()))
    item = replace(
        base,
        component_name="DYNAMIC0",
        common_predicate=True,
        correlation_key=(
            "1000001:1:DYNAMIC0:SYMPTOM_OVER_THRESHOLD:redis:common"
        ),
    )
    common = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {},
        {},
        Queue(),
    )
    common.add_expanded_item(item)
    orchestrator = PrimaryOrchestrator(
        Queue(),
        {
            "redis": bundle.monitor_plans["redis"],
            "common": common,
        },
        bundle.work_items,
        CorrelationEngine({}),
        TelemetryPublisher(FakeStateDB(), DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )
    orchestrator.process_expansion(
        DSEExpansionEvent(
            monitor_id="common",
            plan_generation="sha256:test",
            template_id="template",
            signature=rules.materialized_rules[0].signature,
            added_items=(item,),
        )
    )

    orchestrator.process_event(
        replace(
            evidence(item, EvaluationResultType.NO_MATCH, 1),
            monitor_id="common",
            work_state_generation=0,
        )
    )

    command = common.control_queue.get_nowait()
    assert command.correlation_key == item.correlation_key
    assert command.monitor_id == "common"
    assert command.command == MonitorCommandType.RESUME
    assert command.target_state == MonitorWorkState.READY
    assert bundle.monitor_plans["redis"].control_queue.empty()

    # The monitor installs an expanded child before the primary consumes its
    # expansion event.  Reject a child whose key is already owned by another
    # monitor without overwriting that original owner or work item.
    collision = replace(item, correlation_key=base.correlation_key)
    common.add_expanded_item(collision)
    with pytest.raises(ValueError, match="assigned to multiple monitors"):
        orchestrator.process_expansion(
            DSEExpansionEvent(
                monitor_id="common",
                plan_generation="sha256:test",
                template_id="collision",
                signature=rules.materialized_rules[0].signature,
                added_items=(collision,),
            )
        )

    assert (
        orchestrator._plan_by_work_key[base.correlation_key]
        is bundle.monitor_plans["redis"]
    )
    assert orchestrator.work_items[base.correlation_key] is base


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


def dse_retirement_fixture(
    template_ids=("template",), runtime_item=True, record_status="ACTIVE"
):
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    static_bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    base = next(iter(static_bundle.work_items.values()))
    item = replace(
        base,
        component_name="DYNAMIC0",
        source_type="dse",
        source_id="dse:dynamic0",
        dse_binding=SimpleNamespace(instance="DYNAMIC0"),
        correlation_key=(
            "1000001:1:DYNAMIC0:SYMPTOM_OVER_THRESHOLD:dse:dynamic0"
        ),
    )
    signature = rules.materialized_rules[0].signature
    templates = {
        template_id: DSEWorkTemplate(
            template_id,
            replace(item, correlation_key="template:" + template_id),
            signature,
            SimpleNamespace(),
        )
        for template_id in template_ids
    }
    common = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {},
        {},
        Queue(),
        templates_by_key=templates,
    )
    database = FakeStateDB()
    config = DLDDConfig(inactive_fault_retention_period=42)
    correlation = CorrelationEngine({})
    work_items = {item.correlation_key: item} if runtime_item else {}
    if runtime_item:
        correlation.register_work_item(signature, item, "sha256:test")
    orchestrator = PrimaryOrchestrator(
        Queue(),
        {"common": common},
        work_items,
        correlation,
        TelemetryPublisher(database, config),
        config,
        "sha256:test",
    )
    record = FaultRecord(
        rule_id=item.rule_id,
        rule_name=item.rule_name,
        rule_version=item.rule_version,
        schema_version=item.schema_version,
        active_rules_checksum="sha256:test",
        component_type=item.component_type,
        component_name=item.component_name,
        symptom=item.symptom,
        severity=item.severity,
        priority=item.priority,
        error_type=item.error_type,
        description="Dynamic sensor fault.",
        status=record_status,
        origin_time=10,
        last_detection_time=11,
        repair_actions=("ACTION_REPLACE",),
        remote_action_time_window=77,
    )
    orchestrator.telemetry.publish_fault(record)
    if runtime_item:
        identity = (item.rule_id, item.component_name)
        orchestrator.faults[identity] = record
        orchestrator.published_by_key[
            (record.component_name, record.symptom)
        ] = record.rule_id
    else:
        orchestrator.reconcile_existing_faults()
    return orchestrator, database, common, item, signature, record


def dse_expansion_event(
    signature,
    template_id="template",
    *,
    present_instances=(),
    added_items=(),
    removed_keys=(),
    observed_at=1234.9,
):
    return DSEExpansionEvent(
        monitor_id="common",
        plan_generation="sha256:test",
        template_id=template_id,
        signature=signature,
        added_items=tuple(added_items),
        removed_keys=tuple(removed_keys),
        present_instances=tuple(present_instances),
        observed_at=observed_at,
    )


def test_dse_retirement_ownership_lifecycle():
    """Retire only after discovery releases every live owner."""

    orchestrator, database, plan, item, signature, record = (
        dse_retirement_fixture()
    )

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            removed_keys=(item.correlation_key,),
        )
    )

    payload = database.values[record.redis_key]
    assert payload["status"] == "INACTIVE"
    assert payload["reason"] == (
        "DSE discovery no longer reports instance 'DYNAMIC0'"
    )
    assert payload["last_detection_time"] == "1234"
    assert json.loads(payload["repair_actions"]) == []
    assert database.ttls[record.redis_key] == 42
    assert record.inactive_deadline == 1276.9
    assert database.delete_calls == 0
    assert orchestrator.faults[(item.rule_id, "DYNAMIC0")].status == "INACTIVE"
    assert not orchestrator.service_diagnostics

    plan.add_expanded_item(item)
    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            present_instances=("DYNAMIC0",),
            added_items=(item,),
        )
    )
    decision = orchestrator.correlation.consume(
        evidence(item, EvaluationResultType.MATCH, 2)
    )
    orchestrator._publish_decision(decision)
    assert database.values[record.redis_key]["status"] == "ACTIVE"
    assert database.values[record.redis_key]["reason"] == ""
    assert database.values[record.redis_key]["occurrences"] == "2"

    # FIFO registration permits retirement before the exact child is dropped.
    orchestrator, database, plan, item, signature, record = (
        dse_retirement_fixture()
    )
    # Reproduce the intentional queue ordering: the monitor publishes removal
    # before physically dropping the exact child so registration events remain
    # FIFO ahead of child evidence.
    plan.add_expanded_item(item)

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            removed_keys=(item.correlation_key,),
        )
    )

    assert item.correlation_key in plan.expanded_item_snapshot()
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert database.delete_calls == 0

    # A different live child for the scope still blocks retirement.
    orchestrator, database, plan, item, signature, record = (
        dse_retirement_fixture()
    )
    another = replace(
        item,
        source_id="dse:dynamic0:other",
        correlation_key=(
            "1000001:1:DYNAMIC0:SYMPTOM_OVER_THRESHOLD:dse:dynamic0:other"
        ),
    )
    plan.add_expanded_item(item)
    plan.add_expanded_item(another)
    orchestrator.work_items[another.correlation_key] = another

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            removed_keys=(item.correlation_key,),
        )
    )

    assert database.values[record.redis_key]["status"] == "ACTIVE"

    # Multiple templates must all relinquish the instance before retirement.
    orchestrator, database, plan, item, signature, record = (
        dse_retirement_fixture(("template-a", "template-b"))
    )

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            "template-a",
            removed_keys=(item.correlation_key,),
        )
    )
    assert database.values[record.redis_key]["status"] == "ACTIVE"

    second = replace(
        item,
        source_id="dse:dynamic0:second",
        correlation_key=(
            "1000001:1:DYNAMIC0:SYMPTOM_OVER_THRESHOLD:dse:dynamic0:second"
        ),
    )
    plan.add_expanded_item(second)
    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            "template-b",
            present_instances=("DYNAMIC0",),
            added_items=(second,),
        )
    )
    assert database.values[record.redis_key]["status"] == "ACTIVE"

    plan.remove_expanded_item(second.correlation_key)
    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            "template-b",
            removed_keys=(second.correlation_key,),
        )
    )
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert database.delete_calls == 0


def test_dse_restart_reconciliation_retry_and_rediscovery():
    """Reconcile retained DSE faults, retry writes, and clear retired history."""

    orchestrator, database, unused_plan, unused_item, signature, record = (
        dse_retirement_fixture(runtime_item=False)
    )
    identity = (record.rule_id, record.component_name)
    assert identity in orchestrator.pending_dynamic_faults

    orchestrator.process_expansion(
        dse_expansion_event(signature)
    )
    assert identity not in orchestrator.pending_dynamic_faults
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert "DSE discovery" in database.values[record.redis_key][
        "reason"
    ]

    # An unchanged current inventory still reconciles a retained fault.
    orchestrator, unused_database, plan, item, signature, record = (
        dse_retirement_fixture(runtime_item=False)
    )
    identity = (record.rule_id, record.component_name)
    # Model a child already registered before the retained fault was loaded.
    # The next complete inventory snapshot has no added_items delta.
    plan.add_expanded_item(item)
    orchestrator.work_items[item.correlation_key] = item
    orchestrator.correlation.register_work_item(
        signature, item, "sha256:test"
    )

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            present_instances=(record.component_name,),
        )
    )

    assert identity not in orchestrator.pending_dynamic_faults
    assert identity in orchestrator.reconciliation
    assert plan.control_queue.get_nowait().correlation_key == item.correlation_key

    # Database write failure leaves a dirty retained row which can be retried.
    orchestrator, database, unused_plan, item, signature, record = (
        dse_retirement_fixture()
    )
    database.fail_writes_with(RuntimeError("STATE_DB unavailable"))

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            removed_keys=(item.correlation_key,),
        )
    )

    identity = (item.rule_id, item.component_name)
    assert orchestrator.faults[identity].status == "INACTIVE"
    assert identity in orchestrator.dirty_faults
    assert database.values[record.redis_key]["status"] == "ACTIVE"

    database.clear_failures()
    orchestrator._retry_dirty_faults()
    assert identity not in orchestrator.dirty_faults
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert database.delete_calls == 0

    # Rediscovery begins with empty correlation history for every event.
    conditions = SimpleNamespace(
        events=(
            SimpleNamespace(id=1, match_count=1, match_period=0),
            SimpleNamespace(id=2, match_count=1, match_period=0),
        ),
        logic_tree=parse_logic("1 AND 2"),
        logic_lookback_time=0,
    )
    signature = SimpleNamespace(
        metadata=SimpleNamespace(id=1000001),
        conditions=conditions,
    )
    event_a = SimpleNamespace(
        rule_id=1000001,
        component_name="DYNAMIC0",
        event_id=1,
        correlation_key="event-a",
        source_id="source-a",
        value_config=ValueConfig(),
    )
    event_b = SimpleNamespace(
        rule_id=1000001,
        component_name="DYNAMIC0",
        event_id=2,
        correlation_key="event-b",
        source_id="source-b",
        value_config=ValueConfig(),
    )
    correlation = CorrelationEngine({})
    correlation.register_work_item(signature, event_a, "sha256:test")
    correlation.register_work_item(signature, event_b, "sha256:test")
    assert not correlation.consume(
        evidence(event_b, EvaluationResultType.MATCH, 1)
    ).active

    correlation.retire(1000001, "DYNAMIC0")
    correlation.unregister_work_item(event_a)
    correlation.unregister_work_item(event_b)
    correlation.register_work_item(signature, event_a, "sha256:test")
    correlation.register_work_item(signature, event_b, "sha256:test")

    # Event B must be sampled again after rediscovery. Its old match must not
    # combine with the new Event A sample.
    assert not correlation.consume(
        evidence(event_a, EvaluationResultType.MATCH, 2)
    ).active


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


def test_local_action_recheck_and_artifact_lifecycle():
    """Run successful, quarantined, arbitrated, and timed-out action paths."""

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
    assert (
        orchestrator.faults[(item.rule_id, item.component_name)].status
        == "CANDIDATE"
    )
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
    assert action_state["rule_instance_id"] == "1000001@PSU"
    assert "correlation_key" not in action_state
    assert action_state["worker_id"] == "worker-1"
    assert action_state["started_at"] == 100.0
    assert action_state["completed_at"] == 101.0
    assert database.values[fault_key]["origin_time"] == "101"
    assert database.values[fault_key]["last_detection_time"] == "102"
    assert json.loads(database.values[fault_key]["events"])[0]["value_read"] == 51.5
    assert (
        json.loads(database.values[fault_key]["healthz_artifact"])["artifact_id"]
        == "dldd-test.tar.gz"
    )

    # Artifact requests receive floored time and complete component identity.
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

    assert request == {
        "artifact_id": "dldd-test.tar.gz",
        "requested_at": 101,
        "location": "/var/lib/sonic/dldd/artifacts/dldd-test.tar.gz",
    }
    assert artifact_client.metadata["timestamp"] == 1234
    assert artifact_client.metadata["component_info"] == {
        "component": "PSU",
        "name": execution.component_name,
    }

    # Ordinary evidence cannot race the mandatory post-action recheck.
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

    # A higher-severity action candidate reserves publication ownership.
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

    # Missing recheck evidence retries, then preserves the active fault.
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

    assert orchestrator._publish_fault_record(record)
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


def test_action_runner_failure_and_deadline_still_recheck():
    """Missing and stuck action runners cannot bypass mandatory recheck."""

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
    assert (
        bundle.monitor_plans["redis"].control_queue.get_nowait().command.value
        == "RECHECK_ONCE"
    )
    orchestrator.process_event(
        evidence(item, EvaluationResultType.NO_MATCH, 2, from_recheck=True)
    )

    fault = database.values["FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"]
    assert fault["status"] == "INACTIVE"
    assert json.loads(fault["local_action_state"])["state"] == "EXECUTION_ERROR"

    # A future which misses its action deadline also advances to recheck.
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
    assert pending.action_result.state == "TIMED_OUT"
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


def test_restart_and_periodic_fault_reconciliation_lifecycle():
    """Handle foreign, malformed, stale, and healthy retained fault rows."""

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
            "producer": "another-service",
            "status": "ACTIVE",
            "component_type": "FOREIGN",
            "component_name": "FOREIGN",
        },
    )
    database.hset(
        "FAULT_INFO|BROKEN|SYMPTOM_UNKNOWN",
        {
            "producer": "dldd",
            "rule": "BROKEN",
            "rule_id": 1000001,
            "schema_version": rules.schema_version,
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

    # Stale owned rows become inactive but preserve their wire history.
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
            "producer": "dldd",
            "rule": item.rule_name,
            "rule_id": item.rule_id,
            "rule_version": item.rule_version,
            "schema_version": item.schema_version,
            "active_rules_checksum": "sha256:old",
            "component_type": item.component_type,
            "component_name": item.component_name,
            "component_serial_number": "",
            "symptom": item.symptom,
            "status": "ACTIVE",
            "origin_time": 10,
            "last_detection_time": 11,
            "occurrences": 2,
            "description": "Vendor-authored PSU fault description.",
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
    assert database.values[key]["description"] == (
        "Vendor-authored PSU fault description."
    )
    assert database.values[key]["reason"] == (
        "stale rule/source after DLDD restart"
    )

    decision = orchestrator.correlation.consume(
        evidence(item, EvaluationResultType.MATCH, 1)
    )
    orchestrator._publish_decision(decision)

    assert database.values[key]["status"] == "ACTIVE"
    assert database.values[key]["active_rules_checksum"] == "sha256:new"
    assert database.values[key]["schema_version"] == item.schema_version
    assert database.values[key]["reason"] == ""

    # Normal periodic confirmation is operational work, not a diagnostic.
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
        DLDDConfig(active_fault_recheck_interval=60),
        "sha256:test",
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )
    initial = orchestrator.correlation.consume(
        evidence(item, EvaluationResultType.MATCH, 1)
    )
    orchestrator._publish_decision(initial)

    for sequence in (2, 3):
        clock[0] += 61
        orchestrator.tick()
        assert (item.rule_id, item.component_name) in orchestrator.reconciliation
        orchestrator.process_event(
            evidence(
                item,
                EvaluationResultType.MATCH,
                sequence,
                from_recheck=True,
            )
        )

    assert not orchestrator.service_diagnostics
    assert orchestrator.faults[
        (item.rule_id, item.component_name)
    ].status == "ACTIVE"
