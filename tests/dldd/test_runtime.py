from __future__ import absolute_import

from dataclasses import replace
from queue import Empty, Queue
import subprocess
from threading import Event as ThreadEvent, Thread
import time

import pytest

from dldd.adapters import (
    CLIAdapter,
    FileAdapter,
    I2CAdapter,
    PlatformAPIAdapter,
    RedisAdapter,
    SysfsAdapter,
)
from dldd.evaluators import EvaluationContractError, evaluate
from dldd.monitor import AsyncCollectionPool, MonitorThread, command_for_event
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.planner import build_plans
from dldd.runtime import (
    CollectedValue,
    EvaluationResult,
    EvaluationResultType,
    MonitorCommandType,
    MonitorExecutionPlan,
    MonitorWorkItem,
    MonitorWorkState,
    MonitorWorkStateRecord,
    SourceAvailability,
    ValueConfig,
)
from dldd.validation import load_rules


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


def test_evaluators_cover_schema_contracts():
    assert evaluate({"type": "mask", "logic": "&", "value": "0b1000"}, "0b1100")
    assert evaluate({"type": "comparison", "operator": ">", "value": 3}, "4")
    assert evaluate({"type": "string", "operator": "contains", "value": "SU", "case_sensitive": False}, "psu")
    assert evaluate({"type": "boolean", "value": True}, "true")
    with pytest.raises(EvaluationContractError):
        evaluate({"type": "comparison", "operator": "bad", "value": 3}, 4)


def test_regex_evaluation_times_out_catastrophic_backtracking():
    started = time.monotonic()

    with pytest.raises(EvaluationContractError, match="exceeded"):
        evaluate(
            {"type": "string", "operator": "regex", "value": "(a+)+$"},
            "a" * 10000 + "!",
        )

    assert time.monotonic() - started < 1.0


def test_monitor_single_flight_and_clear_transition():
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
        command_for_event(
            matched,
            MonitorCommandType.RESUME,
            MonitorWorkState.READY,
            "processed",
        )
    )
    monitor.drain_control_queue()
    monitor.poll_once()
    cleared = evidence.get_nowait()
    assert cleared.result.result == EvaluationResultType.NO_MATCH


def test_stale_monitor_command_is_rejected():
    item = work_item()
    evidence = Queue()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([result(EvaluationResultType.MATCH, True)])},
        evidence,
    )
    monitor.poll_once()
    event = evidence.get_nowait()
    command = command_for_event(
        event,
        MonitorCommandType.RESUME,
        MonitorWorkState.READY,
        "processed",
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.work_state_generation += 1
    assert monitor.apply_command(command) is False
    assert state.state == MonitorWorkState.IN_FLIGHT


def test_execution_plan_items_are_immutable():
    item = work_item()
    execution_plan = plan(item)
    with pytest.raises(TypeError):
        execution_plan.items_by_key["new"] = item
    with pytest.raises(TypeError):
        item.source["new"] = "value"


def test_source_recovery_requires_configured_success_samples():
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
        command_for_event(
            unavailable_event,
            MonitorCommandType.RESUME,
            MonitorWorkState.DEGRADED,
            "retry",
        )
    )
    monitor.poll_once()
    with pytest.raises(Empty):
        evidence.get_nowait()
    monitor.poll_once()
    assert evidence.get_nowait().result.result == EvaluationResultType.SOURCE_RECOVERED


def test_queue_full_does_not_lose_a_clear_transition():
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


def test_queue_full_does_not_lose_a_source_recovery_transition():
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


def test_queue_full_failure_does_not_invent_an_unobserved_recovery():
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


def test_due_recheck_does_not_poll_unrelated_ready_keys_early():
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


def test_per_key_sampling_intervals_start_due_and_coalesce_missed_cycles():
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


def test_each_key_schedules_from_its_actual_attempt_time():
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


def test_async_collection_does_not_block_other_due_work_items():
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


def test_async_collection_pool_rejects_work_beyond_bounded_capacity():
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


def test_async_pool_saturation_leaves_monitor_work_due_without_failure():
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


def test_queued_async_work_is_not_duplicated_when_cadence_circles():
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


def test_async_recheck_jumps_ahead_of_normal_queued_work():
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


def test_async_recheck_submission_uses_high_priority():
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


def test_async_collection_match_uses_normal_evidence_path():
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


def test_planner_resolves_explicit_and_monitor_default_sampling_intervals():
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


def test_recheck_once_bypasses_cadence_without_resetting_normal_due_time():
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


def test_dynamic_monitor_default_updates_only_inherited_work_items():
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

    monitor.update_polling_interval(10)
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

    monitor.update_polling_interval(100)
    monitor.drain_interval_update_queue()
    assert execution_plan.polling_interval == 100
    assert execution_plan.items_by_key["inherited"] is inherited_identity
    assert execution_plan.items_by_key["explicit"].sampling_interval == 300
    # Increasing a default does not postpone an already-nearer inherited due.
    assert execution_plan.state_by_key["inherited"].next_sample_due == 10.0


def test_lower_dynamic_polling_interval_pulls_next_poll_forward():
    clock = [0.0]
    item = work_item()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: clock[0],
    )
    monitor._next_poll = 3600.0

    monitor.update_polling_interval(1)

    assert monitor.plan.polling_interval == 60
    monitor.drain_interval_update_queue()
    assert monitor.plan.polling_interval == 1
    # A never-sampled eligible key remains immediately due.
    assert monitor._next_poll == 0.0


def test_interval_update_queued_during_collection_cannot_be_overwritten():
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
    monitor.update_polling_interval(60)
    finish_collection.set()
    worker.join(1.0)
    assert not worker.is_alive()
    state = execution_plan.state_by_key[inherited.correlation_key]
    assert state.next_sample_due == 86400

    monitor.run_once()
    assert execution_plan.polling_interval == 60
    assert state.next_sample_due == 60


def test_replacement_monitor_consumes_plan_owned_interval_update():
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
    old_monitor.update_polling_interval(7)

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


def test_recheck_lease_expiry_recovers_key_and_records_diagnostic():
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


def test_static_validation_does_not_require_platform_dse_hook():
    validated = load_rules(
        "tests/dldd/fixtures/valid-psu-hld.yaml", materialize=False
    )
    assert validated.file_valid
    assert validated.ruleset is not None
    assert len(validated.ruleset.signatures) == 1


def test_redis_adapter_uses_full_key_and_slash_value_path():
    calls = []

    def reader(database, table, key):
        calls.append((database, table, key))
        return {"value": '{"output_voltage": 51.5}'}

    base = work_item()
    redis_item = MonitorWorkItem(
        rule_id=base.rule_id,
        rule_name=base.rule_name,
        rule_version=base.rule_version,
        schema_version=base.schema_version,
        severity=base.severity,
        priority=base.priority,
        symptom=base.symptom,
        error_type=base.error_type,
        component_type=base.component_type,
        component_name=base.component_name,
        event_id=base.event_id,
        correlation_key=base.correlation_key,
        source_id=base.source_id,
        source_type="redis",
        source={
            "database": "STATE_DB",
            "table": "PSU_INFO",
            "key": "PSU_INFO|PSU0",
            "path": "value/output_voltage",
        },
        evaluation={"type": "comparison", "operator": ">", "value": 50.0},
        value_config=ValueConfig(type="float", unit="volts"),
    )
    collected = RedisAdapter(reader).collect(redis_item)
    assert collected.result == EvaluationResultType.MATCH
    assert collected.value.normalized == 51.5
    assert calls == [("STATE_DB", "PSU_INFO", "PSU_INFO|PSU0")]


class CollectingHook(VendorHook):
    def collect(self, operation):
        return operation["value"]

    def execute_action(self, action):
        return {}


class ValidatingHook(CollectingHook):
    def validate_source(self, operation):
        if "value" not in operation:
            raise ValueError("platform source requires value")


class I2CResolvingHook(CollectingHook):
    def __init__(self):
        self.validated = []

    def validate_source(self, operation):
        self.validated.append(operation["bus"])

    def resolve_i2c_bus(self, bus, operation):
        return "6" if bus == "IO-MUX-6" else bus


def test_builtin_adapters_conform_without_hardware(tmp_path):
    base = work_item()
    source_file = tmp_path / "source.json"
    source_file.write_text('{"reading": 5}')
    file_item = replace(
        base,
        source_type="file",
        source={"file": str(source_file), "format": "json", "path": "reading"},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    sysfs_file = tmp_path / "sysfs"
    sysfs_file.write_text("5")
    sysfs_item = replace(
        base,
        source_type="sysfs",
        source={"file": str(sysfs_file), "format": "integer"},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    cli_item = replace(
        base,
        source_type="cli",
        source={"argv": ["diagnostic"], "timeout": 1, "max_output_bytes": 32},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    i2c_item = replace(
        base,
        source_type="i2c",
        source={
            "i2c_type": "get",
            "bus": "6",
            "chip_addr": "0x58",
            "command": "0x7a",
            "size": "b",
        },
        evaluation={"type": "mask", "logic": "&", "value": "0x80"},
    )
    hooks = VendorHookRegistry()
    hooks.register("platform", CollectingHook())
    platform_item = replace(
        base,
        source_type="platform_api",
        source={"hook": "platform", "value": 5},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    adapters = (
        (FileAdapter(), file_item),
        (SysfsAdapter(), sysfs_item),
        (
            CLIAdapter(
                lambda argv, **kwargs: subprocess.CompletedProcess(
                    argv, 0, stdout=b"5", stderr=b""
                )
            ),
            cli_item,
        ),
        (I2CAdapter(lambda source: "0x80"), i2c_item),
        (PlatformAPIAdapter(hooks), platform_item),
    )

    for adapter, item in adapters:
        adapter.validate(item)
        assert adapter.collect(item).result == EvaluationResultType.MATCH


def test_file_adapter_rejects_unsupported_format_before_polling(tmp_path):
    item = replace(
        work_item(),
        source_type="file",
        source={"file": str(tmp_path / "source"), "format": "pickle"},
    )

    with pytest.raises(ValueError, match="unsupported file format"):
        FileAdapter().validate(item)


def test_platform_hook_preflights_vendor_source_fields():
    hooks = VendorHookRegistry()
    hooks.register("platform", ValidatingHook())
    item = replace(
        work_item(),
        source_type="platform_api",
        source={"hook": "platform"},
    )

    with pytest.raises(ValueError, match="requires value"):
        PlatformAPIAdapter(hooks).validate(item)


def test_i2c_adapter_vendor_hook_resolves_logical_bus_before_collection():
    observed = []
    hook = I2CResolvingHook()
    hooks = VendorHookRegistry()
    hooks.register("i2c", hook)
    item = replace(
        work_item(),
        source_type="i2c",
        source={
            "i2c_type": "get",
            "bus": "IO-MUX-6",
            "chip_addr": "0x58",
            "command": "0x7a",
            "size": "b",
        },
        evaluation={"type": "mask", "logic": "&", "value": "0x80"},
    )
    adapter = I2CAdapter(
        lambda source: observed.append(source["bus"]) or "0x80",
        hooks=hooks,
    )

    adapter.validate(item)
    result = adapter.collect(item)

    assert result.result == EvaluationResultType.MATCH
    assert hook.validated == ["IO-MUX-6"]
    assert observed == ["6"]
