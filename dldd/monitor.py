"""Monitor threads and their single-flight state machine."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from itertools import count
from queue import Empty, Full, PriorityQueue, Queue
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Set, Tuple

from .adapters import DataSourceAdapter
from .bounded_calls import start_daemon_workers
from .planner import monitor_type_for_source, work_items_for_dse_expansion
from .runtime import (
    EvaluationResult,
    EvaluationResultType,
    DSEExpansionEvent,
    FaultEvidenceEvent,
    MonitorCommandType,
    MonitorControlCommand,
    MonitorExecutionPlan,
    MonitorWorkState,
    MonitorWorkStateRecord,
    RuleRuntimeStatus,
    SourceAvailability,
)


LOGGER = logging.getLogger(__name__)

DEFAULT_ASYNC_COLLECTION_WORKERS = 8
DEFAULT_ASYNC_COLLECTION_PENDING = 256
DEFAULT_ASYNC_RECHECK_RESERVE = 8
DSE_EXPANSION_FAST_INTERVAL = 5.0
DSE_EXPANSION_STABLE_SCANS = 3
DSE_EXPANSION_STABLE_INTERVAL = 300.0
_LEASE_RECOVERY = {
    MonitorWorkState.IN_FLIGHT: (
        "ack_deadline", "acknowledgement lease", False
    ),
    MonitorWorkState.HELD_BY_PRIMARY: (
        "hold_deadline", "hold deadline", False
    ),
    MonitorWorkState.RECHECK_REQUESTED: (
        "hold_deadline", "recheck deadline", True
    ),
}


@dataclass(frozen=True)
class AsyncCollectionCompletion:
    token: str
    result: EvaluationResult


def _monitor_error_result(error: Exception, completed_at: float) -> EvaluationResult:
    """Return the canonical result for an unexpected monitor exception."""

    return EvaluationResult(
        EvaluationResultType.COLLECTION_ERROR,
        completed_at=completed_at,
        error_category="MONITOR_ERROR",
        error=str(error),
        retryable=True,
    )


class AsyncCollectionPool:
    """Run opted-in single-item collection without blocking monitor threads."""

    def __init__(
        self,
        max_workers: int = DEFAULT_ASYNC_COLLECTION_WORKERS,
        max_pending: int = DEFAULT_ASYNC_COLLECTION_PENDING,
        recheck_reserve: int = DEFAULT_ASYNC_RECHECK_RESERVE,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        max_workers = max(1, int(max_workers))
        max_pending = max(0, int(max_pending))
        recheck_reserve = min(max(0, int(recheck_reserve)), max_pending)
        self._clock = monotonic_clock
        self._jobs: PriorityQueue[Tuple[Any, ...]] = PriorityQueue()
        self._sequence = count()
        self._total_slots = threading.BoundedSemaphore(
            max_workers + max_pending
        )
        self._normal_slots = threading.BoundedSemaphore(
            max_workers + max_pending - recheck_reserve
        )
        self._state_lock = threading.Lock()
        self._closed = False
        self._worker_count = max_workers
        self._busy_workers = 0
        self._queued_jobs = 0
        self._queue_latency_seconds = 0.0
        self._queue_latency_samples = 0
        self._execution_seconds = 0.0
        self._execution_samples = 0
        self._metrics_started_at = self._clock()
        self._utilization_updated_at = self._metrics_started_at
        self._busy_worker_seconds = 0.0
        self._workers = start_daemon_workers(
            max_workers, "dldd-async-collection-", self._worker
        )

    def submit(
        self,
        token: str,
        collector: Callable[[], EvaluationResult],
        completion_queue: Queue,
        high_priority: bool = False,
    ) -> bool:
        """Admit work while preserving reserved capacity for rechecks."""

        normal_slot = not high_priority
        with self._state_lock:
            if self._closed:
                return False
            if normal_slot and not self._normal_slots.acquire(False):
                return False
            if not self._total_slots.acquire(False):
                if normal_slot:
                    self._normal_slots.release()
                return False
            priority = 0 if high_priority else 1
            submitted_at = self._clock()
            self._queued_jobs += 1
            self._jobs.put_nowait(
                (
                    priority,
                    next(self._sequence),
                    (
                        token,
                        collector,
                        completion_queue,
                        normal_slot,
                        submitted_at,
                    ),
                )
            )
        return True

    def metrics(self) -> Dict[str, float]:
        """Return a consistent service-lifetime pool telemetry snapshot."""

        with self._state_lock:
            now = self._clock()
            self._advance_utilization(now)
            elapsed = max(0.0, now - self._metrics_started_at)
            queue_average = (
                self._queue_latency_seconds / self._queue_latency_samples
                if self._queue_latency_samples
                else 0.0
            )
            execution_average = (
                self._execution_seconds / self._execution_samples
                if self._execution_samples
                else 0.0
            )
            utilization = (
                self._busy_worker_seconds
                / (elapsed * self._worker_count)
                * 100.0
                if elapsed
                else 0.0
            )
            return {
                "async_pool_workers": self._worker_count,
                "async_pool_busy": self._busy_workers,
                "async_pool_queued": self._queued_jobs,
                "async_pool_avg_queue_latency_ms": round(
                    queue_average * 1000.0, 3
                ),
                "async_pool_avg_execution_time_ms": round(
                    execution_average * 1000.0, 3
                ),
                "async_pool_avg_utilization_percent": round(utilization, 3),
            }

    def _advance_utilization(self, now: float) -> None:
        elapsed = max(0.0, now - self._utilization_updated_at)
        self._busy_worker_seconds += elapsed * self._busy_workers
        self._utilization_updated_at = now

    def _worker(self) -> None:
        while True:
            _, _, job = self._jobs.get()
            execution_started_at = None
            normal_slot = False
            try:
                if job is None:
                    return
                (
                    token,
                    collector,
                    completion_queue,
                    normal_slot,
                    submitted_at,
                ) = job
                with self._state_lock:
                    self._queued_jobs -= 1
                    if self._closed:
                        execute = False
                    else:
                        execute = True
                        execution_started_at = self._clock()
                        self._queue_latency_seconds += max(
                            0.0, execution_started_at - submitted_at
                        )
                        self._queue_latency_samples += 1
                        self._advance_utilization(execution_started_at)
                        self._busy_workers += 1
                if not execute:
                    continue
                try:
                    result = collector()
                except Exception as error:
                    result = _monitor_error_result(error, time.time())
                completion_queue.put_nowait(
                    AsyncCollectionCompletion(token, result)
                )
            finally:
                if execution_started_at is not None:
                    execution_completed_at = self._clock()
                    with self._state_lock:
                        self._advance_utilization(execution_completed_at)
                        self._busy_workers -= 1
                        self._execution_seconds += max(
                            0.0,
                            execution_completed_at - execution_started_at,
                        )
                        self._execution_samples += 1
                if job is not None:
                    self._total_slots.release()
                    if normal_slot:
                        self._normal_slots.release()
                self._jobs.task_done()

    def shutdown(self, wait: bool = True) -> None:
        with self._state_lock:
            if not self._closed:
                self._closed = True
                for _unused_worker in self._workers:
                    self._jobs.put_nowait(
                        (2, next(self._sequence), None)
                    )
        if wait:
            for worker in self._workers:
                worker.join()


class MonitorThread(threading.Thread):
    """Poll one immutable plan and own all writes to its state map."""

    def __init__(
        self,
        plan: MonitorExecutionPlan,
        adapters: Mapping[str, DataSourceAdapter],
        evidence_queue: Queue,
        fault_evidence_ack_timeout: float = 120.0,
        source_recovery_samples: int = 1,
        async_collection_pool: Optional[AsyncCollectionPool] = None,
        stop_event: Optional[threading.Event] = None,
        clock=time.monotonic,
        wall_clock=time.time,
    ) -> None:
        super().__init__(name="dldd-{}".format(plan.monitor_id), daemon=True)
        self.plan = plan
        self.adapters = adapters
        self.evidence_queue = evidence_queue
        self.fault_evidence_ack_timeout = fault_evidence_ack_timeout
        self.source_recovery_samples = max(1, source_recovery_samples)
        self.async_collection_pool = async_collection_pool
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.wall_clock = wall_clock
        self._sequence = 0
        self._next_poll = self.clock()
        self._async_completions: Queue[AsyncCollectionCompletion] = Queue()
        self._async_jobs: Dict[str, Tuple[Any, ...]] = {}
        self._dse_templates_by_child: Dict[str, Set[str]] = {}
        self.diagnostics: Deque[Dict[str, Any]] = deque(maxlen=32)
        async_work = any(
            item.async_collection
            for item in self.plan.items_by_key.values()
        ) or any(
            template.item.async_collection
            for template in self.plan.templates_by_key.values()
        )
        if async_work and self.async_collection_pool is None:
            raise ValueError(
                "async collection work requires a shared collection pool"
            )
        for state in self.plan.state_by_key.values():
            if state.state == MonitorWorkState.COLLECTING:
                self._transition(state, MonitorWorkState.READY)
                state.next_sample_due = None

    def stop(self) -> None:
        self.stop_event.set()

    def update_polling_intervals(self, intervals) -> None:
        """Queue all source defaults for atomic application by this thread."""

        self.plan.interval_update_queue.put_nowait(
            self.plan.validated_polling_intervals(intervals)
        )

    def drain_interval_update_queue(self) -> None:
        """Apply queued defaults while retaining sole ownership of cadence state."""

        self._drain_queue(
            self.plan.interval_update_queue, self._apply_polling_intervals
        )

    @staticmethod
    def _drain_queue(queue, consume) -> None:
        """Drain one owned queue while balancing every completed item."""

        while True:
            try:
                item = queue.get_nowait()
            except Empty:
                return
            try:
                consume(item)
            finally:
                queue.task_done()

    def _apply_polling_intervals(self, intervals) -> None:
        intervals = self.plan.validated_polling_intervals(intervals)
        now = self.clock()
        self.plan.polling_intervals = intervals
        self.plan.polling_interval = intervals[self.plan.monitor_type]
        for key, item in self.plan.item_snapshot().items():
            if item.sampling_interval_is_explicit:
                continue
            state = self.plan.state_by_key[key]
            if state.next_sample_due is not None:
                # Do not postpone work already scheduled sooner.
                interval = intervals[monitor_type_for_source(item.source_type)]
                state.next_sample_due = min(state.next_sample_due, now + interval)
        self._refresh_next_poll(now)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("unhandled monitor cycle error in %s", self.plan.monitor_id)
            self.stop_event.wait(0.2)

    def run_once(self) -> None:
        self.drain_async_completions()
        self.drain_interval_update_queue()
        self.drain_control_queue()
        now = self.clock()
        self._recover_expired_ownership(now)
        self._expand_due_templates(now)
        normal_poll_due = now >= self._next_poll
        recheck_due = any(
            state.state == MonitorWorkState.RECHECK_REQUESTED
            and (
                state.recheck_not_before is None
                or now >= state.recheck_not_before
            )
            for state in self.plan.state_by_key.values()
        )
        if not normal_poll_due and not recheck_due:
            return
        self.poll_once(include_normal=normal_poll_due, respect_schedule=True)
        self._refresh_next_poll(self.clock())

    def _refresh_next_poll(self, now: float) -> None:
        due_times = []
        for state in self.plan.state_by_key.values():
            if state.state not in (
                MonitorWorkState.READY,
                MonitorWorkState.DEGRADED,
            ):
                continue
            due_times.append(
                now if state.next_sample_due is None else state.next_sample_due
            )
        for expansion_state in self.plan.expansion_state_by_key.values():
            due_times.append(
                now
                if expansion_state.next_expansion_due is None
                else expansion_state.next_expansion_due
            )
        self._next_poll = (
            min(due_times)
            if due_times
            else now + max(0.1, self.plan.polling_interval)
        )

    def _make_normal_work_due(self, state: MonitorWorkStateRecord) -> None:
        due = (
            self.clock()
            if state.next_sample_due is None
            else state.next_sample_due
        )
        self._next_poll = min(self._next_poll, due)

    def drain_control_queue(self) -> None:
        self._drain_queue(self.plan.control_queue, self.apply_command)

    def apply_command(self, command: MonitorControlCommand) -> bool:
        if command.monitor_id != self.plan.monitor_id:
            LOGGER.warning("discarding command for monitor %s", command.monitor_id)
            return False
        if command.plan_generation != self.plan.plan_generation:
            LOGGER.warning("discarding stale command generation %s", command.command_id)
            return False
        state = self.plan.state_by_key.get(command.correlation_key)
        if state is None:
            LOGGER.warning("discarding command for unknown key %s", command.correlation_key)
            return False
        if (
            command.expected_work_state_generation is not None
            and command.expected_work_state_generation != state.work_state_generation
        ):
            LOGGER.warning("discarding stale work-state command %s", command.command_id)
            return False

        if command.command == MonitorCommandType.RESUME:
            target = command.target_state
            if target not in (MonitorWorkState.READY, MonitorWorkState.DEGRADED):
                target = MonitorWorkState.READY
            self._transition(state, target)
            state.ack_deadline = None
            state.hold_deadline = None
            state.recheck_not_before = None
            self._make_normal_work_due(state)
        elif command.command == MonitorCommandType.HOLD:
            self._transition(state, MonitorWorkState.HELD_BY_PRIMARY)
            state.ack_deadline = None
            state.hold_deadline = command.hold_deadline
        elif command.command == MonitorCommandType.RECHECK_ONCE:
            self._transition(state, MonitorWorkState.RECHECK_REQUESTED)
            state.ack_deadline = None
            state.hold_deadline = command.hold_deadline
            state.recheck_not_before = command.recheck_not_before
        elif command.command == MonitorCommandType.SUSPEND:
            target = command.target_state
            if target not in (MonitorWorkState.BROKEN, MonitorWorkState.SUSPENDED):
                target = MonitorWorkState.SUSPENDED
            self._transition(state, target)
            state.ack_deadline = None
            state.hold_deadline = None
            state.recheck_not_before = None
        else:
            LOGGER.warning("discarding unsupported command %s", command.command)
            return False
        return True

    @staticmethod
    def _transition(state: MonitorWorkStateRecord, target: MonitorWorkState) -> None:
        state.state = target
        state.work_state_generation += 1

    def _recover_expired_ownership(self, now: float) -> None:
        for key, state in self.plan.state_by_key.items():
            recovery = _LEASE_RECOVERY.get(state.state)
            if recovery is None:
                continue
            deadline_name, label, clear_recheck = recovery
            deadline = getattr(state, deadline_name)
            if deadline is not None and now >= deadline:
                LOGGER.error("primary %s expired for %s", label, key)
                self._record_diagnostic(
                    "primary ownership lease expired",
                    state.state.value,
                    correlation_key=key,
                    now=now,
                )
                self._transition(state, MonitorWorkState.READY)
                setattr(state, deadline_name, None)
                if clear_recheck:
                    state.recheck_not_before = None
                self._make_normal_work_due(state)

    def _expand_due_templates(self, now: float) -> None:
        adapter = self.adapters.get("dse")
        if adapter is None:
            return
        for template_id in sorted(self.plan.templates_by_key):
            state = self.plan.expansion_state_by_key[template_id]
            if (
                state.next_expansion_due is not None
                and now < state.next_expansion_due
            ):
                continue
            self._expand_template(
                template_id,
                self.plan.templates_by_key[template_id],
                state,
                adapter,
                now,
            )

    def _expand_template(
        self, template_id, template, state, adapter, now
    ) -> None:
        try:
            result = adapter.expand(template)
            items = work_items_for_dse_expansion(template, result)
        except Exception as error:
            state.last_error = str(error)
            state.unchanged_scans = 0
            state.next_expansion_due = now + DSE_EXPANSION_FAST_INTERVAL
            self._record_diagnostic(
                "DSE expansion failed: {}".format(error),
                "EXPANSION",
                template_id=template_id,
            )
            return

        fingerprint = tuple(
            sorted(
                (item.dse_binding.instance, item.dse_binding.source_id)
                for item in items
                if item.dse_binding is not None
            )
        )
        changed = (
            state.binding_fingerprint is None
            or fingerprint != state.binding_fingerprint
        )
        desired = {item.correlation_key: item for item in items}
        added = []
        for key, item in desired.items():
            existing = self.plan.expanded_items_by_key.get(key)
            if existing == item:
                continue
            added.append(item)

        relinquished = []
        removed = []
        deferred_removal = False
        for key in tuple(state.child_keys - set(desired)):
            child_state = self.plan.state_by_key.get(key)
            if child_state is None or child_state.state not in (
                MonitorWorkState.READY,
                MonitorWorkState.DEGRADED,
                MonitorWorkState.SUSPENDED,
                MonitorWorkState.BROKEN,
            ):
                deferred_removal = True
                continue
            relinquished.append(key)
            owners = self._dse_templates_by_child.get(key, set())
            if not (owners - {template_id}):
                removed.append(key)

        # Publish the first successful inventory, including an empty one.
        if added or removed or changed:
            event = DSEExpansionEvent(
                monitor_id=self.plan.monitor_id,
                plan_generation=self.plan.plan_generation,
                template_id=template_id,
                signature=template.signature,
                added_items=tuple(added),
                removed_keys=tuple(removed),
                present_instances=tuple(
                    sorted({binding.instance for binding in result.bindings})
                ),
                observed_at=self.wall_clock(),
            )
            try:
                # Register children before their first sample reaches primary.
                self.evidence_queue.put_nowait(event)
            except Full:
                state.last_error = "primary evidence queue is full"
                state.next_expansion_due = now + DSE_EXPANSION_FAST_INTERVAL
                self._record_diagnostic(
                    "DSE expansion registration queue is full",
                    "EXPANSION",
                    template_id=template_id,
                )
                return

        for key, item in desired.items():
            self.plan.add_expanded_item(item)
            self._dse_templates_by_child.setdefault(key, set()).add(
                template_id
            )
        for key in relinquished:
            owners = self._dse_templates_by_child.get(key, set())
            owners.discard(template_id)
            if owners:
                continue
            self._dse_templates_by_child.pop(key, None)
            self.plan.remove_expanded_item(key)

        retained = state.child_keys - set(relinquished)
        state.child_keys = retained | set(desired)
        state.binding_fingerprint = fingerprint
        state.last_expansion_timestamp = self.wall_clock()
        state.last_error = ""
        # Pending retirements keep inventory on the fast scan interval.
        state.unchanged_scans = (
            0 if changed or deferred_removal else state.unchanged_scans + 1
        )
        interval = (
            DSE_EXPANSION_STABLE_INTERVAL
            if state.unchanged_scans >= DSE_EXPANSION_STABLE_SCANS
            else DSE_EXPANSION_FAST_INTERVAL
        )
        state.next_expansion_due = now + interval

    def _record_diagnostic(
        self, reason: str, state: str, now=None, **context
    ) -> None:
        diagnostic = {
            "monitor": self.plan.monitor_id,
            **context,
            "state": state,
            "reason": reason,
            "observed_at": self.wall_clock(),
        }
        if now is not None:
            diagnostic["monotonic_at"] = now
        self.diagnostics.append(diagnostic)

    def poll_once(
        self,
        now: Optional[float] = None,
        include_normal: bool = True,
        respect_schedule: bool = False,
    ) -> None:
        self.drain_async_completions()
        cycle_now = self.clock() if now is None else now
        items = self.plan.item_snapshot()
        for key in sorted(items):
            state = self.plan.state_by_key[key]
            recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
            key_now = self.clock() if respect_schedule else cycle_now
            if not recheck and (
                not include_normal
                or state.state
                not in (MonitorWorkState.READY, MonitorWorkState.DEGRADED)
            ):
                continue
            if (
                recheck
                and state.recheck_not_before is not None
                and key_now < state.recheck_not_before
            ):
                continue
            item = items[key]
            next_sample_due: Optional[float] = None
            if not recheck:
                attempt_time = key_now
                if (
                    respect_schedule
                    and state.next_sample_due is not None
                    and attempt_time < state.next_sample_due
                ):
                    continue
                # Schedule each key from its own collection attempt.
                interval = (
                    item.sampling_interval
                    if item.sampling_interval_is_explicit
                    else self.plan.polling_intervals[
                        monitor_type_for_source(item.source_type)
                    ]
                )
                next_sample_due = attempt_time + interval
            if item.async_collection:
                if (
                    self._submit_async_collection(key, state, item)
                    and next_sample_due is not None
                ):
                    state.next_sample_due = next_sample_due
                continue
            if next_sample_due is not None:
                state.next_sample_due = next_sample_due
            state.last_attempt_timestamp = self.wall_clock()
            adapter = self.adapters[item.source_type]
            result = self._collect_result(adapter, item)
            self._handle_result(key, state, item, result, recheck)
        self.drain_async_completions()

    def _submit_async_collection(self, key, state, item) -> bool:
        from_recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
        previous_state = state.state
        adapter = self.adapters[item.source_type]
        token = uuid.uuid4().hex
        pool = self.async_collection_pool
        if pool is None:
            raise RuntimeError("async collection pool is unavailable")
        submitted = pool.submit(
            token,
            lambda: self._collect_result(adapter, item),
            self._async_completions,
            high_priority=from_recheck,
        )
        if not submitted:
            self._record_diagnostic(
                "async collection capacity is exhausted",
                previous_state.value,
                correlation_key=key,
            )
            return False
        state.last_attempt_timestamp = self.wall_clock()
        self._transition(state, MonitorWorkState.COLLECTING)
        self._async_jobs[token] = (
            key,
            item,
            previous_state,
            state.work_state_generation,
        )
        return True

    def drain_async_completions(self) -> None:
        while True:
            try:
                completion = self._async_completions.get_nowait()
            except Empty:
                return
            try:
                pending = self._async_jobs.pop(completion.token, None)
                if pending is None:
                    continue
                key, item, previous_state, generation = pending
                state = self.plan.state_by_key.get(key)
                if (
                    state is None
                    or state.state != MonitorWorkState.COLLECTING
                    or state.work_state_generation != generation
                ):
                    self._record_diagnostic(
                        "discarded stale async collection result",
                        "COLLECTING",
                        correlation_key=key,
                    )
                    continue
                self._transition(state, previous_state)
                self._handle_result(
                    key,
                    state,
                    item,
                    completion.result,
                    previous_state == MonitorWorkState.RECHECK_REQUESTED,
                )
            finally:
                self._async_completions.task_done()

    def _collect_result(self, adapter, item) -> EvaluationResult:
        try:
            return adapter.collect(item)
        except Exception as error:
            return _monitor_error_result(error, self.wall_clock())

    def _handle_result(
        self,
        key: str,
        state: MonitorWorkStateRecord,
        item,
        result: EvaluationResult,
        from_recheck: bool,
    ) -> None:
        previous_sample = state.last_sample_state
        previous_source = state.source_status

        if result.result in (EvaluationResultType.MATCH, EvaluationResultType.NO_MATCH):
            state.last_success_timestamp = self.wall_clock()
            state.consecutive_failure_count = 0
            if previous_source == SourceAvailability.UNAVAILABLE:
                state.recovery_success_count += 1
                if state.recovery_success_count < self.source_recovery_samples:
                    return
                recovered = EvaluationResult(
                    EvaluationResultType.SOURCE_RECOVERED,
                    value=result.value,
                    evaluator_type=result.evaluator_type,
                    operator=result.operator,
                    expected=result.expected,
                    condition_config=result.condition_config,
                    source_status=SourceAvailability.RECOVERED,
                    collection_started_at=result.collection_started_at,
                    completed_at=result.completed_at,
                )
                if self._enqueue(item, state, recovered, from_recheck):
                    state.source_status = SourceAvailability.AVAILABLE
                    state.recovery_success_count = 0
                return
            state.source_status = SourceAvailability.AVAILABLE
            state.recovery_success_count = 0
            should_enqueue = (
                result.result == EvaluationResultType.MATCH
                or previous_sample == EvaluationResultType.MATCH.value
                or from_recheck
            )
            if should_enqueue and not self._enqueue(item, state, result, from_recheck):
                return
            state.last_sample_state = result.result.value
            return

        state.consecutive_failure_count += 1
        state.recovery_success_count = 0
        if result.result in (
            EvaluationResultType.SOURCE_UNAVAILABLE,
            EvaluationResultType.COLLECTION_ERROR,
        ):
            state.source_status = SourceAvailability.UNAVAILABLE
            self._refresh_dse_inventory_after_source_failure(item)
        if not self._enqueue(item, state, result, from_recheck):
            state.source_status = previous_source

    def _refresh_dse_inventory_after_source_failure(self, item) -> None:
        """Promptly confirm whether a failed dynamic child still exists."""

        if item.dse_binding is None:
            return
        now = self.clock()
        for template_id in self._dse_templates_by_child.get(
            item.correlation_key, ()
        ):
            self.plan.expansion_state_by_key[
                template_id
            ].next_expansion_due = now
        self._next_poll = min(self._next_poll, now)

    def _enqueue(
        self,
        item,
        state: MonitorWorkStateRecord,
        result: EvaluationResult,
        from_recheck: bool,
    ) -> bool:
        self._sequence += 1
        previous_work_state = state.state
        self._transition(state, MonitorWorkState.IN_FLIGHT)
        enqueued_at = self.wall_clock()
        runtime_status = None
        if result.result in (
            EvaluationResultType.SOURCE_UNAVAILABLE,
            EvaluationResultType.SOURCE_RECOVERED,
            EvaluationResultType.COLLECTION_ERROR,
            EvaluationResultType.EVALUATION_ERROR,
        ):
            runtime_status = RuleRuntimeStatus(
                state="BROKEN" if not result.retryable else "DEGRADED",
                rule_id=item.rule_id,
                rule_name=item.rule_name,
                event_id=item.event_id,
                component_name=item.component_name,
                source_id=item.source_id,
                correlation_key=item.correlation_key,
                reason=result.error,
                error_category=result.error_category,
                failure_count=state.consecutive_failure_count,
                last_success_timestamp=state.last_success_timestamp,
                last_attempt_timestamp=enqueued_at,
                retryable=result.retryable,
            )
        event = FaultEvidenceEvent(
            signature_id=item.rule_id,
            event_id=item.event_id,
            component_name=item.component_name,
            source_id=item.source_id,
            correlation_key=item.correlation_key,
            monitor_id=self.plan.monitor_id,
            plan_generation=self.plan.plan_generation,
            work_state_generation=state.work_state_generation,
            sequence=self._sequence,
            event_timestamp=result.completed_at or enqueued_at,
            enqueue_timestamp=enqueued_at,
            result=result,
            from_recheck=from_recheck,
            runtime_status=runtime_status,
        )
        try:
            self.evidence_queue.put_nowait(event)
        except Full:
            # Restore eligibility when the primary queue is full.
            LOGGER.error("fault evidence queue is full; releasing %s", item.correlation_key)
            self._transition(state, previous_work_state)
            state.ack_deadline = None
            if not from_recheck:
                retry_at = self.clock()
                state.next_sample_due = retry_at
                self._next_poll = min(self._next_poll, retry_at)
            return False
        state.last_evidence_sequence = self._sequence
        state.last_enqueue_timestamp = enqueued_at
        state.ack_deadline = self.clock() + self.fault_evidence_ack_timeout
        return True
