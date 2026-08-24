from __future__ import absolute_import

from dataclasses import replace
from copy import deepcopy
import json
from queue import Empty, Queue
from threading import Event as ThreadEvent, Thread
import time
from types import SimpleNamespace

import pytest

from dldd.adapters import (
    DSEAdapter,
    adapter_map,
)
from dldd.correlation import CorrelationEngine
from dldd.dse import (
    DSEBinding,
    DSEEvaluationHandle,
    DSEExpansionPolicy,
    DSEExpansionResult,
    DSEHook,
    DSERegistry,
    DSESourceHandle,
    ResolvedEvaluation,
    parse_reference,
)
from dldd.evaluators import EvaluationContractError, evaluate
from dldd.monitor import (
    AsyncCollectionCompletion,
    AsyncCollectionPool,
    MonitorThread,
    command_for_plan,
)
from dldd.models import ValueConfig
from dldd.planner import build_plans, work_items_for_dse_expansion
from dldd.runtime import (
    CollectedValue,
    DSEExpansionEvent,
    EvaluationResult,
    EvaluationResultType,
    MonitorCommandType,
    MonitorControlCommand,
    MonitorExecutionPlan,
    MonitorWorkItem,
    MonitorWorkState,
    MonitorWorkStateRecord,
    SourceAvailability,
    ValueConfig as RuntimeValueConfig,
)
from dldd.validation import ValidationContext, load_rules, validate_document


class SequenceAdapter(object):
    def __init__(self, results):
        self.results = list(results)

    def collect(self, _item):
        return self.results.pop(0)


def result(kind, value=None):
    collected = None if value is None else CollectedValue(value, value)
    return EvaluationResult(
        kind,
        value=collected,
        evaluator_type="boolean",
        expected=True,
        completed_at=100.0,
    )


def work_item(key="1:1:PSU0:SYMPTOM_OVER_THRESHOLD:source"):
    return MonitorWorkItem(
        rule_id=1000001,
        rule_name="PSU_FAULT",
        rule_version="1.0.0",
        schema_version="0.0.1",
        severity="CRITICAL",
        priority=1,
        symptom="SYMPTOM_OVER_THRESHOLD",
        error_type="POWER",
        component_type="PSU",
        component_name="PSU0",
        event_id=1,
        correlation_key=key,
        source_id="test:PSU0",
        source_type="test",
        source={},
        evaluation={"type": "boolean", "value": True},
    )


def plan(item):
    return MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {item.correlation_key: item},
        {item.correlation_key: MonitorWorkStateRecord()},
        Queue(),
    )


def test_execution_plan_runtime_contract():
    """Cover dynamic snapshots, interval validation, and state initialization."""

    execution_plan = plan(work_item())
    dynamic = replace(
        work_item(),
        correlation_key="1:1:PSU1:SYMPTOM_OVER_THRESHOLD:dynamic",
        component_name="PSU1",
        source_id="test:PSU1",
    )

    execution_plan.add_expanded_item(dynamic)
    items, states = execution_plan.runtime_snapshot()

    assert set(items) == set(states)
    assert items[dynamic.correlation_key] is dynamic
    execution_plan.remove_expanded_item(dynamic.correlation_key)
    current_items, current_states = execution_plan.runtime_snapshot()
    assert dynamic.correlation_key not in current_items
    assert dynamic.correlation_key not in current_states
    # Returned dictionaries are stable snapshots, not live plan mappings.
    assert dynamic.correlation_key in items
    assert dynamic.correlation_key in states

    for interval in (0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="sampling_interval"):
            replace(work_item(), sampling_interval=interval)
        with pytest.raises(ValueError, match="polling interval"):
            MonitorExecutionPlan(
                "common", "common", interval, "generation", {}, {}, Queue()
            )

    # Static plans synthesize a missing state record.
    item = work_item()
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "generation",
        {item.correlation_key: item},
        {},
        Queue(),
    )

    assert isinstance(
        execution_plan.state_by_key[item.correlation_key],
        MonitorWorkStateRecord,
    )

    with pytest.raises(TypeError):
        execution_plan.items_by_key["new"] = item
    with pytest.raises(TypeError):
        item.source["new"] = "value"

    source = {"binding": {"path": ["rails", {"field": "voltage"}]}}
    evaluation = {
        "type": "comparison",
        "operator": ">",
        "value": 50,
        "value_configs": {"metadata": ["vendor", {"unit": "volts"}]},
    }
    item = replace(work_item(), source=source, evaluation=evaluation)

    source["binding"]["path"][1]["field"] = "current"
    evaluation["value_configs"]["metadata"][1]["unit"] = "amps"
    assert item.source["binding"]["path"][1]["field"] == "voltage"
    assert item.evaluation["value_configs"]["metadata"][1]["unit"] == "volts"
    assert item.source["binding"]["path"][:1] == ("rails",)

    with pytest.raises(TypeError):
        item.source["binding"]["path"][1]["field"] = "power"
    with pytest.raises(TypeError):
        item.evaluation["value_configs"]["metadata"][0] = "platform"


def test_value_and_evaluation_contracts():
    """Exercise canonical values and every evaluator, including regex limits."""

    assert evaluate({"type": "mask", "logic": "&", "value": "0b1000"}, "0b1100")
    assert evaluate({"type": "comparison", "operator": ">", "value": 3}, "4")
    assert evaluate(
        {
            "type": "string",
            "operator": "contains",
            "value": "SU",
            "case_sensitive": False,
        },
        "psu",
    )
    assert evaluate({"type": "boolean", "value": True}, "true")
    with pytest.raises(EvaluationContractError):
        evaluate({"type": "comparison", "operator": "bad", "value": 3}, 4)

    assert RuntimeValueConfig is ValueConfig
    config = ValueConfig.from_mapping(
        {"type": "float", "unit": "volts", "scaling": 2, "encoding": "N/A"}
    )
    assert config.as_payload() == {
        "type": "float",
        "unit": "volts",
        "scaling": 2,
        "encoding": "N/A",
    }
    with pytest.raises(ValueError, match="unknown value config fields"):
        ValueConfig.from_mapping({"type": "float", "extra": True})
    with pytest.raises(ValueError, match="canonical value"):
        ValueConfig(type=[])
    for scaling in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="scaling must be numeric"):
            ValueConfig(scaling=scaling)

    started = time.monotonic()

    with pytest.raises(EvaluationContractError, match="exceeded"):
        evaluate(
            {"type": "string", "operator": "regex", "value": "(a+)+$"},
            "a" * 10000 + "!",
        )

    assert time.monotonic() - started < 1.0


def test_monitor_evidence_ownership_and_stale_acknowledgement():
    """Exercise match/clear single-flight and stale primary acknowledgement."""

    item = work_item()
    evidence = Queue()
    adapter = SequenceAdapter(
        [
            result(EvaluationResultType.NO_MATCH, False),
            result(EvaluationResultType.MATCH, True),
            result(EvaluationResultType.NO_MATCH, False),
        ]
    )
    monitor = MonitorThread(plan(item), {"test": adapter}, evidence)

    monitor.poll_once()
    with pytest.raises(Empty):
        evidence.get_nowait()
    monitor.poll_once()
    matched = evidence.get_nowait()
    state = monitor.plan.state_by_key[item.correlation_key]
    assert state.state == MonitorWorkState.IN_FLIGHT

    monitor.poll_once()
    assert len(adapter.results) == 1
    monitor.plan.control_queue.put(
        command_for_plan(
            monitor.plan,
            matched.correlation_key,
            MonitorCommandType.RESUME,
            MonitorWorkState.READY,
            "processed",
            evidence=matched,
        )
    )
    monitor.drain_control_queue()
    monitor.poll_once()
    cleared = evidence.get_nowait()
    assert cleared.result.result == EvaluationResultType.NO_MATCH

    # A command for an earlier state generation cannot release newer evidence.
    item = work_item()
    evidence = Queue()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([result(EvaluationResultType.MATCH, True)])},
        evidence,
    )
    monitor.poll_once()
    event = evidence.get_nowait()
    command = command_for_plan(
        monitor.plan,
        event.correlation_key,
        MonitorCommandType.RESUME,
        MonitorWorkState.READY,
        "processed",
        evidence=event,
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.work_state_generation += 1
    assert monitor.apply_command(command) is False
    assert state.state == MonitorWorkState.IN_FLIGHT


def test_monitor_construction_and_run_lifecycle(caplog):
    """Validate construction recovery, async requirements, stop, and run errors."""

    async_item = replace(work_item(), async_collection=True)
    with pytest.raises(ValueError, match="requires a shared collection pool"):
        MonitorThread(
            plan(async_item),
            {"test": SequenceAdapter([])},
            Queue(),
        )

    execution_plan = plan(work_item())
    state = execution_plan.state_by_key[next(iter(execution_plan.state_by_key))]
    state.state = MonitorWorkState.COLLECTING
    state.next_sample_due = 100
    monitor = MonitorThread(
        execution_plan, {"test": SequenceAdapter([])}, Queue()
    )
    assert state.state == MonitorWorkState.READY
    assert state.next_sample_due is None
    monitor.stop()
    assert monitor.stop_event.is_set()

    # The run loop contains an unexpected cycle exception and exits cleanly.
    stop_event = ThreadEvent()
    monitor = MonitorThread(
        plan(work_item()),
        {"test": SequenceAdapter([])},
        Queue(),
        stop_event=stop_event,
    )

    def fail_cycle():
        stop_event.set()
        raise RuntimeError("cycle failed")

    monitor.run_once = fail_cycle
    monitor.run()
    assert "unhandled monitor cycle error" in caplog.text


def test_monitor_command_state_transitions():
    """Apply invalid, normalized, held, recheck, suspend, and plan commands."""

    item = work_item()
    monitor = MonitorThread(
        plan(item), {"test": SequenceAdapter([])}, Queue()
    )
    base = MonitorControlCommand(
        "command",
        "common",
        "sha256:test",
        item.correlation_key,
        MonitorCommandType.RESUME,
        MonitorWorkState.READY,
        "test",
    )

    assert not monitor.apply_command(replace(base, monitor_id="redis"))
    assert not monitor.apply_command(
        replace(base, plan_generation="stale-generation")
    )
    assert not monitor.apply_command(
        replace(base, correlation_key="unknown-key")
    )
    assert not monitor.apply_command(replace(base, command="UNKNOWN"))

    # Resume and suspend normalize target states owned by the primary thread.
    item = work_item()
    monitor = MonitorThread(
        plan(item), {"test": SequenceAdapter([])}, Queue()
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    base = MonitorControlCommand(
        "command",
        "common",
        "sha256:test",
        item.correlation_key,
        MonitorCommandType.RESUME,
        MonitorWorkState.HELD_BY_PRIMARY,
        "test",
    )

    assert monitor.apply_command(base)
    assert state.state == MonitorWorkState.READY
    assert monitor.apply_command(
        replace(
            base,
            command=MonitorCommandType.SUSPEND,
            target_state=MonitorWorkState.READY,
        )
    )
    assert state.state == MonitorWorkState.SUSPENDED

    # Normal command progression owns and then permanently suspends the key.
    item = work_item()
    monitor = MonitorThread(
        plan(item), {"test": SequenceAdapter([])}, Queue()
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    base = MonitorControlCommand(
        "command",
        "common",
        "sha256:test",
        item.correlation_key,
        MonitorCommandType.HOLD,
        MonitorWorkState.HELD_BY_PRIMARY,
        "test",
        hold_deadline=20,
    )

    state.ack_deadline = 10
    assert monitor.apply_command(base)
    assert state.state == MonitorWorkState.HELD_BY_PRIMARY
    assert state.ack_deadline is None
    assert state.hold_deadline == 20

    assert monitor.apply_command(
        replace(
            base,
            command=MonitorCommandType.RECHECK_ONCE,
            target_state=MonitorWorkState.RECHECK_REQUESTED,
            recheck_not_before=15,
        )
    )
    assert state.state == MonitorWorkState.RECHECK_REQUESTED
    assert state.recheck_not_before == 15

    assert monitor.apply_command(
        replace(
            base,
            command=MonitorCommandType.SUSPEND,
            target_state=MonitorWorkState.BROKEN,
        )
    )
    assert state.state == MonitorWorkState.BROKEN
    assert state.hold_deadline is None
    assert state.recheck_not_before is None

    # Bootstrap commands are plan-owned and carry no evidence generation.
    item = work_item()
    execution_plan = plan(item)

    command = command_for_plan(
        execution_plan,
        item.correlation_key,
        MonitorCommandType.RECHECK_ONCE,
        MonitorWorkState.RECHECK_REQUESTED,
        "bootstrap",
        hold_deadline=30,
    )

    assert command.monitor_id == execution_plan.monitor_id
    assert command.expected_work_state_generation is None
    assert command.evidence_sequence is None
    assert command.hold_deadline == 30


def test_monitor_result_publication_recovery_and_backpressure_lifecycle():
    item = work_item()
    evidence = Queue()
    unavailable = EvaluationResult(
        EvaluationResultType.SOURCE_UNAVAILABLE,
        completed_at=100.0,
        error="missing",
    )
    adapter = SequenceAdapter(
        [
            unavailable,
            result(EvaluationResultType.MATCH, True),
            result(EvaluationResultType.MATCH, True),
        ]
    )
    monitor = MonitorThread(
        plan(item),
        {"test": adapter},
        evidence,
        source_recovery_samples=2,
    )
    monitor.poll_once()
    unavailable_event = evidence.get_nowait()
    monitor.apply_command(
        command_for_plan(
            monitor.plan,
            unavailable_event.correlation_key,
            MonitorCommandType.RESUME,
            MonitorWorkState.DEGRADED,
            "retry",
            evidence=unavailable_event,
        )
    )
    monitor.poll_once()
    with pytest.raises(Empty):
        evidence.get_nowait()
    monitor.poll_once()
    assert evidence.get_nowait().result.result == EvaluationResultType.SOURCE_RECOVERED


    # A full evidence queue cannot discard a clear transition.
    item = work_item()
    state = MonitorWorkStateRecord(
        state=MonitorWorkState.READY,
        last_sample_state=EvaluationResultType.MATCH.value,
    )
    execution_plan = plan(item)
    execution_plan.state_by_key[item.correlation_key] = state
    evidence = Queue(maxsize=1)
    evidence.put(object())
    monitor = MonitorThread(
        execution_plan,
        {
            "test": SequenceAdapter(
                [
                    result(EvaluationResultType.NO_MATCH, False),
                    result(EvaluationResultType.NO_MATCH, False),
                ]
            )
        },
        evidence,
    )

    monitor.poll_once()
    assert state.last_sample_state == EvaluationResultType.MATCH.value
    assert state.state == MonitorWorkState.READY
    evidence.get_nowait()
    monitor.poll_once()

    assert evidence.get_nowait().result.result == EvaluationResultType.NO_MATCH


    # The same backpressure rule applies to source recovery.
    item = work_item()
    state = MonitorWorkStateRecord(
        state=MonitorWorkState.DEGRADED,
        source_status=SourceAvailability.UNAVAILABLE,
    )
    execution_plan = plan(item)
    execution_plan.state_by_key[item.correlation_key] = state
    evidence = Queue(maxsize=1)
    evidence.put(object())
    monitor = MonitorThread(
        execution_plan,
        {
            "test": SequenceAdapter(
                [
                    result(EvaluationResultType.MATCH, True),
                    result(EvaluationResultType.MATCH, True),
                ]
            )
        },
        evidence,
    )

    monitor.poll_once()
    assert state.source_status == SourceAvailability.UNAVAILABLE
    evidence.get_nowait()
    monitor.poll_once()

    assert evidence.get_nowait().result.result == EvaluationResultType.SOURCE_RECOVERED
    assert state.source_status == SourceAvailability.AVAILABLE


    # A rejected unavailable event cannot fabricate a later recovery.
    item = work_item()
    execution_plan = plan(item)
    state = execution_plan.state_by_key[item.correlation_key]
    evidence = Queue(maxsize=1)
    evidence.put(object())
    unavailable = EvaluationResult(
        EvaluationResultType.SOURCE_UNAVAILABLE,
        completed_at=100.0,
        error="missing",
    )
    monitor = MonitorThread(
        execution_plan,
        {
            "test": SequenceAdapter(
                [unavailable, result(EvaluationResultType.MATCH, True)]
            )
        },
        evidence,
    )

    monitor.poll_once()
    assert state.source_status == SourceAvailability.AVAILABLE
    evidence.get_nowait()
    monitor.poll_once()

    assert evidence.get_nowait().result.result == EvaluationResultType.MATCH


    # Evaluation failures break the rule without misclassifying transport.
    item = work_item()
    evaluation_error = EvaluationResult(
        EvaluationResultType.EVALUATION_ERROR,
        completed_at=100.0,
        error_category="EVALUATION_ERROR",
        error="comparator failed",
        retryable=False,
    )
    evidence = Queue()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([evaluation_error])},
        evidence,
    )

    monitor.poll_once()

    state = monitor.plan.state_by_key[item.correlation_key]
    event = evidence.get_nowait()
    assert state.source_status == SourceAvailability.AVAILABLE
    assert state.consecutive_failure_count == 1
    assert event.runtime_status.state == "BROKEN"


def test_monitor_scheduler_due_key_and_per_item_cadence_lifecycle():
    first = work_item("first")
    second = replace(work_item("second"), event_id=2)
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {first.correlation_key: first, second.correlation_key: second},
        {
            first.correlation_key: MonitorWorkStateRecord(
                state=MonitorWorkState.RECHECK_REQUESTED
            ),
            second.correlation_key: MonitorWorkStateRecord(),
        },
        Queue(),
    )
    calls = []

    class RecordingAdapter(object):
        def collect(self, item):
            calls.append(item.correlation_key)
            return result(EvaluationResultType.NO_MATCH, False)

    monitor = MonitorThread(
        execution_plan,
        {"test": RecordingAdapter()},
        Queue(),
        clock=lambda: 0.0,
    )
    monitor._next_poll = 60.0

    monitor.run_once()

    assert calls == ["first"]


    # Per-key intervals start due and coalesce missed cycles.
    clock = [0.0]
    fast = replace(
        work_item("fast"),
        sampling_interval=10,
        sampling_interval_is_explicit=True,
    )
    slow = replace(
        work_item("slow"),
        event_id=2,
        sampling_interval=30,
        sampling_interval_is_explicit=True,
    )
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {fast.correlation_key: fast, slow.correlation_key: slow},
        {
            fast.correlation_key: MonitorWorkStateRecord(),
            slow.correlation_key: MonitorWorkStateRecord(),
        },
        Queue(),
    )
    calls = []

    class RecordingAdapter(object):
        def collect(self, item):
            calls.append((clock[0], item.correlation_key))
            return result(EvaluationResultType.NO_MATCH, False)

    monitor = MonitorThread(
        execution_plan,
        {"test": RecordingAdapter()},
        Queue(),
        clock=lambda: clock[0],
        wall_clock=lambda: 1000.0 + clock[0],
    )

    monitor.run_once()
    assert calls == [(0.0, "fast"), (0.0, "slow")]
    assert execution_plan.state_by_key["fast"].next_sample_due == 10.0
    assert execution_plan.state_by_key["slow"].next_sample_due == 30.0
    assert execution_plan.state_by_key["fast"].last_attempt_timestamp == 1000.0
    assert execution_plan.state_by_key["slow"].last_attempt_timestamp == 1000.0

    clock[0] = 9.0
    monitor.run_once()
    assert len(calls) == 2

    clock[0] = 10.0
    monitor.run_once()
    assert calls[-1] == (10.0, "fast")

    # A late cycle collects each due key once and schedules from the actual
    # attempt time instead of replaying every missed interval.
    clock[0] = 100.0
    monitor.run_once()
    assert calls[-2:] == [(100.0, "fast"), (100.0, "slow")]
    assert execution_plan.state_by_key["fast"].next_sample_due == 110.0
    assert execution_plan.state_by_key["slow"].next_sample_due == 130.0


    # Every key schedules from its own actual collection attempt.
    clock = [0.0]
    first = replace(
        work_item("first"),
        sampling_interval=10,
        sampling_interval_is_explicit=True,
    )
    second = replace(
        work_item("second"),
        event_id=2,
        sampling_interval=10,
        sampling_interval_is_explicit=True,
    )
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {"first": first, "second": second},
        {
            "first": MonitorWorkStateRecord(),
            "second": MonitorWorkStateRecord(),
        },
        Queue(),
    )

    class SlowFirstAdapter(object):
        def collect(self, item):
            if item.correlation_key == "first":
                clock[0] += 7.0
            return result(EvaluationResultType.NO_MATCH, False)

    monitor = MonitorThread(
        execution_plan,
        {"test": SlowFirstAdapter()},
        Queue(),
        clock=lambda: clock[0],
    )

    monitor.run_once()

    assert execution_plan.state_by_key["first"].next_sample_due == 10.0
    assert execution_plan.state_by_key["second"].next_sample_due == 17.0


def test_async_monitor_does_not_block_inline_or_duplicate_collection():
    release = ThreadEvent()
    started = ThreadEvent()
    completed = ThreadEvent()
    calls = []
    async_item = replace(
        work_item("async"),
        async_collection=True,
    )
    inline_item = replace(
        work_item("inline"),
        event_id=2,
    )
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {"async": async_item, "inline": inline_item},
        {
            "async": MonitorWorkStateRecord(),
            "inline": MonitorWorkStateRecord(),
        },
        Queue(),
    )

    class BlockingAdapter(object):
        def collect(self, item):
            calls.append(item.correlation_key)
            if item.async_collection:
                started.set()
                release.wait(2)
                completed.set()
            return result(EvaluationResultType.NO_MATCH, False)

    pool = AsyncCollectionPool(
        max_workers=1,
        max_pending=1,
        recheck_reserve=0,
    )
    monitor = MonitorThread(
        execution_plan,
        {"test": BlockingAdapter()},
        Queue(),
        async_collection_pool=pool,
    )
    try:
        before = time.monotonic()
        monitor.poll_once()
        elapsed = time.monotonic() - before

        assert elapsed < 0.5
        assert started.wait(1)
        assert "inline" in calls
        assert calls.count("async") == 1
        assert execution_plan.state_by_key["async"].state == (
            MonitorWorkState.COLLECTING
        )

        monitor.poll_once()
        assert calls.count("async") == 1

        release.set()
        assert completed.wait(1)
        deadline = time.monotonic() + 1
        while (
            execution_plan.state_by_key["async"].state
            == MonitorWorkState.COLLECTING
            and time.monotonic() < deadline
        ):
            monitor.drain_async_completions()
            time.sleep(0.01)
        assert execution_plan.state_by_key["async"].state == (
            MonitorWorkState.READY
        )
    finally:
        release.set()
        pool.shutdown()


def test_async_collection_pool_capacity_failure_and_shutdown_lifecycle():
    # Exercise pool limits, priority reserve, errors, and both shutdown modes.

    release = ThreadEvent()
    started = ThreadEvent()
    completions = Queue()
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)

    def collect():
        started.set()
        release.wait(2)
        return result(EvaluationResultType.NO_MATCH, False)

    try:
        assert pool.submit("first", collect, completions)
        assert started.wait(1)
        assert not pool.submit("second", collect, completions)
    finally:
        release.set()
        pool.shutdown()

    # A closed pool consistently rejects new work and repeated shutdown.
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)
    pool.shutdown(wait=True)

    assert not pool.submit(
        "closed",
        lambda: result(EvaluationResultType.NO_MATCH, False),
        Queue(),
    )
    pool.shutdown(wait=False)

    # Priority work may use its reserve but cannot exceed total capacity.
    release = ThreadEvent()
    started = ThreadEvent()
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)

    def block():
        started.set()
        release.wait(1)
        return result(EvaluationResultType.NO_MATCH, False)

    try:
        assert pool.submit("active", block, Queue())
        assert started.wait(1)
        assert not pool.submit(
            "priority",
            lambda: result(EvaluationResultType.NO_MATCH, False),
            Queue(),
            high_priority=True,
        )
    finally:
        release.set()
        pool.shutdown(wait=True)

    # Failed total-capacity acquisition releases the normal admission slot.
    release = ThreadEvent()
    started = ThreadEvent()
    pool = AsyncCollectionPool(
        max_workers=1, max_pending=1, recheck_reserve=1
    )

    def block():
        started.set()
        release.wait(1)
        return result(EvaluationResultType.NO_MATCH, False)

    try:
        assert pool.submit("priority-active", block, Queue(), high_priority=True)
        assert started.wait(1)
        assert pool.submit(
            "priority-queued",
            lambda: result(EvaluationResultType.NO_MATCH, False),
            Queue(),
            high_priority=True,
        )
        assert not pool.submit(
            "normal",
            lambda: result(EvaluationResultType.NO_MATCH, False),
            Queue(),
        )
        # The failed total-capacity acquisition must release the normal slot.
        assert pool._normal_slots.acquire(False)
        pool._normal_slots.release()
    finally:
        release.set()
        pool.shutdown(wait=True)

    # Worker exceptions become ordinary collection-error completions.
    completions = Queue()
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)
    try:
        assert pool.submit(
            "failure",
            lambda: (_ for _ in ()).throw(RuntimeError("collector failed")),
            completions,
        )
        completion = completions.get(timeout=1)
        assert completion.token == "failure"
        assert completion.result.result == EvaluationResultType.COLLECTION_ERROR
        assert completion.result.error == "collector failed"
        completions.task_done()
    finally:
        pool.shutdown(wait=True)

    # Nonblocking shutdown completes active work and discards queued work.
    release = ThreadEvent()
    started = ThreadEvent()
    completions = Queue()
    pool = AsyncCollectionPool(
        max_workers=1, max_pending=1, recheck_reserve=0
    )

    def block():
        started.set()
        release.wait(1)
        return result(EvaluationResultType.NO_MATCH, False)

    assert pool.submit("active", block, completions)
    assert started.wait(1)
    assert pool.submit(
        "queued",
        lambda: result(EvaluationResultType.MATCH, True),
        completions,
    )
    pool.shutdown(wait=False)
    release.set()
    for worker in pool._workers:
        worker.join(timeout=1)

    completed = completions.get_nowait()
    completions.task_done()
    assert completed.token == "active"
    assert completions.empty()


def test_async_collection_pool_reports_lifetime_timing_and_usage():
    class ManualClock(object):
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            return self.value

    clock = ManualClock()
    first_started = ThreadEvent()
    first_release = ThreadEvent()
    second_started = ThreadEvent()
    second_release = ThreadEvent()
    completions = Queue()
    pool = AsyncCollectionPool(
        max_workers=1,
        max_pending=1,
        recheck_reserve=0,
        monotonic_clock=clock,
    )

    def first():
        first_started.set()
        first_release.wait(1)
        return result(EvaluationResultType.NO_MATCH, False)

    def second():
        second_started.set()
        second_release.wait(1)
        return result(EvaluationResultType.NO_MATCH, False)

    try:
        assert pool.metrics() == {
            "async_pool_workers": 1,
            "async_pool_busy": 0,
            "async_pool_queued": 0,
            "async_pool_avg_queue_latency_ms": 0.0,
            "async_pool_avg_execution_time_ms": 0.0,
            "async_pool_avg_utilization_percent": 0.0,
        }
        assert pool.submit("first", first, completions)
        assert first_started.wait(1)

        clock.value = 1.0
        assert pool.submit("second", second, completions)
        metrics = pool.metrics()
        assert metrics["async_pool_busy"] == 1
        assert metrics["async_pool_queued"] == 1
        assert metrics["async_pool_avg_utilization_percent"] == 100.0

        clock.value = 2.0
        first_release.set()
        assert second_started.wait(1)
        assert pool.metrics()["async_pool_avg_queue_latency_ms"] == 500.0

        clock.value = 4.0
        second_release.set()
        assert completions.get(timeout=1).token == "first"
        assert completions.get(timeout=1).token == "second"
        deadline = time.monotonic() + 1
        while pool.metrics()["async_pool_busy"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.metrics() == {
            "async_pool_workers": 1,
            "async_pool_busy": 0,
            "async_pool_queued": 0,
            "async_pool_avg_queue_latency_ms": 500.0,
            "async_pool_avg_execution_time_ms": 2000.0,
            "async_pool_avg_utilization_percent": 100.0,
        }
    finally:
        first_release.set()
        second_release.set()
        pool.shutdown()


def test_async_collection_pool_serializes_submit_with_shutdown():
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)
    original_slots = pool._normal_slots
    submit_inside_lock = ThreadEvent()
    allow_submit = ThreadEvent()
    shutdown_completed = ThreadEvent()
    accepted = []

    class BlockingSlots(object):
        def acquire(self, blocking=True):
            submit_inside_lock.set()
            allow_submit.wait(1)
            return original_slots.acquire(blocking)

        def release(self):
            original_slots.release()

    pool._normal_slots = BlockingSlots()
    submitter = Thread(
        target=lambda: accepted.append(
            pool.submit(
                "racing",
                lambda: result(EvaluationResultType.NO_MATCH, False),
                Queue(),
            )
        )
    )
    shutdown_thread = Thread(
        target=lambda: (
            pool.shutdown(wait=False),
            shutdown_completed.set(),
        )
    )
    try:
        submitter.start()
        assert submit_inside_lock.wait(1)
        shutdown_thread.start()
        assert not shutdown_completed.wait(0.05)

        allow_submit.set()
        submitter.join(1)
        shutdown_thread.join(1)
        assert accepted == [True]
        assert shutdown_completed.is_set()
        assert not pool.submit(
            "after-shutdown",
            lambda: result(EvaluationResultType.NO_MATCH, False),
            Queue(),
        )
    finally:
        allow_submit.set()
        submitter.join(1)
        shutdown_thread.join(1)
        pool.shutdown(wait=True)


def test_async_monitor_admission_priority_and_cadence_lifecycle():
    """Capacity failures stay due and admitted work remains single-flight."""

    release = ThreadEvent()
    started = ThreadEvent()
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)

    def occupy_pool():
        started.set()
        release.wait(2)
        return result(EvaluationResultType.NO_MATCH, False)

    assert pool.submit("occupy", occupy_pool, Queue())
    assert started.wait(1)
    item = replace(work_item("waiting"), async_collection=True)
    adapter = SequenceAdapter([result(EvaluationResultType.NO_MATCH, False)])
    monitor = MonitorThread(
        plan(item),
        {"test": adapter},
        Queue(),
        async_collection_pool=pool,
    )
    try:
        monitor.poll_once()

        state = monitor.plan.state_by_key["waiting"]
        assert state.state == MonitorWorkState.READY
        assert state.last_attempt_timestamp is None
        assert state.next_sample_due is None
        assert state.consecutive_failure_count == 0
        assert len(adapter.results) == 1
        assert monitor.diagnostics[-1]["reason"] == (
            "async collection capacity is exhausted"
        )
    finally:
        release.set()
        pool.shutdown()

    # Once queued, repeated cadence cycles cannot submit the same key again.
    release_worker = ThreadEvent()
    worker_occupied = ThreadEvent()
    async_collected = ThreadEvent()
    calls = []
    pool = AsyncCollectionPool(
        max_workers=1,
        max_pending=1,
        recheck_reserve=0,
    )

    def occupy_worker():
        worker_occupied.set()
        release_worker.wait(2)
        return result(EvaluationResultType.NO_MATCH, False)

    assert pool.submit("occupy", occupy_worker, Queue())
    assert worker_occupied.wait(1)
    item = replace(
        work_item("queued"),
        sampling_interval=1,
        sampling_interval_is_explicit=True,
        async_collection=True,
    )

    class RecordingAdapter(object):
        def collect(self, unused_item):
            calls.append("queued")
            async_collected.set()
            return result(EvaluationResultType.NO_MATCH, False)

    monitor = MonitorThread(
        plan(item),
        {"test": RecordingAdapter()},
        Queue(),
        async_collection_pool=pool,
    )
    try:
        monitor.poll_once(now=0)
        assert monitor.plan.state_by_key["queued"].state == (
            MonitorWorkState.COLLECTING
        )
        assert calls == []

        monitor.poll_once(now=10)
        assert calls == []

        release_worker.set()
        assert async_collected.wait(1)
        deadline = time.monotonic() + 1
        while (
            monitor.plan.state_by_key["queued"].state
            == MonitorWorkState.COLLECTING
            and time.monotonic() < deadline
        ):
            monitor.drain_async_completions()
            time.sleep(0.01)
        assert calls == ["queued"]
        assert monitor.plan.state_by_key["queued"].state == (
            MonitorWorkState.READY
        )
    finally:
        release_worker.set()
        pool.shutdown()


    # Priority rechecks jump the queue without moving the normal cadence.

    release_worker = ThreadEvent()
    worker_occupied = ThreadEvent()
    all_collected = ThreadEvent()
    order = []
    pool = AsyncCollectionPool(
        max_workers=1,
        max_pending=3,
        recheck_reserve=1,
    )

    def occupy_worker():
        worker_occupied.set()
        release_worker.wait(2)
        return result(EvaluationResultType.NO_MATCH, False)

    def collect(name):
        def run():
            order.append(name)
            if len(order) == 3:
                all_collected.set()
            return result(EvaluationResultType.NO_MATCH, False)

        return run

    try:
        assert pool.submit("occupy", occupy_worker, Queue())
        assert worker_occupied.wait(1)
        assert pool.submit("normal-one", collect("normal-one"), Queue())
        assert pool.submit("normal-two", collect("normal-two"), Queue())
        assert not pool.submit("normal-three", collect("normal-three"), Queue())
        assert pool.submit(
            "recheck",
            collect("recheck"),
            Queue(),
            high_priority=True,
        )

        release_worker.set()
        assert all_collected.wait(1)
        assert order == ["recheck", "normal-one", "normal-two"]
    finally:
        release_worker.set()
        pool.shutdown()

    # Monitor-side recheck submission explicitly requests high priority.
    class RecordingPool(object):
        def __init__(self):
            self.high_priority = None

        def submit(
            self,
            unused_token,
            unused_collector,
            unused_completion_queue,
            high_priority=False,
        ):
            self.high_priority = high_priority
            return False

    item = replace(work_item("recheck"), async_collection=True)
    execution_plan = plan(item)
    execution_plan.state_by_key["recheck"].state = (
        MonitorWorkState.RECHECK_REQUESTED
    )
    pool = RecordingPool()
    monitor = MonitorThread(
        execution_plan,
        {"test": SequenceAdapter([])},
        Queue(),
        async_collection_pool=pool,
    )

    monitor.poll_once(include_normal=False)

    assert pool.high_priority is True
    assert execution_plan.state_by_key["recheck"].state == (
        MonitorWorkState.RECHECK_REQUESTED
    )

    # Successful admission changes ownership but preserves the normal deadline.
    class AcceptingPool(object):
        def submit(self, *args, **kwargs):
            return True

    item = replace(work_item("recheck"), async_collection=True)
    execution_plan = plan(item)
    state = execution_plan.state_by_key[item.correlation_key]
    state.state = MonitorWorkState.RECHECK_REQUESTED
    state.next_sample_due = 100.0
    monitor = MonitorThread(
        execution_plan,
        {"test": SequenceAdapter([])},
        Queue(),
        async_collection_pool=AcceptingPool(),
    )

    monitor.poll_once(include_normal=False)

    assert state.state == MonitorWorkState.COLLECTING
    assert state.next_sample_due == 100.0


def test_async_result_publication_and_error_containment():
    """Publish normal evidence and contain stale completions and exceptions."""

    item = replace(work_item("async-match"), async_collection=True)
    evidence = Queue()
    pool = AsyncCollectionPool(max_workers=1, max_pending=0)
    monitor = MonitorThread(
        plan(item),
        {
            "test": SequenceAdapter(
                [result(EvaluationResultType.MATCH, True)]
            )
        },
        evidence,
        async_collection_pool=pool,
    )
    try:
        monitor.poll_once()
        deadline = time.monotonic() + 1
        while evidence.empty() and time.monotonic() < deadline:
            monitor.drain_async_completions()
            time.sleep(0.01)

        event = evidence.get_nowait()
        assert event.correlation_key == "async-match"
        assert event.result.result == EvaluationResultType.MATCH
        assert monitor.plan.state_by_key["async-match"].state == (
            MonitorWorkState.IN_FLIGHT
        )
    finally:
        pool.shutdown()

    # Unknown completions are harmless; stale known work is diagnosed.
    item = replace(work_item("async"), async_collection=True)

    class AcceptingPool(object):
        def submit(self, *args, **kwargs):
            return True

    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([])},
        Queue(),
        async_collection_pool=AcceptingPool(),
    )
    monitor._async_completions.put(
        AsyncCollectionCompletion(
            "unknown", result(EvaluationResultType.NO_MATCH, False)
        )
    )
    monitor.drain_async_completions()
    assert not monitor.diagnostics

    monitor.poll_once()
    token = next(iter(monitor._async_jobs))
    # Model plan replacement racing a worker completion.
    monitor.plan.state_by_key.pop(item.correlation_key)
    monitor._async_completions.put(
        AsyncCollectionCompletion(
            token, result(EvaluationResultType.NO_MATCH, False)
        )
    )
    monitor.drain_async_completions()
    assert monitor.diagnostics[-1]["reason"] == (
        "discarded stale async collection result"
    )

    # Adapter exceptions remain retryable monitor collection failures.
    class RaisingAdapter(object):
        def collect(self, item):
            raise RuntimeError("adapter failed")

    monitor = MonitorThread(
        plan(work_item()), {"test": RaisingAdapter()}, Queue()
    )
    converted = monitor._collect_result(RaisingAdapter(), work_item())

    assert converted.result == EvaluationResultType.COLLECTION_ERROR
    assert converted.error_category == "MONITOR_ERROR"
    assert converted.error == "adapter failed"
    assert converted.retryable


def test_dse_cycle_tracking_ignores_unowned_and_nonwarmup_children():
    item = work_item()
    monitor = MonitorThread(
        plan(item), {"test": SequenceAdapter([])}, Queue()
    )
    monitor._mark_dse_cycle_attempt(item.correlation_key)

    monitor._dse_templates_by_child[item.correlation_key] = {"template"}
    monitor.plan.expansion_state_by_key["template"] = SimpleNamespace(
        phase="STABLE",
        pending_cycle_keys={item.correlation_key},
        last_complete_cycle_timestamp=None,
        next_expansion_due=None,
    )
    monitor._mark_dse_cycle_attempt(item.correlation_key)
    assert monitor.plan.expansion_state_by_key[
        "template"
    ].pending_cycle_keys == {item.correlation_key}

    monitor.plan.expansion_state_by_key["template"].phase = "WARMUP"
    monitor.plan.expansion_state_by_key["template"].pending_cycle_keys = {
        item.correlation_key,
        "another-child",
    }
    monitor._mark_dse_cycle_attempt(item.correlation_key)
    state = monitor.plan.expansion_state_by_key["template"]
    assert state.pending_cycle_keys == {"another-child"}
    assert state.next_expansion_due is None


def test_planner_sampling_defaults_and_recheck_cadence_lifecycle():
    validated = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    original = validated.materialized_rules[0]
    original_event = original.events[0]
    explicit_event = replace(
        original_event.event,
        sampling_interval=17,
        async_collection=True,
    )
    explicit_rule = replace(
        original,
        events=(replace(original_event, event=explicit_event),),
    )

    explicit_bundle = build_plans(
        (explicit_rule,),
        "sha256:explicit",
        {"redis": 41, "file": 42, "common": 43},
    )
    explicit_item = next(iter(explicit_bundle.work_items.values()))
    assert explicit_item.sampling_interval == 17
    assert explicit_item.sampling_interval_is_explicit
    assert explicit_item.async_collection

    inherited_bundle = build_plans(
        (original,),
        "sha256:inherited",
        {"redis": 41, "file": 42, "common": 43},
    )
    inherited_item = next(iter(inherited_bundle.work_items.values()))
    assert inherited_item.sampling_interval == 41
    assert not inherited_item.sampling_interval_is_explicit
    assert not inherited_item.async_collection


    # A one-shot recheck honors deadlines and survives evidence backpressure.

    clock = [0.0]
    item = replace(
        work_item(),
        sampling_interval=100,
        sampling_interval_is_explicit=True,
    )
    evidence = Queue()
    monitor = MonitorThread(
        plan(item),
        {
            "test": SequenceAdapter(
                [
                    result(EvaluationResultType.NO_MATCH, False),
                    result(EvaluationResultType.NO_MATCH, False),
                ]
            )
        },
        evidence,
        clock=lambda: clock[0],
    )

    monitor.run_once()
    state = monitor.plan.state_by_key[item.correlation_key]
    assert state.next_sample_due == 100.0

    state.state = MonitorWorkState.RECHECK_REQUESTED
    state.recheck_not_before = 10.0
    clock[0] = 10.0
    monitor.run_once()

    recheck = evidence.get_nowait()
    assert recheck.from_recheck
    assert state.next_sample_due == 100.0

    # Before its not-before deadline, collection remains untouched.
    item = work_item()
    adapter = SequenceAdapter([result(EvaluationResultType.NO_MATCH, False)])
    monitor = MonitorThread(
        plan(item),
        {"test": adapter},
        Queue(),
        clock=lambda: 5.0,
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.state = MonitorWorkState.RECHECK_REQUESTED
    state.recheck_not_before = 10.0

    monitor.poll_once(include_normal=False)

    assert len(adapter.results) == 1
    assert state.state == MonitorWorkState.RECHECK_REQUESTED

    # A full evidence queue keeps both the recheck and normal due time intact.
    item = work_item()
    evidence = Queue(maxsize=1)
    evidence.put_nowait("occupied")
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([result(EvaluationResultType.MATCH, True)])},
        evidence,
        clock=lambda: 5.0,
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.state = MonitorWorkState.RECHECK_REQUESTED
    state.next_sample_due = 100.0

    monitor.poll_once(include_normal=False)

    assert state.state == MonitorWorkState.RECHECK_REQUESTED
    assert state.next_sample_due == 100.0


def test_runtime_polling_updates_survive_collection_and_monitor_replacement():
    """Apply interval changes across inherited, active, and replacement plans."""

    clock = [0.0]
    inherited = replace(
        work_item("inherited"),
        sampling_interval=60,
        sampling_interval_is_explicit=False,
    )
    explicit = replace(
        work_item("explicit"),
        event_id=2,
        sampling_interval=300,
        sampling_interval_is_explicit=True,
    )
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {
            inherited.correlation_key: inherited,
            explicit.correlation_key: explicit,
        },
        {
            inherited.correlation_key: MonitorWorkStateRecord(
                next_sample_due=50.0
            ),
            explicit.correlation_key: MonitorWorkStateRecord(
                next_sample_due=50.0
            ),
        },
        Queue(),
    )
    monitor = MonitorThread(
        execution_plan,
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: clock[0],
    )
    items_mapping = execution_plan.items_by_key
    inherited_identity = execution_plan.items_by_key["inherited"]
    explicit_identity = execution_plan.items_by_key["explicit"]
    assert inherited_identity is inherited
    assert explicit_identity is explicit

    monitor.update_polling_intervals(
        {"redis": 10, "file": 10, "common": 10}
    )
    assert execution_plan.polling_interval == 60
    monitor.drain_interval_update_queue()

    assert execution_plan.polling_interval == 10
    assert execution_plan.items_by_key is items_mapping
    assert execution_plan.items_by_key["inherited"] is inherited_identity
    assert execution_plan.items_by_key["explicit"] is explicit_identity
    assert execution_plan.items_by_key["inherited"].sampling_interval == 60
    assert execution_plan.items_by_key["explicit"].sampling_interval == 300
    assert execution_plan.state_by_key["inherited"].next_sample_due == 10.0
    assert execution_plan.state_by_key["explicit"].next_sample_due == 50.0

    monitor.update_polling_intervals(
        {"redis": 100, "file": 100, "common": 100}
    )
    monitor.drain_interval_update_queue()
    assert execution_plan.polling_interval == 100
    assert execution_plan.items_by_key["inherited"] is inherited_identity
    assert execution_plan.items_by_key["explicit"].sampling_interval == 300
    # Increasing a default does not postpone an already-nearer inherited due.
    assert execution_plan.state_by_key["inherited"].next_sample_due == 10.0

    # Lowering the interval pulls a never-sampled plan forward immediately.
    clock = [0.0]
    item = work_item()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: clock[0],
    )
    monitor._next_poll = 3600.0

    monitor.update_polling_intervals(
        {"redis": 1, "file": 1, "common": 1}
    )

    assert monitor.plan.polling_interval == 60
    monitor.drain_interval_update_queue()
    assert monitor.plan.polling_interval == 1
    # A never-sampled eligible key remains immediately due.
    assert monitor._next_poll == 0.0

    # An update queued during collection wins over the old post-collect due.
    clock = [0.0]
    collection_started = ThreadEvent()
    finish_collection = ThreadEvent()
    inherited = replace(
        work_item(),
        sampling_interval=86400,
        sampling_interval_is_explicit=False,
    )
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        86400,
        "sha256:test",
        {inherited.correlation_key: inherited},
        {inherited.correlation_key: MonitorWorkStateRecord()},
        Queue(),
    )

    class BlockingAdapter(object):
        def collect(self, _item):
            collection_started.set()
            assert finish_collection.wait(1.0)
            return result(EvaluationResultType.NO_MATCH, False)

    monitor = MonitorThread(
        execution_plan,
        {"test": BlockingAdapter()},
        Queue(),
        clock=lambda: clock[0],
    )

    worker = Thread(target=monitor.run_once)
    worker.start()
    assert collection_started.wait(1.0)
    monitor.update_polling_intervals(
        {"redis": 60, "file": 60, "common": 60}
    )
    finish_collection.set()
    worker.join(1.0)
    assert not worker.is_alive()
    state = execution_plan.state_by_key[inherited.correlation_key]
    assert state.next_sample_due == 86400

    monitor.run_once()
    assert execution_plan.polling_interval == 60
    assert state.next_sample_due == 60

    # Interval updates belong to the plan and survive monitor replacement.
    clock = [0.0]
    inherited = replace(
        work_item(),
        sampling_interval=60,
        sampling_interval_is_explicit=False,
    )
    execution_plan = plan(inherited)
    old_monitor = MonitorThread(
        execution_plan,
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: clock[0],
    )
    old_monitor.update_polling_intervals(
        {"redis": 7, "file": 7, "common": 7}
    )

    replacement = MonitorThread(
        execution_plan,
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: clock[0],
    )
    replacement.drain_interval_update_queue()

    assert replacement.plan is old_monitor.plan
    assert execution_plan.polling_interval == 7
    assert execution_plan.interval_update_queue.empty()


def test_expired_monitor_evidence_ownership_recovers_to_ready():
    # Recover expired recheck, evidence, and primary-hold ownership.

    item = work_item()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: 100.0,
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.state = MonitorWorkState.RECHECK_REQUESTED
    state.hold_deadline = 99.0

    monitor._recover_expired_ownership(100.0)

    assert state.state == MonitorWorkState.READY
    assert monitor.diagnostics[-1]["state"] == "RECHECK_REQUESTED"

    for work_state, deadline_field in (
        (MonitorWorkState.IN_FLIGHT, "ack_deadline"),
        (MonitorWorkState.HELD_BY_PRIMARY, "hold_deadline"),
    ):
        item = work_item()
        monitor = MonitorThread(
            plan(item),
            {"test": SequenceAdapter([])},
            Queue(),
            clock=lambda: 100.0,
        )
        state = monitor.plan.state_by_key[item.correlation_key]
        state.state = work_state
        setattr(state, deadline_field, 99.0)

        monitor._recover_expired_ownership(100.0)

        assert state.state == MonitorWorkState.READY
        assert getattr(state, deadline_field) is None
        assert monitor.diagnostics[-1]["state"] == work_state.value


class _Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class RuntimeDSEHook(DSEHook):
    def __init__(self):
        self.values = {"SENSOR0": 10.0}
        self.thresholds = {"SENSOR0": 20.0}
        self.authoritative = False
        self.expansion_error = None
        self.expansions = 0
        self.collections = 0
        self.evaluations = 0

    def resolve_source(self, reference, context):
        def expand(unused_context):
            self.expansions += 1
            if self.expansion_error is not None:
                raise self.expansion_error
            return DSEExpansionResult(
                tuple(
                    DSEBinding(
                        instance=name,
                        source_id="SENSOR_INFO|{}".format(name),
                        data={"name": name},
                        value_configs=ValueConfig(
                            type="float", unit="units"
                        ),
                    )
                    for name in sorted(self.values)
                ),
                authoritative=self.authoritative,
            )

        def get_value(invocation):
            self.collections += 1
            return self.values[invocation.binding.instance]

        return DSESourceHandle(
            reference,
            expand,
            get_value,
            DSEExpansionPolicy(
                bootstrap_scans=2,
                bootstrap_interval=1,
                warmup_cycles=2,
                stable_interval=10,
            ),
        )

    def resolve_evaluation(self, reference, context):
        def get_comparator(invocation):
            self.evaluations += 1
            return ResolvedEvaluation(
                expected_value=self.thresholds[invocation.binding.instance],
                operator=">=",
                value_configs=ValueConfig(type="float", unit="units"),
            )

        return DSEEvaluationHandle(reference, get_comparator)


def test_runtime_dse_expands_warms_up_and_refreshes_evaluator_each_sample():
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    configured = document["signatures"][0]["signature"]["conditions"][
        "events"
    ][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
            "sampling_interval": 1,
        }
    )
    hook = RuntimeDSEHook()
    validated = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    assert validated.activation_valid
    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    assert not bundle.work_items
    assert len(bundle.templates) == 1
    template = next(iter(bundle.templates.values()))
    assert template.item.schema_version == validated.schema_version

    clock = _Clock()
    evidence = Queue()
    monitor = MonitorThread(
        bundle.monitor_plans["common"],
        adapter_map(),
        evidence,
        clock=clock,
        wall_clock=lambda: 1000.0 + clock.value,
    )

    for value in (0.0, 1.0, 2.0, 3.0):
        clock.value = value
        monitor.run_once()

    expansion = evidence.get_nowait()
    assert isinstance(expansion, DSEExpansionEvent)
    assert len(expansion.added_items) == 1
    assert expansion.added_items[0].schema_version == validated.schema_version
    state = next(iter(monitor.plan.expansion_state_by_key.values()))
    assert state.phase == "STABLE"
    assert hook.expansions == 4
    assert hook.collections == 4
    assert hook.evaluations == 4

    hook.thresholds["SENSOR0"] = 5.0
    clock.value = 4.0
    monitor.run_once()

    matched = evidence.get_nowait()
    assert matched.result.result == EvaluationResultType.MATCH
    assert matched.result.expected == 5.0
    assert hook.expansions == 4
    assert hook.evaluations == 5

    hook.thresholds.clear()
    child = next(iter(monitor.plan.expanded_items_by_key.values()))
    failed = adapter_map()["dse"].collect(child)
    assert failed.result == EvaluationResultType.EVALUATION_ERROR
    assert failed.error_category == "EVALUATION_ERROR"


@pytest.mark.parametrize(
    "blocked_state",
    (
        MonitorWorkState.IN_FLIGHT,
        MonitorWorkState.HELD_BY_PRIMARY,
    ),
)
def test_authoritative_dse_removal_waits_for_primary_owned_child(
    blocked_state,
):
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    configured = document["signatures"][0]["signature"]["conditions"][
        "events"
    ][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
        }
    )
    hook = RuntimeDSEHook()
    hook.authoritative = True
    validated = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    evidence = Queue()
    monitor = MonitorThread(
        bundle.monitor_plans["common"], adapter_map(), evidence
    )
    template_id, template = next(iter(bundle.templates.items()))
    expansion_state = monitor.plan.expansion_state_by_key[template_id]

    monitor._expand_template(
        template_id, template, expansion_state, adapter_map()["dse"], 0.0
    )
    added = evidence.get_nowait()
    child_key = added.added_items[0].correlation_key
    assert added.present_instances == ("SENSOR0",)

    hook.values.clear()
    monitor.plan.state_by_key[child_key].state = blocked_state
    monitor._expand_template(
        template_id, template, expansion_state, adapter_map()["dse"], 1.0
    )
    deferred = evidence.get_nowait()
    assert deferred.authoritative
    assert deferred.present_instances == ()
    assert deferred.removed_keys == ()
    assert child_key in monitor.plan.expanded_items_by_key

    monitor.plan.state_by_key[child_key].state = MonitorWorkState.READY
    monitor._expand_template(
        template_id, template, expansion_state, adapter_map()["dse"], 2.0
    )
    removed = evidence.get_nowait()
    assert removed.removed_keys == (child_key,)
    assert child_key not in monitor.plan.expanded_items_by_key


def _runtime_dse_bundle(hook):
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    configured = document["signatures"][0]["signature"]["conditions"][
        "events"
    ][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
        }
    )
    validated = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    return build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )


def test_dse_discovery_phase_lifecycle():
    """Exercise failures, empty inventory, scheduling, change, and stability."""

    hook = RuntimeDSEHook()
    bundle = _runtime_dse_bundle(hook)
    monitor = MonitorThread(
        bundle.monitor_plans["common"], adapter_map(), Queue()
    )
    template_id, template = next(iter(bundle.templates.items()))
    state = monitor.plan.expansion_state_by_key[template_id]
    state.phase = "STABLE"
    hook.expansion_error = RuntimeError("inventory unavailable")

    monitor._expand_template(
        template_id, template, state, adapter_map()["dse"], 10.0
    )

    assert state.phase == "WARMUP"
    assert state.warmup_cycles_completed == 0
    assert state.next_expansion_due == (
        10.0 + template.source_handle.policy.bootstrap_interval
    )
    assert state.last_error == "inventory unavailable"
    assert "DSE expansion failed" in monitor.diagnostics[-1]["reason"]

    state.phase = "BOOTSTRAP"
    monitor._expand_template(
        template_id, template, state, adapter_map()["dse"], 20.0
    )
    assert state.phase == "BOOTSTRAP"
    assert state.next_expansion_due == (
        20.0 + template.source_handle.policy.bootstrap_interval
    )

    # Authoritative empty inventory progresses through the normal phases.
    hook = RuntimeDSEHook()
    hook.authoritative = True
    hook.values.clear()
    hook.thresholds.clear()
    bundle = _runtime_dse_bundle(hook)
    evidence = Queue()
    monitor = MonitorThread(
        bundle.monitor_plans["common"], adapter_map(), evidence
    )
    template_id, template = next(iter(bundle.templates.items()))
    state = monitor.plan.expansion_state_by_key[template_id]
    adapter = adapter_map()["dse"]

    monitor._expand_template(template_id, template, state, adapter, 0.0)
    assert state.phase == "BOOTSTRAP"
    monitor._expand_template(template_id, template, state, adapter, 1.0)
    assert state.phase == "WARMUP"
    monitor._expand_template(template_id, template, state, adapter, 2.0)
    assert state.phase == "WARMUP"
    monitor._expand_template(template_id, template, state, adapter, 3.0)

    assert state.phase == "STABLE"
    assert state.child_keys == set()
    assert state.next_expansion_due == (
        3.0 + template.source_handle.policy.stable_interval
    )
    events = tuple(evidence.get_nowait() for unused in range(4))
    assert all(event.authoritative for event in events)
    assert all(event.present_instances == () for event in events)

    # Discovery scheduling waits for both child-cycle and time gates.
    hook = RuntimeDSEHook()
    bundle = _runtime_dse_bundle(hook)
    monitor = MonitorThread(
        bundle.monitor_plans["common"], adapter_map(), Queue()
    )
    template_id = next(iter(bundle.templates))
    state = monitor.plan.expansion_state_by_key[template_id]
    calls = []
    monitor._expand_template = lambda *args: calls.append(args)

    state.pending_cycle_keys = {"child"}
    monitor._expand_due_templates(5.0)
    assert calls == []
    monitor._refresh_next_poll(5.0)
    assert monitor._next_poll == 65.0

    state.pending_cycle_keys.clear()
    state.next_expansion_due = 10.0
    monitor._expand_due_templates(5.0)
    assert calls == []
    monitor._expand_due_templates(10.0)
    assert len(calls) == 1

    # Inventory changes reset warmup; unchanged scans restore stable cadence.
    hook = RuntimeDSEHook()
    hook.authoritative = True
    bundle = _runtime_dse_bundle(hook)
    evidence = Queue()
    monitor = MonitorThread(
        bundle.monitor_plans["common"], adapter_map(), evidence
    )
    template_id, template = next(iter(bundle.templates.items()))
    state = monitor.plan.expansion_state_by_key[template_id]
    adapter = adapter_map()["dse"]

    monitor._expand_template(template_id, template, state, adapter, 0.0)
    evidence.get_nowait()
    state.phase = "WARMUP"
    state.warmup_cycles_completed = 2
    hook.values = {"SENSOR1": 11.0}
    hook.thresholds = {"SENSOR1": 20.0}
    monitor._expand_template(template_id, template, state, adapter, 1.0)
    evidence.get_nowait()
    assert state.phase == "WARMUP"
    assert state.warmup_cycles_completed == 0

    state.warmup_cycles_completed = template.source_handle.policy.warmup_cycles - 1
    monitor._expand_template(template_id, template, state, adapter, 2.0)
    evidence.get_nowait()
    assert state.phase == "STABLE"
    assert state.next_expansion_due == (
        2.0 + template.source_handle.policy.stable_interval
    )

    monitor._expand_template(template_id, template, state, adapter, 3.0)
    evidence.get_nowait()
    assert state.phase == "STABLE"
    assert state.next_expansion_due == (
        3.0 + template.source_handle.policy.stable_interval
    )

    hook.values = {"SENSOR2": 12.0}
    hook.thresholds = {"SENSOR2": 20.0}
    monitor._expand_template(template_id, template, state, adapter, 4.0)
    evidence.get_nowait()
    assert state.phase == "WARMUP"
    assert state.warmup_cycles_completed == 0


def test_shared_dse_child_is_removed_only_after_last_template_relinquishes():
    hook = RuntimeDSEHook()
    hook.authoritative = True
    bundle = _runtime_dse_bundle(hook)
    first_id, first = next(iter(bundle.templates.items()))
    second_id = first_id + ":second"
    second = replace(first, template_id=second_id)
    plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "generation",
        {},
        {},
        Queue(),
        templates_by_key={first_id: first, second_id: second},
    )
    evidence = Queue()
    monitor = MonitorThread(plan, adapter_map(), evidence)
    adapter = adapter_map()["dse"]

    monitor._expand_template(
        first_id,
        first,
        plan.expansion_state_by_key[first_id],
        adapter,
        0.0,
    )
    evidence.get_nowait()
    monitor._expand_template(
        second_id,
        second,
        plan.expansion_state_by_key[second_id],
        adapter,
        0.0,
    )
    evidence.get_nowait()
    child_key = next(iter(plan.expanded_items_by_key))

    hook.values.clear()
    hook.thresholds.clear()
    monitor._expand_template(
        first_id,
        first,
        plan.expansion_state_by_key[first_id],
        adapter,
        1.0,
    )
    first_removal = evidence.get_nowait()
    assert first_removal.removed_keys == ()
    assert child_key in plan.expanded_items_by_key

    monitor._expand_template(
        second_id,
        second,
        plan.expansion_state_by_key[second_id],
        adapter,
        1.0,
    )
    final_removal = evidence.get_nowait()
    assert final_removal.removed_keys == (child_key,)
    assert child_key not in plan.expanded_items_by_key


def test_runtime_dse_evaluation_contract_and_value_config_precedence():
    reference = parse_reference("sensor:get_value()")
    evaluation_reference = parse_reference("sensor:get_threshold()")
    binding = DSEBinding(
        instance="SENSOR0",
        source_id="SENSOR_INFO|SENSOR0",
    )
    item = replace(
        work_item(),
        source_type="dse",
        source={},
        evaluation={"type": "dse"},
        dse_binding=binding,
        dse_source_handle=DSESourceHandle(
            reference,
            lambda unused_context: DSEExpansionResult((binding,)),
            lambda unused_invocation: 10,
        ),
        dse_evaluation_handle=DSEEvaluationHandle(
            evaluation_reference,
            lambda unused_invocation: {
                "type": "dse",
                "operator": ">=",
                "value": 5,
            },
        ),
    )

    result = DSEAdapter().collect(item)

    assert result.result == EvaluationResultType.EVALUATION_ERROR
    assert result.retryable is False
    assert "must return ResolvedEvaluation" in result.error

    # Typed runtime evaluation preserves explicit rule metadata precedence.
    reference = parse_reference("sensor:get_value()")
    evaluation_reference = parse_reference("sensor:get_threshold()")
    binding = DSEBinding(
        instance="SENSOR0",
        source_id="SENSOR_INFO|SENSOR0",
    )
    vendor_config = ValueConfig(type="float", unit="vendor-units")
    adapter = DSEAdapter()
    item = replace(
        work_item(),
        source_type="dse",
        source={},
        evaluation={
            "type": "dse",
            "operator": ">=",
            "value_configs": ValueConfig(
                type="int", unit="rule-units"
            ).as_payload(),
        },
        dse_binding=binding,
        dse_source_handle=DSESourceHandle(
            reference,
            lambda unused_context: DSEExpansionResult((binding,)),
            lambda unused_invocation: 10,
        ),
        dse_evaluation_handle=DSEEvaluationHandle(
            evaluation_reference,
            lambda unused_invocation: ResolvedEvaluation(
                expected_value=5,
                operator=">=",
                value_configs=vendor_config,
            ),
        ),
    )

    explicit = adapter.get_evaluator(item)
    implicit = adapter.get_evaluator(
        replace(
            item,
            evaluation={
                "type": "dse",
                "operator": ">=",
                "value_configs": ValueConfig().as_payload(),
            },
        )
    )

    assert explicit["value_configs"]["unit"] == "rule-units"
    assert implicit["value_configs"] == vendor_config.as_payload()


def test_runtime_dse_does_not_publish_children_before_expansion_registration():
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    configured = document["signatures"][0]["signature"]["conditions"][
        "events"
    ][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
            "sampling_interval": 1,
        }
    )
    hook = RuntimeDSEHook()
    validated = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    clock = _Clock()
    evidence = Queue(maxsize=1)
    evidence.put_nowait("occupied")
    monitor = MonitorThread(
        bundle.monitor_plans["common"],
        adapter_map(),
        evidence,
        clock=clock,
    )

    monitor.run_once()

    assert not monitor.plan.expanded_items_by_key
    assert "queue is full" in next(
        iter(monitor.plan.expansion_state_by_key.values())
    ).last_error

    evidence.get_nowait()
    evidence.task_done()
    clock.value = 1.0
    monitor.run_once()

    assert len(monitor.plan.expanded_items_by_key) == 1
    assert isinstance(evidence.get_nowait(), DSEExpansionEvent)


@pytest.mark.parametrize("logic", ("1 AND 2", "1 OR 2"))
def test_runtime_dse_clones_common_predicates_only_for_discovered_instances(
    logic,
):
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    conditions = document["signatures"][0]["signature"]["conditions"]
    direct = deepcopy(conditions["events"][0])
    direct["event"]["id"] = 2
    configured = conditions["events"][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
        }
    )
    conditions["events"].append(direct)
    conditions["logic"] = logic
    hook = RuntimeDSEHook()
    hook.values["SENSOR1"] = 11.0
    hook.thresholds["SENSOR1"] = 20.0
    validated = validate_document(
        document,
        ValidationContext(
            dse_registry=DSERegistry(hook=hook)
        ),
    )
    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    assert not bundle.work_items
    assert not bundle.signatures
    template = next(iter(bundle.templates.values()))
    assert len(template.common_items) == 1
    assert template.common_items[0].correlation_key.startswith("prototype:")
    evidence = Queue()
    monitor = MonitorThread(
        bundle.monitor_plans["common"],
        adapter_map(),
        evidence,
        clock=_Clock(),
    )

    monitor._expand_due_templates(0.0)

    registration = evidence.get_nowait()
    assert {
        (item.event_id, item.component_name, item.common_predicate)
        for item in registration.added_items
    } == {
        (1, "SENSOR0", False),
        (1, "SENSOR1", False),
        (2, "SENSOR0", True),
        (2, "SENSOR1", True),
    }
    correlation = CorrelationEngine(bundle.signatures)
    for item in registration.added_items:
        correlation.register_work_item(
            registration.signature, item, registration.plan_generation
        )
    assert set(correlation.executions) == {
        (1000001, "SENSOR0"),
        (1000001, "SENSOR1"),
    }
    assert all(
        set(execution.event_keys) == {1, 2}
        for execution in correlation.executions.values()
    )


def test_dse_expansion_reuses_static_common_work_for_overlapping_instance():
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    conditions = document["signatures"][0]["signature"]["conditions"]
    explicit = deepcopy(conditions["events"][0])
    explicit["event"]["id"] = 2
    explicit["event"]["instances"] = ["SENSOR0:explicit-source"]
    common = deepcopy(conditions["events"][0])
    common["event"]["id"] = 3
    configured = conditions["events"][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
        }
    )
    conditions["events"] = [conditions["events"][0], explicit, common]
    conditions["logic"] = "1 OR 2 OR 3"
    hook = RuntimeDSEHook()
    hook.values["SENSOR1"] = 11.0
    hook.thresholds["SENSOR1"] = 20.0
    validated = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    assert validated.activation_valid
    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    template = next(iter(bundle.templates.values()))

    expanded = work_items_for_dse_expansion(
        template, DSEAdapter().expand(template)
    )

    assert {
        (item.event_id, item.component_name) for item in bundle.work_items.values()
    } == {(2, "SENSOR0"), (3, "SENSOR0")}
    assert {
        (item.event_id, item.component_name) for item in expanded
    } == {(1, "SENSOR0"), (1, "SENSOR1"), (3, "SENSOR1")}
    assert not set(bundle.work_items).intersection(
        item.correlation_key for item in expanded
    )


def test_direct_only_common_rule_keeps_component_fallback_work():
    validated = load_rules("tests/dldd/fixtures/valid-redis-rule.json")

    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 41, "file": 42, "common": 43},
    )

    item = next(iter(bundle.work_items.values()))
    assert item.component_name == item.component_type
    assert bundle.signatures[(item.rule_id, item.component_name)].event_keys
    assert not bundle.templates


@pytest.mark.parametrize(
    "intervals,error_type,message",
    (
        (60, TypeError, "must be a mapping"),
        (
            {"redis": 60, "file": 60},
            ValueError,
            "missing common",
        ),
    ),
)
def test_monitor_plan_rejects_incomplete_atomic_cadence_updates(
    intervals, error_type, message
):
    execution_plan = plan(work_item())

    with pytest.raises(error_type, match=message):
        execution_plan.queue_polling_interval_update(intervals)


def test_mixed_dse_clones_follow_source_defaults_and_atomic_updates():
    with open("tests/dldd/fixtures/valid-redis-rule.json") as stream:
        document = json.load(stream)
    conditions = document["signatures"][0]["signature"]["conditions"]
    redis_inherited = deepcopy(conditions["events"][0])
    redis_inherited["event"]["id"] = 2
    redis_inherited["event"].pop("sampling_interval", None)
    file_inherited = deepcopy(redis_inherited)
    file_inherited["event"]["id"] = 3
    file_inherited["event"]["type"] = "file"
    file_inherited["event"]["path"] = {
        "file": "/tmp/dldd-test-value",
        "format": "integer",
    }
    redis_explicit = deepcopy(redis_inherited)
    redis_explicit["event"]["id"] = 4
    redis_explicit["event"]["sampling_interval"] = 17
    configured = conditions["events"][0]["event"]
    configured.update(
        {
            "type": "dse",
            "path": "{sensor*}:{get_value()}",
            "evaluation": {
                "type": "dse",
                "value": "{sensor*}:{get_high_threshold()}",
            },
        }
    )
    configured.pop("sampling_interval", None)
    conditions["events"] = [
        conditions["events"][0],
        redis_inherited,
        file_inherited,
        redis_explicit,
    ]
    conditions["logic"] = "1 OR 2 OR 3 OR 4"
    hook = RuntimeDSEHook()
    hook.values["SENSOR1"] = 11.0
    hook.thresholds["SENSOR1"] = 20.0
    validated = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    assert validated.activation_valid
    bundle = build_plans(
        validated.materialized_rules,
        "generation",
        {"redis": 100, "file": 200, "common": 300},
    )
    evidence = Queue()
    adapters = {
        "dse": DSEAdapter(),
        "redis": SequenceAdapter(
            [result(EvaluationResultType.NO_MATCH, False) for unused in range(4)]
        ),
        "file": SequenceAdapter(
            [result(EvaluationResultType.NO_MATCH, False) for unused in range(2)]
        ),
    }
    clock = _Clock()
    monitor = MonitorThread(
        bundle.monitor_plans["common"], adapters, evidence, clock=clock
    )

    monitor._expand_due_templates(0.0)
    evidence.get_nowait()
    monitor.poll_once(now=0.0, respect_schedule=True)

    due_by_event = {}
    for item in monitor.plan.item_snapshot().values():
        due_by_event.setdefault(item.event_id, set()).add(
            monitor.plan.state_by_key[item.correlation_key].next_sample_due
        )
    assert due_by_event == {
        1: {300.0},
        2: {100.0},
        3: {200.0},
        4: {17.0},
    }

    clock.value = 10.0
    monitor.update_polling_intervals(
        {"redis": 20, "file": 400, "common": 50}
    )
    monitor.drain_interval_update_queue()

    due_by_event = {}
    for item in monitor.plan.item_snapshot().values():
        due_by_event.setdefault(item.event_id, set()).add(
            monitor.plan.state_by_key[item.correlation_key].next_sample_due
        )
    assert monitor.plan.polling_intervals == {
        "redis": 20.0,
        "file": 400.0,
        "common": 50.0,
    }
    assert due_by_event == {
        1: {60.0},
        2: {30.0},
        3: {200.0},
        4: {17.0},
    }
