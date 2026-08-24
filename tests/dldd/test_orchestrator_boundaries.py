"""Focused behavioral coverage for primary-thread orchestration boundaries."""

import json
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
from queue import Queue
from types import SimpleNamespace

import pytest

from dldd.actions import ActionSequenceResult
from dldd.config import DLDDConfig
from dldd.correlation import CorrelationEngine
from dldd.models import Operation
from dldd.orchestrator import PrimaryOrchestrator, Reconciliation
from dldd.planner import build_plans
from dldd.runtime import (
    CollectedValue,
    DSEExpansionEvent,
    EvaluationResult,
    EvaluationResultType,
    FaultEvidenceEvent,
    FaultRecord,
    MonitorWorkState,
    RuleRuntimeStatus,
)
from dldd.telemetry import TelemetryPublisher
from dldd.validation import load_rules
from tests.dldd_fakes import FakeStateDB
from tests.dldd.test_orchestrator import (
    dse_expansion_event,
    dse_retirement_fixture,
    evidence,
)


def runtime_fixture(*, config=None, source_probe=None, artifact_client=None):
    rules = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))
    database = FakeStateDB()
    clock = [0.0]
    config = config or DLDDConfig()
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, config),
        config,
        "sha256:test",
        artifact_client=artifact_client,
        source_lifecycle_probe=source_probe,
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )
    return orchestrator, bundle, item, database, clock


def competing_rules_fixture():
    with open(
        "tests/dldd/fixtures/valid-redis-rule.json", encoding="utf-8"
    ) as stream:
        document = json.load(stream)
    high = document["signatures"][0]
    high["signature"]["actions"]["repair_actions"].pop("local_actions", None)
    low = deepcopy(high)
    low_metadata = low["signature"]["metadata"]
    low_metadata["id"] += 1
    low_metadata["name"] = "LOW_PRIORITY_RULE"
    low_metadata["severity"] = "MINOR"
    document["signatures"].append(low)
    rules = load_rules(json.dumps(document))
    bundle = build_plans(
        rules.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    items = {item.rule_id: item for item in bundle.work_items.values()}
    database = FakeStateDB()
    orchestrator = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
    )
    return orchestrator, items[1000001], items[1000002], database


def event(
    item,
    result_type,
    sequence=1,
    *,
    from_recheck=False,
    retryable=True,
    runtime_status=None,
    error="source failed",
):
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
            result_type,
            value=value,
            evaluator_type="comparison",
            operator=">",
            expected=50.0,
            completed_at=100.0 + sequence,
            error_category="collection_error",
            error=error,
            retryable=retryable,
        ),
        from_recheck=from_recheck,
        runtime_status=runtime_status,
    )


def runtime_status(item, *, failures=1, state="DEGRADED"):
    return RuleRuntimeStatus(
        state=state,
        rule_id=item.rule_id,
        rule_name=item.rule_name,
        event_id=item.event_id,
        component_name=item.component_name,
        source_id=item.source_id,
        correlation_key=item.correlation_key,
        failure_count=failures,
        last_success_timestamp=44,
    )


def active_record(orchestrator, item, **updates):
    execution = orchestrator.correlation.executions[
        (item.rule_id, item.component_name)
    ]
    record = orchestrator._new_fault_record(
        execution, status="ACTIVE", observed_at=10
    )
    for name, value in updates.items():
        setattr(record, name, value)
    identity = (item.rule_id, item.component_name)
    orchestrator.faults[identity] = record
    orchestrator.published_by_key[
        (record.component_name, record.symptom)
    ] = item.rule_id
    return identity, record


def add_static_owner(orchestrator, item, signature, *, register=False):
    """Add the non-DSE work which keeps a component scope executable."""

    static = replace(
        item,
        source_type="platform_api",
        source_id="platform:static-owner",
        correlation_key=item.correlation_key + ":static",
        dse_binding=None,
    )
    orchestrator.work_items[static.correlation_key] = static
    if register:
        orchestrator.correlation.register_work_item(
            signature, static, "sha256:test"
        )
    return static


def test_runtime_config_update_and_fault_republish_lifecycle():
    """Apply latest config, refresh deadlines, and retry inactive publication."""

    orchestrator, _, item, database, clock = runtime_fixture()
    identity, record = active_record(orchestrator, item, status="INACTIVE")
    orchestrator.next_active_recheck[identity] = 100
    orchestrator.source_status[item.source_id] = {
        "state": "UNAVAILABLE",
        "since": 900,
        "grace_deadline": 999,
    }
    orchestrator.source_status["already-recovered"] = {"state": "RECOVERED"}
    orchestrator.faults[(999, "ACTIVE")] = replace(
        record,
        rule_id=999,
        component_name="ACTIVE",
        status="ACTIVE",
    )
    orchestrator.queue_config_update(DLDDConfig(active_fault_recheck_interval=20))
    latest = DLDDConfig(
        active_fault_recheck_interval=7,
        source_unavailable_grace_period=11,
        inactive_fault_retention_period=22,
    )
    orchestrator.queue_config_update(latest)

    orchestrator.tick()

    assert orchestrator.config is latest
    assert orchestrator.next_active_recheck[identity] == 7
    assert orchestrator.source_status[item.source_id]["grace_deadline"] == 911
    assert database.ttls[record.redis_key] == 22

    # A failed inactive-row TTL refresh remains dirty until DB recovery.
    orchestrator, _, item, database, clock = runtime_fixture()
    identity, record = active_record(orchestrator, item, status="INACTIVE")
    database.fail_writes_with(RuntimeError("STATE_DB unavailable"))
    latest = DLDDConfig(inactive_fault_retention_period=19)
    orchestrator.queue_config_update(latest)

    orchestrator.tick()

    assert orchestrator.config is latest
    assert orchestrator.telemetry.config is latest
    assert identity in orchestrator.dirty_faults
    assert record.redis_key not in database.values

    database.clear_failures()
    clock[0] = 5
    orchestrator.tick()
    assert identity not in orchestrator.dirty_faults
    assert database.ttls[record.redis_key] == 19


def test_evidence_dispatch_and_localized_failure_policy():
    """Bound dispatch, release unmapped work, and localize evaluation failures."""

    orchestrator, bundle, item, _, _ = runtime_fixture()
    ordinary = event(item, EvaluationResultType.NO_MATCH)
    orchestrator._primary_processing_failures[item.correlation_key] = 2
    orchestrator.evidence_queue.put(ordinary)
    orchestrator.evidence_queue.put(
        DSEExpansionEvent(
            monitor_id="wrong-monitor",
            plan_generation="wrong-generation",
            template_id="unknown",
            signature=next(iter(bundle.signatures.values())).signature,
        )
    )

    assert orchestrator.process_batch(limit=1) == 1
    assert item.correlation_key not in orchestrator._primary_processing_failures
    assert orchestrator.evidence_queue.qsize() == 1
    assert orchestrator.process_batch(limit=1) == 1
    assert orchestrator.service_diagnostics[-1]["reason"] == (
        "dse_expansion_registration_failed"
    )

    # Valid but unmapped evidence is released without publishing a fault.
    orchestrator, bundle, item, database, _ = runtime_fixture()
    orchestrator.correlation.executions.clear()

    orchestrator.process_event(event(item, EvaluationResultType.MATCH))

    command = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert command.command.value == "RESUME"
    assert command.reason == "unmapped evidence"
    assert not database.values

    # Evaluation errors remain localized and follow retry/failure policy.
    for retryable, failures, expected_state, expected_command in (
        (True, 1, "DEGRADED", "RESUME"),
        (False, 1, "BROKEN", "SUSPEND"),
        (True, 2, "BROKEN", "SUSPEND"),
    ):
        config = DLDDConfig(individual_max_failure_threshold=1)
        orchestrator, bundle, item, _, _ = runtime_fixture(config=config)
        status = runtime_status(item, failures=failures)

        orchestrator.process_event(
            event(
                item,
                EvaluationResultType.EVALUATION_ERROR,
                retryable=retryable,
                runtime_status=status,
                error="invalid comparator output",
            )
        )

        broken = orchestrator.broken_rules[item.correlation_key]
        command = bundle.monitor_plans["redis"].control_queue.get_nowait()
        assert broken["state"] == expected_state
        assert command.command.value == expected_command


def test_source_failure_recovery_and_lifecycle_probe_transitions(caplog):
    """Run grace, fatal, partial recovery, and expected outage transitions."""

    config = DLDDConfig(
        individual_max_failure_threshold=1,
        source_unavailable_grace_period=5,
    )
    orchestrator, bundle, item, _, clock = runtime_fixture(config=config)
    status = runtime_status(item, failures=2)

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.SOURCE_UNAVAILABLE,
            runtime_status=status,
        )
    )
    first = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert first.target_state == MonitorWorkState.DEGRADED
    assert item.correlation_key not in orchestrator.broken_rules

    clock[0] = 6
    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.COLLECTION_ERROR,
            sequence=2,
            runtime_status=status,
        )
    )
    fatal = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert fatal.target_state == MonitorWorkState.BROKEN
    assert orchestrator.broken_rules[item.correlation_key]["state"] == "BROKEN"

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.SOURCE_RECOVERED,
            sequence=3,
            runtime_status=runtime_status(item, failures=0),
        )
    )
    recovered = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert recovered.command.value == "RECHECK_ONCE"
    assert orchestrator.source_status[item.source_id]["state"] == "RECOVERED"
    assert item.correlation_key not in orchestrator.broken_rules

    # Partial recovery leaves the failed sibling as the only affected rule.
    orchestrator, bundle, item, database, _ = runtime_fixture()
    other = replace(
        item,
        rule_id=item.rule_id + 1,
        rule_name="OTHER_RULE",
        correlation_key=item.correlation_key + ":other",
    )
    orchestrator.work_items[other.correlation_key] = other
    failed_keys = {item.correlation_key, other.correlation_key}
    orchestrator._source_failure_keys[item.source_id] = set(failed_keys)
    identity, record = active_record(orchestrator, item)
    orchestrator.source_status[item.source_id] = {"state": "UNAVAILABLE"}

    orchestrator.process_event(
        event(item, EvaluationResultType.SOURCE_RECOVERED)
    )

    status = orchestrator.source_status[item.source_id]
    assert status["affected_rules"] == [other.rule_id]
    assert status["stale_faults"] == []
    assert record.stale_source is False
    assert identity not in orchestrator.dirty_faults
    assert bundle.monitor_plans["redis"].control_queue.get_nowait().command.value == (
        "RECHECK_ONCE"
    )
    assert database.values == {}

    # Probe failure ends graceful suspension as ordinary unavailability.
    def failing_probe(_item):
        raise RuntimeError("platform API unavailable")

    orchestrator, bundle, item, _, clock = runtime_fixture(
        source_probe=failing_probe
    )
    orchestrator._suspended_sources[item.source_id] = {item.correlation_key}
    orchestrator.source_status[item.source_id] = {"state": "SUSPENDED"}
    with caplog.at_level("WARNING", logger="dldd.orchestrator"):
        orchestrator.tick()

    status = orchestrator.source_status[item.source_id]
    assert status["state"] == "UNAVAILABLE"
    assert status["graceful"] is False
    command = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert command.target_state == MonitorWorkState.DEGRADED
    assert [record.getMessage() for record in caplog.records] == [
        "source lifecycle probe failed for {}; treating source as unavailable: "
        "platform API unavailable".format(item.component_name)
    ]

    orchestrator._suspended_sources[item.source_id] = set()
    clock[0] = 5
    orchestrator.tick()
    assert item.source_id not in orchestrator._suspended_sources

    # A successful expected-outage probe keeps the source suspended.
    orchestrator, _, item, _, _ = runtime_fixture(
        source_probe=lambda unused_item: True
    )
    orchestrator._suspended_sources[item.source_id] = {
        item.correlation_key
    }
    orchestrator.source_status[item.source_id] = {"state": "SUSPENDED"}

    orchestrator.tick()

    assert orchestrator._suspended_sources[item.source_id] == {
        item.correlation_key
    }
    assert orchestrator.source_status[item.source_id]["state"] == "SUSPENDED"


def test_service_health_and_monitor_release_state_projection():
    """Project service health and release keys to their owning monitor state."""

    orchestrator, _, item, _, _ = runtime_fixture(
        config=DLDDConfig(broken_rules_max_threshold=0)
    )
    assert orchestrator.service_state() == "OK"
    orchestrator.source_status[item.source_id] = {"state": "UNAVAILABLE"}
    assert orchestrator.service_state() == "DEGRADED"
    orchestrator.broken_rules[item.correlation_key] = {
        "rule_id": item.rule_id,
        "state": "BROKEN",
    }
    assert orchestrator.service_state() == "BROKEN|FATAL"

    # Key release preserves broken, suspended, unavailable, and ready states.
    orchestrator, bundle, item, _, _ = runtime_fixture()
    queue = bundle.monitor_plans["redis"].control_queue

    orchestrator.broken_rules[item.correlation_key] = {"state": "BROKEN"}
    orchestrator._release_key(item.correlation_key, "release")
    assert queue.get_nowait().target_state == MonitorWorkState.BROKEN

    orchestrator.broken_rules.clear()
    orchestrator.source_status[item.source_id] = {"state": "SUSPENDED"}
    orchestrator._release_key(item.correlation_key, "release")
    assert queue.get_nowait().target_state == MonitorWorkState.SUSPENDED

    orchestrator.source_status[item.source_id] = {"state": "UNAVAILABLE"}
    orchestrator._release_key(item.correlation_key, "release")
    assert queue.get_nowait().target_state == MonitorWorkState.DEGRADED

    orchestrator.source_status.clear()
    orchestrator._release_key(item.correlation_key, "release")
    assert queue.get_nowait().target_state == MonitorWorkState.READY


class ArtifactStates:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def status(self, _artifact_id):
        if self.error:
            raise self.error
        return SimpleNamespace(as_payload=lambda: self.response)


def test_artifact_refresh_ignores_ineligible_unchanged_and_failed_requests():
    artifact = {"artifact_id": "a", "state": "REQUESTED"}
    client = ArtifactStates(response=artifact)
    orchestrator, _, item, database, _ = runtime_fixture(artifact_client=client)
    identity, record = active_record(orchestrator, item, healthz_artifact=artifact)
    orchestrator._refresh_artifact_states()
    assert database.values == {}

    client.response = {"artifact_id": "a", "state": "COMPLETED"}
    orchestrator._refresh_artifact_states()
    assert record.healthz_artifact["state"] == "COMPLETED"
    assert record.redis_key in database.values

    record.healthz_artifact = {"artifact_id": "a", "state": "REQUESTED"}
    client.error = RuntimeError("healthz unavailable")
    orchestrator._refresh_artifact_states()
    assert identity in orchestrator.faults

    record.healthz_artifact = {"state": "REQUESTED"}
    orchestrator._refresh_artifact_states()


def test_fault_serialization_operation_payload_and_dirty_retry():
    """Validate wire fields, retry filtering, and canonical operations."""

    with pytest.raises(ValueError, match="component_type"):
        PrimaryOrchestrator._fault_from_payload({"component_type": "PSU"})

    record = PrimaryOrchestrator._fault_from_payload(
        {
            "rule_id": "7",
            "component_type": "VENDOR_WIDGET",
            "component_name": "WIDGET0",
            "repair_actions": [{"action": "ACTION_REPLACE"}, {}],
            "local_action_state": {
                "state": "FAILED",
                "action_suppressed": True,
            },
            "source_stale": True,
        }
    )
    assert record.rule_id == 7
    assert record.component_type == "VENDOR_WIDGET"
    assert record.repair_actions == ("ACTION_REPLACE",)
    assert record.action_suppressed is True
    assert record.stale_source is True

    # Dirty retry drops absent/candidate records and republishes active records.
    orchestrator, _, item, database, clock = runtime_fixture()
    active_identity, active = active_record(orchestrator, item)
    missing_identity = (999, "missing")
    candidate_identity = (998, "candidate")
    candidate = replace(active, rule_id=998, status="CANDIDATE")
    orchestrator.faults[candidate_identity] = candidate
    orchestrator.dirty_faults.update(
        {active_identity, missing_identity, candidate_identity}
    )

    orchestrator._retry_dirty_faults()

    assert missing_identity not in orchestrator.dirty_faults
    assert candidate_identity not in orchestrator.dirty_faults
    assert active_identity not in orchestrator.dirty_faults
    assert active.redis_key in database.values
    clock[0] = 1
    orchestrator.dirty_faults.add(active_identity)
    orchestrator._retry_dirty_faults()
    assert active_identity in orchestrator.dirty_faults

    # Canonical optional fields survive operation materialization.
    operation = Operation(
        options={"token": "vendor", "path": "ignored"},
        type="i2c",
        command="read",
        argv=("i2cget", "-y", "1"),
        path={"bus": 1},
        timeout=3,
        max_output_bytes=8,
        executor=None,
    )
    assert operation.as_runtime_payload() == {
        "token": "vendor",
        "type": "i2c",
        "command": "read",
        "argv": ["i2cget", "-y", "1"],
        "path": {"bus": 1},
        "timeout": 3,
        "max_output_bytes": 8,
    }


def test_reconciliation_evidence_ownership_and_conservative_completion():
    """Quarantine ordinary evidence and conservatively finish nondecisions."""

    orchestrator, bundle, item, _, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item)
    execution = orchestrator.correlation.executions[identity]
    orchestrator._start_reconciliation(execution, record, "bootstrap")
    queue = bundle.monitor_plans["redis"].control_queue
    queue.get_nowait()

    orchestrator.process_event(event(item, EvaluationResultType.MATCH))
    held = queue.get_nowait()
    assert held.command.value == "HOLD"
    assert held.reason == "reconciliation_evidence_quarantined"

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.SOURCE_RECOVERED,
            sequence=2,
            from_recheck=True,
        )
    )
    retry = queue.get_nowait()
    assert retry.command.value == "RECHECK_ONCE"
    assert retry.reason == "source_recovered_recheck_required"
    assert identity in orchestrator.reconciliation

    # A nondecisive recheck preserves the retained active state.
    orchestrator, bundle, item, database, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item)
    execution = orchestrator.correlation.executions[identity]
    orchestrator._start_reconciliation(execution, record, "bootstrap")
    queue = bundle.monitor_plans["redis"].control_queue
    queue.get_nowait()

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.COLLECTION_ERROR,
            from_recheck=True,
            runtime_status=runtime_status(item, failures=1),
        )
    )

    assert identity not in orchestrator.reconciliation
    assert identity in orchestrator.uncertain_faults
    assert record.status == "ACTIVE"
    assert record.stale_source is True
    assert database.values[record.redis_key]["source_stale"] == "true"
    assert [queue.get_nowait().command.value for _ in range(2)] == ["HOLD", "RESUME"]


def test_fault_lifetime_suppression_failure_and_occurrence_projection():
    """Preserve action suppression, isolate errors, and advance occurrences."""

    orchestrator, bundle, item, database, _ = runtime_fixture()
    _, record = active_record(orchestrator, item, action_suppressed=True)

    orchestrator.process_event(event(item, EvaluationResultType.MATCH))

    command = bundle.monitor_plans["redis"].control_queue.get_nowait()
    assert command.command.value == "RESUME"
    assert "already executed" in command.reason
    assert not orchestrator.pending
    assert record.status == "ACTIVE"
    assert not database.values

    # Primary processing and release failures remain localized to the work key.
    orchestrator, _, item, _, _ = runtime_fixture()
    orchestrator._command_event = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("monitor queue rejected command")
    )

    orchestrator._handle_processing_failure(
        event(item, EvaluationResultType.MATCH), RuntimeError("correlation failed")
    )

    assert orchestrator.broken_rules[item.correlation_key]["state"] == "DEGRADED"
    assert orchestrator.service_diagnostics[-1]["reason"] == (
        "primary_processing_error"
    )

    # Only active faults associated with failed work appear in stale projection.
    orchestrator, _, item, _, _ = runtime_fixture()
    _, active = active_record(orchestrator, item)
    inactive = replace(
        active,
        rule_id=item.rule_id + 1,
        component_name="PSU-INACTIVE",
        status="INACTIVE",
    )
    orchestrator.faults[(inactive.rule_id, inactive.component_name)] = inactive

    assert orchestrator._stale_fault_keys({item.correlation_key}) == [
        active.redis_key
    ]
    assert orchestrator._stale_fault_keys({"not-an-execution-key"}) == []

    # Reactivating retained history creates a candidate and increments lifetime.
    orchestrator, _, item, _, _ = runtime_fixture()
    identity, _ = active_record(
        orchestrator, item, status="INACTIVE", occurrences=4
    )

    orchestrator.process_event(event(item, EvaluationResultType.MATCH))

    assert orchestrator.faults[identity].status == "CANDIDATE"
    assert orchestrator.faults[identity].occurrences == 5


class FailedActionRunner:
    def submit(self, _rule_name, _actions, _default_timeout):
        future = Future()
        future.dldd_worker_id = "failed-worker"
        future.set_exception(RuntimeError("worker crashed"))
        return future


def test_action_failure_reconciliation_retry_and_nondecisive_recheck():
    """Advance failed actions and preserve faults through uncertain rechecks."""

    orchestrator, bundle, item, _, _ = runtime_fixture()
    orchestrator.action_runner = FailedActionRunner()

    orchestrator.process_event(event(item, EvaluationResultType.MATCH))
    bundle.monitor_plans["redis"].control_queue.get_nowait()
    orchestrator.tick()

    pending = orchestrator.pending[(item.rule_id, item.component_name)]
    assert pending.phase == "WAITING_FOR_RECHECK"
    assert pending.action_result.state == "FAILED"
    assert pending.action_result.last_error == "worker crashed"

    # Tick retries owned reconciliation and drops orphaned active schedules.
    config = DLDDConfig(fault_evidence_ack_timeout=2)
    orchestrator, bundle, item, _, clock = runtime_fixture(config=config)
    identity, record = active_record(orchestrator, item)
    execution = orchestrator.correlation.executions[identity]
    orchestrator._start_reconciliation(execution, record, "bootstrap")
    queue = bundle.monitor_plans["redis"].control_queue
    queue.get_nowait()

    clock[0] = 2
    orchestrator.tick()
    assert queue.get_nowait().reason == "bootstrap_retry"

    invalid = (999999, "gone")
    orchestrator.next_active_recheck[invalid] = 0
    orchestrator.tick()
    assert invalid not in orchestrator.next_active_recheck

    # A nondecisive post-action sample never clears the confirmed fault.
    class CompletedRunner:
        def submit(self, _rule_name, _actions, _default_timeout):
            future = Future()
            future.dldd_worker_id = "worker"
            future.set_result(
                ActionSequenceResult("worker", "COMPLETED", 1, 2, ())
            )
            return future

    orchestrator, bundle, item, database, clock = runtime_fixture()
    orchestrator.action_runner = CompletedRunner()
    orchestrator.process_event(event(item, EvaluationResultType.MATCH))
    queue = bundle.monitor_plans["redis"].control_queue
    queue.get_nowait()
    orchestrator.tick()
    clock[0] = 61
    orchestrator.tick()
    queue.get_nowait()

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.SOURCE_UNAVAILABLE,
            sequence=2,
            from_recheck=True,
            runtime_status=runtime_status(item),
        )
    )

    fault = database.values["FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"]
    assert fault["status"] == "ACTIVE"
    assert fault["source_stale"] == "true"


def test_retained_fault_reconciliation_and_staleness_lifecycle():
    """Restore retained state, clear no-match faults, and publish staleness."""

    first, bundle, item, database, _ = runtime_fixture()
    identity, inactive = active_record(
        first,
        item,
        status="INACTIVE",
        occurrences=4,
        reason="authoritative DSE discovery removed the instance",
    )
    first.telemetry.publish_fault(inactive)

    second = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        clock=lambda: 0,
        wall_clock=lambda: 1000,
    )
    second.reconcile_existing_faults()
    assert second.faults[identity].status == "INACTIVE"
    assert second.faults[identity].occurrences == 4
    assert second.faults[identity].reason == (
        "authoritative DSE discovery removed the instance"
    )
    assert identity not in second.reconciliation
    assert bundle.monitor_plans["redis"].control_queue.empty()

    inactive.status = "ACTIVE"
    first.telemetry.publish_fault(inactive)
    third = PrimaryOrchestrator(
        Queue(),
        bundle.monitor_plans,
        bundle.work_items,
        CorrelationEngine(bundle.signatures),
        TelemetryPublisher(database, DLDDConfig()),
        DLDDConfig(),
        "sha256:test",
        clock=lambda: 0,
        wall_clock=lambda: 1000,
    )
    third.reconcile_existing_faults()
    assert identity in third.reconciliation
    assert bundle.monitor_plans[
        "redis"
    ].control_queue.get_nowait().command.value == "RECHECK_ONCE"

    # No evidence preserves active state and schedules periodic confirmation.
    orchestrator, bundle, item, _, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item)
    execution = orchestrator.correlation.executions[identity]
    state = Reconciliation(
        execution=execution,
        record=record,
        outstanding_rechecks=set(),
    )
    orchestrator.reconciliation[identity] = state

    orchestrator._complete_reconciliation(identity, state)

    assert orchestrator.correlation._active[identity] is True
    assert identity in orchestrator.next_active_recheck
    assert bundle.monitor_plans["redis"].control_queue.get_nowait().command.value == (
        "RESUME"
    )

    # A decisive no-match clears the persisted active record.
    orchestrator, bundle, item, database, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item)
    execution = orchestrator.correlation.executions[identity]
    orchestrator._start_reconciliation(execution, record, "bootstrap")
    queue = bundle.monitor_plans["redis"].control_queue
    queue.get_nowait()

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.NO_MATCH,
            from_recheck=True,
        )
    )

    assert record.status == "INACTIVE"
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert identity not in orchestrator.reconciliation

    # Source staleness publishes only transitions for active records.
    orchestrator, _, item, database, _ = runtime_fixture()
    identity, active = active_record(orchestrator, item)
    inactive = replace(
        active,
        rule_id=item.rule_id + 1,
        component_name="PSU-INACTIVE",
        status="INACTIVE",
    )
    orchestrator.faults[(inactive.rule_id, inactive.component_name)] = inactive
    orchestrator._source_failure_keys[item.source_id] = {item.correlation_key}

    orchestrator._refresh_fault_source_staleness()
    assert active.stale_source is True
    assert inactive.redis_key not in database.values

    orchestrator._source_failure_keys.clear()
    orchestrator._refresh_fault_source_staleness()
    assert active.stale_source is False
    assert database.values[active.redis_key].get("source_stale") is None


def test_artifact_request_error_is_returned_as_bounded_failed_state():
    class FailingArtifactClient:
        def request(self, *_args):
            raise RuntimeError("collector unavailable")

    orchestrator, bundle, _, _, _ = runtime_fixture(
        artifact_client=FailingArtifactClient()
    )
    execution = next(iter(bundle.signatures.values()))

    result = orchestrator._request_artifact(execution)

    assert result["state"] == "FAILED"
    assert result["last_error"] == "collector unavailable"
    assert result["requested_at"] == 1000
    assert result["completed_at"] == 1000


def test_removed_dse_work_cleans_only_its_runtime_state():
    """Exercise removal while failure, suspension, and recovery state evolve."""

    orchestrator, _, _, item, signature, _ = dse_retirement_fixture()
    sibling = replace(item, correlation_key=item.correlation_key + ":sibling")
    orchestrator.work_items[sibling.correlation_key] = sibling
    orchestrator._source_failure_keys[item.source_id] = {
        item.correlation_key,
        sibling.correlation_key,
    }
    orchestrator._suspended_sources[item.source_id] = {
        item.correlation_key,
        sibling.correlation_key,
    }
    orchestrator._source_unavailable_since[item.source_id] = 10
    orchestrator._primary_processing_failures[item.correlation_key] = 2
    orchestrator.source_status[item.source_id] = {
        "state": "SUSPENDED",
        "affected_rules": [item.rule_id],
    }

    orchestrator.process_expansion(
        dse_expansion_event(
            signature,
            authoritative=True,
            removed_keys=(item.correlation_key, "unknown-key"),
        )
    )

    assert orchestrator._source_failure_keys[item.source_id] == {
        sibling.correlation_key
    }
    assert orchestrator._suspended_sources[item.source_id] == {
        sibling.correlation_key
    }
    assert item.correlation_key not in orchestrator._primary_processing_failures
    assert orchestrator.source_status[item.source_id]["affected_rules"] == [
        sibling.rule_id
    ]
    assert item.correlation_key not in orchestrator.work_items

    # Once the removed item was the only suspended key, only its suspension
    # projection disappears; a failing sibling continues to own source state.
    orchestrator, _, _, item, _, _ = dse_retirement_fixture()
    sibling = replace(
        item,
        rule_id=item.rule_id + 1,
        correlation_key=item.correlation_key + ":failed-sibling",
    )
    orchestrator.work_items[sibling.correlation_key] = sibling
    orchestrator._source_failure_keys[item.source_id] = {
        item.correlation_key,
        sibling.correlation_key,
    }
    orchestrator._suspended_sources[item.source_id] = {item.correlation_key}
    orchestrator.source_status[item.source_id] = {"state": "SUSPENDED"}

    orchestrator.work_items.pop(item.correlation_key)
    orchestrator._forget_removed_work_state(item.correlation_key, item)

    assert orchestrator._source_failure_keys[item.source_id] == {
        sibling.correlation_key
    }
    assert item.source_id not in orchestrator._suspended_sources
    assert orchestrator.source_status[item.source_id]["affected_rules"] == [
        sibling.rule_id
    ]

    # Unavailable source projection likewise retains only the live sibling.
    orchestrator, _, _, item, _, _ = dse_retirement_fixture()
    sibling = replace(item, correlation_key=item.correlation_key + ":sibling")
    orchestrator.work_items[sibling.correlation_key] = sibling
    orchestrator._source_failure_keys[item.source_id] = {
        item.correlation_key,
        sibling.correlation_key,
    }
    orchestrator.source_status[item.source_id] = {"state": "UNAVAILABLE"}
    orchestrator._forget_removed_work_state(item.correlation_key, item)
    assert orchestrator._source_failure_keys[item.source_id] == {
        sibling.correlation_key
    }
    assert item.source_id not in orchestrator._suspended_sources

    # A healthy sibling keeps the shared recovery status after removal.
    orchestrator, _, _, item, _, _ = dse_retirement_fixture()
    sibling = replace(
        item,
        rule_id=item.rule_id + 1,
        correlation_key=item.correlation_key + ":healthy-sibling",
    )
    orchestrator.work_items[sibling.correlation_key] = sibling
    orchestrator.source_status[item.source_id] = {"state": "RECOVERED"}
    orchestrator.work_items.pop(item.correlation_key)
    orchestrator._forget_removed_work_state(item.correlation_key, item)
    assert orchestrator.source_status[item.source_id] == {"state": "RECOVERED"}


def test_authoritative_dse_retirement_waits_then_retains_inactive_history():
    """Follow one removed scope from ownership gates through retained history."""

    orchestrator, _, plan, item, signature, _ = dse_retirement_fixture()
    identity = (item.rule_id, item.component_name)
    plan.add_expanded_item(item)
    authoritative = dse_expansion_event(signature, authoritative=True)
    orchestrator._authoritative_dse_instances["template"] = set()

    assert not orchestrator._retire_absent_dse_fault(identity, authoritative)
    plan.remove_expanded_item(item.correlation_key)
    orchestrator.pending[identity] = SimpleNamespace()
    assert not orchestrator._retire_absent_dse_fault(identity, authoritative)
    orchestrator.pending.clear()
    orchestrator.reconciliation[identity] = SimpleNamespace()
    assert not orchestrator._retire_absent_dse_fault(identity, authoritative)

    # With no persisted fault, retirement clears correlation history only.
    orchestrator, _, _, item, signature, record = dse_retirement_fixture()
    identity = (item.rule_id, item.component_name)
    authoritative = dse_expansion_event(signature, authoritative=True)
    orchestrator._authoritative_dse_instances["template"] = set()
    orchestrator._dse_retirement_candidates.add(identity)
    orchestrator.correlation.consume(
        evidence(item, EvaluationResultType.MATCH, 1)
    )
    assert any(key[:2] == identity for key in orchestrator.correlation._events)
    orchestrator.faults.pop(identity)
    orchestrator.work_items.pop(item.correlation_key)
    orchestrator.correlation.unregister_work_item(item)

    assert orchestrator._retire_absent_dse_fault(identity, authoritative)
    assert identity not in orchestrator._dse_retirement_candidates
    assert not any(
        key[:2] == identity for key in orchestrator.correlation._events
    )
    assert identity not in orchestrator.correlation._active

    # Retiring the same scope with prior fault history retains an inactive row.
    orchestrator.faults[identity] = record
    record.status = "INACTIVE"
    orchestrator._dse_retirement_candidates.add(identity)
    assert orchestrator._retire_absent_dse_fault(identity, authoritative)
    assert identity not in orchestrator._dse_retirement_candidates
    assert record.status == "INACTIVE"
    assert "authoritative DSE discovery" in record.reason
    assert record.inactive_deadline == 1276.9

    # Startup reconciliation follows the same contract and refreshes the TTL.
    orchestrator, database, _, _, signature, record = dse_retirement_fixture(
        runtime_item=False,
        record_status="INACTIVE",
    )
    identity = (record.rule_id, record.component_name)
    assert identity in orchestrator._dse_retirement_candidates
    orchestrator.process_expansion(
        dse_expansion_event(signature, authoritative=True)
    )

    payload = database.values[record.redis_key]
    assert identity not in orchestrator._dse_retirement_candidates
    assert payload["status"] == "INACTIVE"
    assert "authoritative DSE discovery" in payload["reason"]
    assert payload["last_detection_time"] == "1234"
    assert database.ttls[record.redis_key] == 42
    assert database.delete_calls == 0

    # A missing publisher-owner projection is reconstructed, not deleted.
    orchestrator, database, _, item, signature, record = dse_retirement_fixture()
    identity = (item.rule_id, item.component_name)
    orchestrator._authoritative_dse_instances["template"] = set()
    orchestrator.published_by_key.clear()
    assert orchestrator._retire_absent_dse_fault(
        identity, dse_expansion_event(signature, authoritative=True)
    )
    assert orchestrator.published_by_key[(record.component_name, record.symptom)] == (
        item.rule_id
    )
    assert database.values[record.redis_key]["status"] == "INACTIVE"


def test_dse_retirement_defers_to_remaining_static_scope():
    """Static work keeps the scope alive and is rechecked only when active."""

    orchestrator, database, plan, item, signature, record = (
        dse_retirement_fixture()
    )
    identity = (item.rule_id, item.component_name)
    static = add_static_owner(orchestrator, item, signature, register=True)
    orchestrator.work_items.pop(item.correlation_key)
    orchestrator.correlation.unregister_work_item(item)
    orchestrator._authoritative_dse_instances["template"] = set()

    assert not orchestrator._retire_absent_dse_fault(
        identity,
        dse_expansion_event(signature, authoritative=True),
    )

    assert record.status == "ACTIVE"
    assert database.values[record.redis_key]["status"] == "ACTIVE"
    assert identity in orchestrator.reconciliation
    command = plan.control_queue.get_nowait()
    assert command.correlation_key == static.correlation_key
    assert command.command.value == "RECHECK_ONCE"

    # Missing and inactive records never create a reconciliation solely because
    # a DSE child disappeared. Exercise both the pre-registration and fully
    # registered static-owner paths used during activation.
    for register_static in (False, True):
        for fault_state in ("missing", "inactive"):
            orchestrator, _, _, item, signature, record = (
                dse_retirement_fixture()
            )
            identity = (item.rule_id, item.component_name)
            add_static_owner(
                orchestrator, item, signature, register=register_static
            )
            if register_static:
                orchestrator.work_items.pop(item.correlation_key)
                orchestrator.correlation.unregister_work_item(item)
            if fault_state == "missing":
                orchestrator.faults.pop(identity)
            else:
                record.status = "INACTIVE"
            orchestrator._authoritative_dse_instances["template"] = set()

            assert not orchestrator._retire_absent_dse_fault(
                identity,
                dse_expansion_event(signature, authoritative=True),
            )
            assert identity not in orchestrator.reconciliation
            if not register_static:
                assert identity not in orchestrator.uncertain_faults


    # A lifecycle hook may independently retain expected source suspension.
    orchestrator, _, item, _, _ = runtime_fixture(
        source_probe=lambda unused_item: True
    )
    orchestrator._suspended_sources[item.source_id] = {
        item.correlation_key
    }
    orchestrator.source_status[item.source_id] = {"state": "SUSPENDED"}

    orchestrator._recover_expected_source_suspensions()

    assert orchestrator._suspended_sources[item.source_id] == {
        item.correlation_key
    }
    assert orchestrator.source_status[item.source_id]["state"] == "SUSPENDED"


def test_dse_retirement_respects_fault_arbitration():
    """Retirement publishes the winning rule without erasing another owner."""

    for alternate_is_active in (True, False):
        orchestrator, database, _, item, signature, record = (
            dse_retirement_fixture()
        )
        identity = (item.rule_id, item.component_name)
        alternate_id = item.rule_id + 1
        alternate_signature = SimpleNamespace(
            metadata=SimpleNamespace(id=alternate_id),
            actions=signature.actions,
        )
        orchestrator.arbiter.retire = lambda *_args: SimpleNamespace(
            signature=alternate_signature
        )
        if alternate_is_active:
            alternate = replace(
                record,
                rule_id=alternate_id,
                rule_name="ALTERNATE",
                status="ACTIVE",
            )
            orchestrator.faults[(alternate_id, item.component_name)] = alternate
        orchestrator._authoritative_dse_instances["template"] = set()

        assert orchestrator._retire_absent_dse_fault(
            identity, dse_expansion_event(signature, authoritative=True)
        )

        payload = database.values[record.redis_key]
        if alternate_is_active:
            assert payload["rule_id"] == str(alternate_id)
            assert payload["status"] == "ACTIVE"
        else:
            assert payload["rule_id"] == str(item.rule_id)
            assert payload["status"] == "INACTIVE"

    # A retired rule which was already suppressed cannot replace the owner.
    orchestrator, database, _, item, signature, record = dse_retirement_fixture()
    identity = (item.rule_id, item.component_name)
    orchestrator._authoritative_dse_instances["template"] = set()
    orchestrator.published_by_key[(record.component_name, record.symptom)] = 999
    before = dict(database.values[record.redis_key])

    assert orchestrator._retire_absent_dse_fault(
        identity, dse_expansion_event(signature, authoritative=True)
    )
    assert database.values[record.redis_key] == before
    assert record.status == "INACTIVE"


def test_fault_arbiter_promotion_suppression_and_clear_lifecycle():
    """Promote winners while preserving active and inactive fault history."""

    orchestrator, high, low, database = competing_rules_fixture()
    orchestrator.process_event(event(low, EvaluationResultType.MATCH))
    low_record = orchestrator.faults[(low.rule_id, low.component_name)]
    low_record.occurrences = 3
    low_record.origin_time = 17

    orchestrator.process_event(event(high, EvaluationResultType.MATCH, sequence=2))

    high_record = orchestrator.faults[(high.rule_id, high.component_name)]
    assert high_record.origin_time == 17
    assert high_record.occurrences == 3
    assert database.values[high_record.redis_key]["rule_id"] == str(high.rule_id)

    # Promotion also inherits occurrence history from an inactive loser.
    orchestrator, high, low, _ = competing_rules_fixture()
    orchestrator.process_event(event(low, EvaluationResultType.MATCH))
    orchestrator.process_event(event(low, EvaluationResultType.NO_MATCH, sequence=2))
    low_record = orchestrator.faults[(low.rule_id, low.component_name)]
    low_record.occurrences = 4

    orchestrator.process_event(event(high, EvaluationResultType.MATCH, sequence=3))

    high_record = orchestrator.faults[(high.rule_id, high.component_name)]
    assert high_record.occurrences == 5

    # Loser updates stay suppressed until clearing the winner promotes it.
    orchestrator, high, low, database = competing_rules_fixture()
    orchestrator.process_event(event(high, EvaluationResultType.MATCH))
    orchestrator.process_event(event(low, EvaluationResultType.MATCH))
    fault_key = "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"
    assert database.values[fault_key]["rule_id"] == str(high.rule_id)

    # Clearing the suppressed rule cannot overwrite the active winner.
    orchestrator.process_event(event(low, EvaluationResultType.NO_MATCH, sequence=2))
    assert database.values[fault_key]["rule_id"] == str(high.rule_id)

    # Reactivate the lower rule, then clear the winner; arbitration promotes it
    # into the singular component/symptom row with inherited occurrence history.
    orchestrator.process_event(event(low, EvaluationResultType.MATCH, sequence=3))
    orchestrator.process_event(event(high, EvaluationResultType.NO_MATCH, sequence=4))
    assert database.values[fault_key]["rule_id"] == str(low.rule_id)
    assert database.values[fault_key]["status"] == "ACTIVE"


def test_owned_recheck_source_recovery_and_runtime_status_lifecycle():
    """Keep primary ownership through recovery, unmapped, and status updates."""

    class CompletedRunner:
        def submit(self, *_args):
            future = Future()
            future.dldd_worker_id = "worker"
            future.set_result(
                ActionSequenceResult("worker", "COMPLETED", 1, 2, ())
            )
            return future

    orchestrator, bundle, item, _, clock = runtime_fixture()
    orchestrator.action_runner = CompletedRunner()
    orchestrator.process_event(event(item, EvaluationResultType.MATCH))
    queue = bundle.monitor_plans["redis"].control_queue
    queue.get_nowait()
    orchestrator.tick()
    clock[0] = 61
    orchestrator.tick()
    queue.get_nowait()

    orchestrator.process_event(
        event(
            item,
            EvaluationResultType.SOURCE_RECOVERED,
            sequence=2,
            from_recheck=True,
        )
    )

    command = queue.get_nowait()
    assert command.command.value == "RECHECK_ONCE"
    assert command.hold_deadline is not None
    assert (item.rule_id, item.component_name) in orchestrator.pending

    # Unmapped owned evidence completes only after every expected key responds.
    for leave_outstanding in (False, True):
        orchestrator, bundle, item, _, _ = runtime_fixture()
        orchestrator.correlation.executions.clear()
        outstanding = {item.correlation_key}
        if leave_outstanding:
            outstanding.add("another-event-key")
        state = SimpleNamespace(
            outstanding_rechecks=outstanding,
            last_decision=None,
            recheck_failed=False,
            source_failed=False,
        )
        completed = []

        orchestrator._process_owned_recheck_evidence(
            event(item, EvaluationResultType.MATCH, from_recheck=True),
            (item.rule_id, item.component_name),
            state,
            pending_reason="recheck_pending",
            hold_deadline=10,
            complete=lambda identity, current: completed.append(
                (identity, current)
            ),
        )

        assert state.last_decision is None
        assert bool(completed) is not leave_outstanding
        assert (
            bundle.monitor_plans["redis"]
            .control_queue.get_nowait()
            .command.value
            == "HOLD"
        )

    # Runtime status can update owned state without releasing the monitor key.
    orchestrator, bundle, item, _, _ = runtime_fixture(
        source_probe=lambda _item: True
    )
    queue = bundle.monitor_plans["redis"].control_queue

    orchestrator._process_runtime_status(
        event(
            item,
            EvaluationResultType.EVALUATION_ERROR,
            runtime_status=runtime_status(item),
        ),
        release=False,
    )
    assert queue.empty()

    other = replace(
        item,
        rule_id=item.rule_id + 1,
        correlation_key=item.correlation_key + ":other",
    )
    orchestrator.work_items[other.correlation_key] = other
    orchestrator._source_failure_keys[item.source_id] = {
        item.correlation_key,
        other.correlation_key,
    }
    orchestrator._process_runtime_status(
        event(item, EvaluationResultType.SOURCE_RECOVERED), release=False
    )
    assert orchestrator.source_status[item.source_id]["affected_rules"] == [
        other.rule_id
    ]
    assert queue.empty()

    orchestrator._process_runtime_status(
        event(item, EvaluationResultType.SOURCE_UNAVAILABLE), release=False
    )
    assert orchestrator.source_status[item.source_id]["state"] == "SUSPENDED"
    assert queue.empty()


def test_inactive_and_uncertain_reconciliation_publication():
    """Avoid duplicate inactive writes and publish stale uncertainty once."""

    orchestrator, bundle, item, database, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item, status="INACTIVE")
    execution = orchestrator.correlation.executions[identity]
    decision = orchestrator.correlation.consume(
        event(item, EvaluationResultType.NO_MATCH)
    )
    reconciliation = Reconciliation(execution, record, set())
    reconciliation.last_decision = decision
    orchestrator.reconciliation[identity] = reconciliation

    orchestrator._complete_reconciliation(identity, reconciliation)

    assert record.status == "INACTIVE"
    assert database.values == {}
    assert bundle.monitor_plans["redis"].control_queue.get_nowait().command.value == (
        "RESUME"
    )

    # Missing evidence preserves an active record but marks its source stale.
    orchestrator, _, item, database, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item)
    execution = orchestrator.correlation.executions[identity]
    reconciliation = Reconciliation(execution, record, set())
    reconciliation.recheck_failed = True
    orchestrator.reconciliation[identity] = reconciliation

    orchestrator._complete_reconciliation(identity, reconciliation)

    assert record.stale_source is True
    assert database.values[record.redis_key]["source_stale"] == "true"


def test_active_and_inactive_fault_publication_edge_lifecycle():
    """Preserve action history and recover from stale arbitration ownership."""

    orchestrator, _, item, database, _ = runtime_fixture()
    identity, record = active_record(
        orchestrator,
        item,
        local_action_state="COMPLETED",
        local_action_details={"state": "COMPLETED", "worker_id": "worker"},
        actions_taken=({"type": "cli", "state": "SUCCESS"},),
        action_suppressed=True,
    )
    orchestrator.correlation.set_active(item.rule_id, item.component_name, True)
    decision = orchestrator.correlation.consume(
        event(item, EvaluationResultType.NO_MATCH)
    )

    orchestrator._publish_decision(decision)

    assert record.status == "INACTIVE"
    assert record.local_action_state == "COMPLETED"
    assert record.local_action_details["worker_id"] == "worker"
    assert record.actions_taken
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert identity not in orchestrator.next_active_recheck

    # Active publication tolerates stale owner state and no arbiter winner.
    orchestrator, _, item, database, _ = runtime_fixture()
    decision = orchestrator.correlation.consume(
        event(item, EvaluationResultType.MATCH)
    )
    fault_key = (item.component_name, item.symptom)
    orchestrator.published_by_key[fault_key] = 999

    orchestrator._publish_decision(decision)
    record = orchestrator.faults[(item.rule_id, item.component_name)]
    assert database.values[record.redis_key]["rule_id"] == str(item.rule_id)

    second, _, second_item, second_database, _ = runtime_fixture()
    second_decision = second.correlation.consume(
        event(second_item, EvaluationResultType.MATCH)
    )
    second.arbiter.update = lambda _decision: None
    second._publish_decision(second_decision)
    second_record = second.faults[(second_item.rule_id, second_item.component_name)]
    assert second_database.values[second_record.redis_key]["status"] == "ACTIVE"

    # An unmaterialized alternate cannot displace the cleared owner record.
    orchestrator, bundle, item, database, _ = runtime_fixture()
    identity, record = active_record(orchestrator, item)
    orchestrator.correlation.set_active(item.rule_id, item.component_name, True)
    decision = orchestrator.correlation.consume(
        event(item, EvaluationResultType.NO_MATCH)
    )
    alternate_signature = SimpleNamespace(
        metadata=SimpleNamespace(id=item.rule_id + 1),
        actions=next(iter(bundle.signatures.values())).signature.actions,
    )
    orchestrator.arbiter.update = lambda _decision: SimpleNamespace(
        signature=alternate_signature
    )

    orchestrator._publish_decision(decision)

    assert orchestrator.published_by_key[(record.component_name, record.symptom)] == (
        item.rule_id
    )
    assert database.values[record.redis_key]["status"] == "INACTIVE"
    assert identity in orchestrator.faults
