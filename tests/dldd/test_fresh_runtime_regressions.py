"""Regressions for publication gates and monitor ownership/recovery."""

from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
from queue import Queue
from typing import Callable, Dict, Tuple, Union

import pytest

from dldd.actions import ActionRunner, ActionSequenceResult
from dldd.adapters import DSEAdapter, DataSourceAdapter
from dldd.artifacts import ArtifactReference, HealthzArtifactClient
from dldd.config import DLDDConfig
from dldd.correlation import CorrelationEngine
from dldd.dse import DSEBinding, DSEExpansionResult, DSEHook, DSERegistry, DSESourceHandle
from dldd.monitor import AsyncCollectionCompletion, AsyncCollectionPool, MonitorThread
from dldd.orchestrator import PrimaryOrchestrator
from dldd.planner import build_plans
from dldd.runtime import (
    CollectedValue, DSEExpansionEvent, EvaluationResult,
    EvaluationResultType, FaultEvidenceEvent,
)
from dldd.telemetry import TelemetryPublisher
from dldd.validation import ValidationContext, load_rules
from tests.dldd_fakes import FakeStateDB, load_valid_rules_document


def runtime(document, *, hook=None, config=None):
    validation = load_rules(
        json.dumps(document),
        ValidationContext(dse_registry=DSERegistry(hook=hook)) if hook else None,
    )
    assert validation.activation_valid
    bundle = build_plans(validation.materialized_rules, "generation", dict(redis=60, file=60, common=60))
    clock, database = [0.0], FakeStateDB()
    queue: Queue[Union[FaultEvidenceEvent, DSEExpansionEvent]] = Queue()
    config = config or DLDDConfig()
    primary = PrimaryOrchestrator(
        queue, bundle.monitor_plans, bundle.work_items,
        CorrelationEngine(bundle.signatures), TelemetryPublisher(database, config),
        config, "generation", local_action_default_timeout=300,
        clock=lambda: clock[0], wall_clock=lambda: 1000 + clock[0],
    )
    return primary, bundle, database, clock


def evidence(item, kind, sequence=1, *, recheck=False):
    return FaultEvidenceEvent(
        item.rule_id, item.event_id, item.component_name, item.source_id,
        item.correlation_key, "redis", "generation", sequence, sequence,
        1000 + sequence, 1000 + sequence,
        EvaluationResult(kind, value=CollectedValue(55, 55), completed_at=1000 + sequence),
        from_recheck=recheck,
    )


class PendingActions(ActionRunner):
    def __init__(self):
        self.future: Future[ActionSequenceResult] = Future()

    def submit(self, rule_name, actions, default_timeout):
        return self.future


@pytest.mark.parametrize("pending_is_high", (False, True))
def test_only_completed_action_candidates_enter_publication_arbitration(pending_is_high):
    document = load_valid_rules_document()
    high = document["signatures"][0]["signature"]
    low = deepcopy(document["signatures"][0])
    low["signature"]["metadata"].update(id=1000002, name="LOW", severity="MINOR")
    document["signatures"].append(low)
    pending_id, direct_id = ((1000001, 1000002) if pending_is_high else (1000002, 1000001))
    direct = low["signature"] if pending_is_high else high
    direct["actions"]["repair_actions"].pop("local_actions")
    pending_rule = high if pending_is_high else low["signature"]
    pending_rule["actions"]["repair_actions"]["local_actions"]["wait_period"] = 0
    primary, bundle, database, _ = runtime(document)
    runner = PendingActions()
    primary.action_runner = runner
    reservations = []

    class Artifacts(HealthzArtifactClient):
        def request(self, metadata, logs, queries):
            reservations.append(metadata["rule_id"])
            assert primary.faults[(metadata["rule_id"], "PSU")].status != "CANDIDATE"
            return ArtifactReference("archive", 1000, "/unused")

    primary.artifact_client = Artifacts()
    items = {item.rule_id: item for item in bundle.work_items.values()}
    key = "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"
    if pending_is_high:
        primary.process_event(evidence(items[pending_id], EvaluationResultType.MATCH))
        primary.process_event(evidence(items[direct_id], EvaluationResultType.MATCH))
        assert database.values[key]["rule_id"] == str(direct_id)
        assert database.values[key]["status"] == "ACTIVE"
    else:
        primary.process_event(evidence(items[direct_id], EvaluationResultType.MATCH))
        primary.process_event(evidence(items[pending_id], EvaluationResultType.MATCH))
        primary.process_event(evidence(items[direct_id], EvaluationResultType.NO_MATCH, 2))
        assert database.values[key]["rule_id"] == str(direct_id)
        assert database.values[key]["status"] == "INACTIVE"
    assert pending_id not in reservations
    assert primary.faults[(pending_id, "PSU")].status == "CANDIDATE"
    runner.future.set_result(ActionSequenceResult("worker", "COMPLETED", 1001, 1002, ()))
    primary.tick()
    primary.process_event(evidence(items[pending_id], EvaluationResultType.MATCH, 3, recheck=True))
    assert database.values[key]["rule_id"] == str(pending_id)
    assert database.values[key]["status"] == "ACTIVE"
    assert database.values[key]["occurrences"] == ("1" if pending_is_high else "2")
    assert reservations == [direct_id, pending_id]


def test_equal_rank_candidate_keeps_first_detection_order_after_its_gate():
    document = load_valid_rules_document()
    candidate = document["signatures"][0]["signature"]
    candidate["actions"]["repair_actions"]["local_actions"]["wait_period"] = 0
    direct = deepcopy(document["signatures"][0])
    direct["signature"]["metadata"].update(id=1000002, name="DIRECT")
    direct["signature"]["actions"]["repair_actions"].pop("local_actions")
    document["signatures"].append(direct)
    primary, bundle, database, _ = runtime(document)
    runner = PendingActions()
    primary.action_runner = runner
    items = {item.rule_id: item for item in bundle.work_items.values()}
    key = "FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"
    primary.process_event(evidence(items[1000001], EvaluationResultType.MATCH, 1))
    assert key not in database.values
    primary.process_event(evidence(items[1000002], EvaluationResultType.MATCH, 2))
    assert database.values[key]["rule_id"] == "1000002"
    runner.future.set_result(ActionSequenceResult("worker", "COMPLETED", 1001, 1002, ()))
    primary.tick()
    primary.process_event(evidence(items[1000001], EvaluationResultType.MATCH, 3, recheck=True))
    assert database.values[key]["rule_id"] == "1000001"
    assert database.values[key]["origin_time"] == "1002"
    assert database.values[key]["occurrences"] == "1"


class DeferredPool(AsyncCollectionPool):
    def __init__(self):
        self.jobs: list[Tuple[
            str, Callable[[], EvaluationResult], Queue[AsyncCollectionCompletion], bool
        ]] = []

    def submit(self, token, collector, completion_queue, high_priority=False):
        self.jobs.append((token, collector, completion_queue, high_priority))
        return True

    def complete(self):
        token, collector, completions, _ = self.jobs.pop(0)
        completions.put_nowait(AsyncCollectionCompletion(token, collector()))


class SampleAdapter(DataSourceAdapter):
    kind = EvaluationResultType.MATCH

    def get_value(self, item):
        return 55

    def collect(self, item):
        return EvaluationResult(self.kind, value=CollectedValue(55, 55), completed_at=1001)


@pytest.mark.parametrize("start_normal_job", (False, True))
def test_async_recheck_waits_for_live_collection_and_coalesces_retry(start_normal_job):
    document = load_valid_rules_document()
    signature = document["signatures"][0]["signature"]
    signature["actions"]["repair_actions"].pop("local_actions")
    signature["conditions"]["events"][0]["event"]["async"] = True
    primary, bundle, database, clock = runtime(document)
    pool, adapter = DeferredPool(), SampleAdapter()
    monitor = MonitorThread(
        bundle.monitor_plans["redis"], {"redis": adapter}, primary.evidence_queue,
        async_collection_pool=pool, clock=lambda: clock[0], wall_clock=lambda: 1000 + clock[0],
    )
    monitor.run_once()
    pool.complete()
    monitor.run_once()
    primary.process_batch()
    monitor.run_once()
    if start_normal_job:
        clock[0] = 60
        monitor.run_once()
        assert len(pool.jobs) == 1 and not pool.jobs[0][3]
    primary._start_reconciliation(next(iter(bundle.signatures.values())), "test_recheck")
    monitor.run_once()
    assert len(pool.jobs) == 1
    if start_normal_job:
        assert not pool.jobs[0][3]
        pool.complete()
        monitor.run_once()
        assert len(pool.jobs) == 1 and pool.jobs[0][3]
    token = pool.jobs[0][0]
    clock[0] += primary.config.fault_evidence_ack_timeout + 1
    primary.tick()
    monitor.run_once()
    assert len(pool.jobs) == 1 and pool.jobs[0][0] == token
    adapter.kind = EvaluationResultType.NO_MATCH
    pool.complete()
    monitor.run_once()
    primary.process_batch()
    assert database.values["FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"]["status"] == "INACTIVE"
    assert not primary.reconciliation
    assert not any(item["reason"] == "discarded stale async collection result" for item in monitor.diagnostics)


def test_replacement_monitor_adopts_outstanding_async_completion():
    document = load_valid_rules_document()
    signature = document["signatures"][0]["signature"]
    signature["actions"]["repair_actions"].pop("local_actions")
    signature["conditions"]["events"][0]["event"]["async"] = True
    primary, bundle, database, clock = runtime(document)
    plan, pool = bundle.monitor_plans["redis"], DeferredPool()
    monitor = MonitorThread(plan, {"redis": SampleAdapter()}, primary.evidence_queue,
                            async_collection_pool=pool, clock=lambda: clock[0])
    monitor.run_once()
    replacement = MonitorThread(plan, monitor.adapters, primary.evidence_queue,
                                async_collection_pool=pool, clock=lambda: clock[0])
    replacement.run_once()
    assert len(pool.jobs) == 1
    pool.complete()
    replacement.run_once()
    primary.process_batch()
    assert database.values["FAULT_INFO|PSU|SYMPTOM_OVER_THRESHOLD"]["status"] == "ACTIVE"


class InventoryHook(DSEHook):
    def __init__(self):
        self.inventories: Dict[str, Union[Tuple[str, ...], Exception]] = {
            "get_one": ("SENSOR0",), "get_two": ("SENSOR0",)
        }

    def resolve_source(self, reference, context):
        def expand(_context):
            inventory = self.inventories[reference.function]
            if isinstance(inventory, Exception):
                raise inventory
            return DSEExpansionResult(tuple(DSEBinding(instance, "source") for instance in inventory))
        return DSESourceHandle(reference, expand, lambda _context: 0)

    def resolve_evaluation(self, reference, context):
        raise AssertionError("rule uses a direct comparison")


def dynamic_document(*, shared=False):
    document = load_valid_rules_document()
    signature = document["signatures"][0]["signature"]
    signature["actions"]["repair_actions"].pop("local_actions")
    first = signature["conditions"]["events"][0]["event"]
    common = deepcopy(first)
    first.update(type="dse", path="{sensor*}:{get_one()}")
    if shared:
        second = deepcopy(first)
        second.update(id=2, path="{sensor*}:{get_two()}")
        common["id"] = 3
        signature["conditions"].update(logic="(1 OR 2) AND 3")
        signature["conditions"]["events"].extend(({"event": second}, {"event": common}))
    return document


@pytest.mark.parametrize("failure", (RuntimeError("inventory unavailable"), TimeoutError()))
def test_discovery_failures_and_recovery_reach_process_status_without_children(failure):
    hook = InventoryHook()
    hook.inventories["get_one"] = ()
    primary, bundle, database, clock = runtime(dynamic_document(), hook=hook)
    monitor = MonitorThread(bundle.monitor_plans["common"], {"dse": DSEAdapter()},
                            primary.evidence_queue, clock=lambda: clock[0])
    monitor.run_once()
    primary.process_batch()
    hook.inventories["get_one"] = failure
    for now in (5, 10, 15):
        clock[0] = now
        monitor.run_once()
        primary.process_batch()
    assert not primary.work_items
    assert primary.service_state() == "DEGRADED"
    assert len(primary.broken_rules) == 1
    diagnostic = next(iter(primary.broken_rules.values()))
    assert diagnostic["failure_count"] == 3
    primary.telemetry.publish_status(primary.service_state(), "0.0.1", "rules", "generation",
                                     broken_rules=primary.broken_rules.values())
    assert database.values["DLDD_STATUS|process_state"]["rule_exception_count"] == "1"
    hook.inventories["get_one"] = ()
    clock[0] = 20
    monitor.run_once()
    primary.process_batch()
    assert primary.service_state() == "OK" and not primary.broken_rules
    assert not primary.work_items


def test_replacement_monitor_retains_other_templates_shared_predicate():
    hook = InventoryHook()
    primary, bundle, _, clock = runtime(dynamic_document(shared=True), hook=hook)
    plan = bundle.monitor_plans["common"]
    monitor = MonitorThread(plan, {"dse": DSEAdapter(), "redis": SampleAdapter()},
                            primary.evidence_queue, clock=lambda: clock[0])
    monitor.run_once()
    primary.process_batch()
    monitor.drain_control_queue()
    common_key = next(key for key, item in primary.work_items.items() if item.event_id == 3)
    replacement = MonitorThread(plan, monitor.adapters, primary.evidence_queue, clock=lambda: clock[0])
    hook.inventories["get_one"] = ()
    templates = {template.source_handle.reference.function: key for key, template in plan.templates_by_key.items()}
    plan.expansion_state_by_key[templates["get_one"]].next_expansion_due = 0
    plan.expansion_state_by_key[templates["get_two"]].next_expansion_due = 300
    replacement.run_once()
    primary.process_batch()
    assert common_key in plan.item_snapshot()
    assert common_key in primary.work_items
    assert primary.correlation.executions[(1000001, "SENSOR0")].event_keys[3] == (common_key,)
